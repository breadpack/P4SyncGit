# Phase 1: 기반 구축 — 단일 Stream 동기화

## 목표

- 단일 P4 stream(mainline)의 changelist를 Git commit으로 순차 변환
- 폴링 방식으로 시작 (트리거는 Phase 3에서 추가)
- 기본 인프라 검증 후 Phase 2로 확장

## 기술 스택

| 영역 | 기술 |
|------|------|
| Worker | Python 3.12+ (systemd/supervisor 데몬) |
| P4 연동 | p4python (공식 바인딩, dict/list 반환) |
| Git 연동 | pygit2 (libgit2 바인딩) + git CLI fallback |
| State DB | sqlite3 (Python 내장) |
| HTTP | FastAPI + uvicorn |
| 알림 | slack-sdk |
| 로깅 | Python 표준 logging (구조화 JSON) |
| 캐시/큐 | redis-py |

## 산출물

```
p4gitsync/
├── pyproject.toml                 # 프로젝트 메타데이터, 의존성
├── config.toml                    # 설정 파일
├── src/
│   └── p4gitsync/
│       ├── __init__.py
│       ├── __main__.py            # 엔트리포인트 (asyncio 루프)
│       ├── services/
│       │   ├── __init__.py
│       │   ├── sync_orchestrator.py   # 동기화 오케스트레이터
│       │   ├── changelist_poller.py   # P4 changelist 폴링
│       │   └── commit_builder.py      # Git commit 생성
│       ├── p4/
│       │   ├── __init__.py
│       │   ├── p4_client.py           # p4python API 래퍼
│       │   ├── p4_change_info.py      # Changelist 정보 모델
│       │   └── p4_file_action.py      # 파일 액션 모델
│       ├── git/
│       │   ├── __init__.py
│       │   ├── git_operator.py        # Git 조작 Protocol
│       │   ├── pygit2_git_operator.py # pygit2 기반 구현
│       │   ├── git_cli_operator.py    # Git CLI 기반 구현 (fallback)
│       │   └── commit_metadata.py     # Commit 메타데이터 모델
│       ├── state/
│       │   ├── __init__.py
│       │   ├── state_store.py         # SQLite 상태 관리
│       │   └── migrations/            # DB 스키마 마이그레이션
│       └── config/
│           ├── __init__.py
│           ├── sync_config.py         # 설정 모델 (dataclass)
│           └── lfs_config.py          # LFS 설정 모델 (LFS 적용 시)
└── tests/
    ├── __init__.py
    ├── test_p4_client.py              # 단위 테스트
    ├── test_state_store.py            # 단위 테스트
    ├── test_git_operator.py           # 단위 테스트
    ├── integration/                   # 통합 테스트
    │   ├── __init__.py
    │   ├── test_e2e_sync.py           # E2E: P4→Git 전체 동기화 플로우
    │   ├── test_initial_import.py     # E2E: 초기 히스토리 import + 재개
    │   ├── test_merge_analyzer.py     # 통합: MergeAnalyzer 회귀 테스트
    │   ├── test_circuit_breaker.py    # 통합: CircuitBreaker 회귀 테스트
    │   ├── test_error_recovery.py     # 통합: 에러 복구 시나리오
    │   └── conftest.py                # P4 mock 데이터, 공통 fixture
    ├── docker-compose.test.yml        # 테스트 환경 (P4 test server + Redis + Git bare repo)
    └── fixtures/                      # P4 mock 데이터
        ├── sample_changelists.json
        └── sample_depot_structure.txt
```

## Step 1-0: 사전 검증 및 분석

### 작업 내용

Phase 1 본격 구현 전에 기술적 리스크를 사전에 검증하고, 전체 구현 방향에 영향을 미치는 결정사항을 확정한다.

#### 1. pygit2 호환성 및 대규모 PoC

- `repo.create_commit`으로 parent를 직접 지정하여 **최소 10,000 commit** 연속 생성 테스트
- 메모리 누수, 성능 저하 여부 확인 — **메모리 프로파일링 필수** (`tracemalloc` 또는 `memory_profiler` 활용)
- 1,000 commit 단위로 메모리 사용량 스냅샷을 기록하여 증가 추이 분석
- 실패 시 Git CLI fallback 전략 검토

```python
import pygit2
import tracemalloc

tracemalloc.start()

repo = pygit2.Repository("/tmp/test-repo")
for i in range(10_000):
    # Tree 생성 → Commit 생성 → ref 업데이트
    tree = repo.TreeBuilder().write()
    parent = [repo.head.target] if not repo.head_is_unborn else []
    sig = pygit2.Signature("test", "test@example.com")
    repo.create_commit("refs/heads/main", sig, sig, f"commit {i}", tree, parent)

    if i % 1000 == 0:
        snapshot = tracemalloc.take_snapshot()
        print(f"[{i}] memory: {tracemalloc.get_traced_memory()}")
```

#### 2. LFS 도입 여부 결정을 위한 depot 분석

- 바이너리 파일 용량/확장자 분포 조사
- `p4 sizes -s //stream/...` 활용하여 전체 데이터 규모 파악
- **LFS 적용 시 최초 commit부터 `.gitattributes`를 포함해야 하므로, Phase 1 시작 전 결정 필수**

#### 3. p4python 호환성 검증

- p4python이 현재 P4 서버 버전과 정상 호환되는지 확인
- `P4.run_changes`, `P4.run_describe`, `P4.run_print` 등 핵심 API의 반환 형식(dict/list) 확인
- SSL 연결, charset 설정 등 환경별 특이사항 검증
- CLI 대비 성능 비교 (초기 import 용도)

```python
from P4 import P4, P4Exception

p4 = P4()
p4.port = "ssl:p4server:1666"
p4.user = "p4sync-service"
p4.client = "p4sync-main"
p4.connect()

# dict 형태로 반환됨
changes = p4.run_changes("-s", "submitted", "-m", "10", "//ProjectSTAR/main/...")
for ch in changes:
    print(ch["change"], ch["user"], ch["desc"])

describe = p4.run_describe("-s", changes[0]["change"])
print(describe[0]["depotFile"])  # list of depot paths
print(describe[0]["action"])     # list of actions

p4.disconnect()
```

#### 4. Stream path remap/exclude 사전 조사

- 동기화 대상 stream의 `p4 stream -o` 출력에서 `Paths`, `Remapped`, `Ignored` 필드 조사
- remap/exclude 규칙이 `p4 changes`, `p4 describe` 결과에 미치는 영향 분석
- Git 측에서 해당 규칙을 어떻게 반영할지 매핑 전략 수립

```python
stream_spec = p4.run_stream("-o", "//ProjectSTAR/main")
print(stream_spec[0].get("Paths"))
print(stream_spec[0].get("Remapped"))
print(stream_spec[0].get("Ignored"))
```

### 완료 기준

- [ ] pygit2 PoC 통과 (commit 10,000개 연속 생성 성공 + 메모리 프로파일링 결과 안정적)
- [ ] LFS 적용 범위 확정 (적용/미적용, 대상 확장자 목록)
- [ ] p4python 호환성 확인 완료 (핵심 API 정상 동작, 반환 형식 확인)
- [ ] Stream path remap/exclude 영향 분석 완료

## Step 1-1: 프로젝트 스캐폴딩

### 작업 내용

1. `p4gitsync/` 패키지 구조 생성 (src 레이아웃)
2. `pyproject.toml`에 의존성 정의
3. 설정 파일 구성 (`config.toml`)

### pyproject.toml

> **의존성 버전 고정 권장**: 재현 가능한 빌드를 위해 `pip-compile` (pip-tools) 또는 `uv lock` 등으로 lock 파일을 생성하여 정확한 의존성 버전을 고정할 것을 권장한다. `requirements.lock` 또는 `uv.lock`을 버전 관리에 포함하여 환경 간 일관성을 보장한다.

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "p4gitsync"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "p4python>=2024.1",
    "pygit2>=1.14",
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "slack-sdk>=3.27",
    "redis>=5.0",
    "tomli>=2.0; python_version < '3.11'",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.3",
    "memory-profiler>=0.61",
]

[project.scripts]
p4gitsync = "p4gitsync.__main__:main"
```

### 설정 구조 (config.toml)

```toml
[p4]
port = "ssl:p4server:1666"
user = "p4sync-service"
workspace = "p4sync-main"
stream = "//ProjectSTAR/main"

[git]
repo_path = "/data/p4sync/git-repo"
remote_url = "git@github.com:org/project-star.git"
default_branch = "main"

[state]
db_path = "/data/p4sync/state.db"

[sync]
polling_interval_seconds = 30
batch_size = 50
push_after_every_commit = false
file_extraction_mode = "print"       # "print" | "sync"
print_to_sync_threshold = 50
git_gc_interval = 5000

[logging]
level = "INFO"
format = "json"                      # "json" | "text"
file = "/var/log/p4gitsync/sync.log"

[slack]
webhook_url = ""
channel = "#p4sync-alerts"
error_retry_threshold = 3            # 이 횟수 초과 시 Slack 알림
```

### 엔트리포인트 (__main__.py)

```python
import asyncio
import logging
import signal
import tomllib
from pathlib import Path

from p4gitsync.services.sync_orchestrator import SyncOrchestrator

logger = logging.getLogger("p4gitsync")


def load_config(path: str = "config.toml") -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


async def run(config: dict) -> None:
    orchestrator = SyncOrchestrator(config)
    await orchestrator.start()


def main() -> None:
    import sys

    config = load_config()
    setup_logging(config.get("logging", {}))

    loop = asyncio.new_event_loop()

    # 플랫폼별 시그널 핸들러 등록
    if sys.platform != "win32":
        # Unix/Linux: asyncio 네이티브 시그널 핸들러 사용
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, loop.stop)
    else:
        # Windows: loop.add_signal_handler()는 NotImplementedError 발생
        # signal.signal()로 대체하여 graceful shutdown 지원
        def _win_signal_handler(signum: int, frame: object) -> None:
            loop.call_soon_threadsafe(loop.stop)

        signal.signal(signal.SIGINT, _win_signal_handler)
        signal.signal(signal.SIGTERM, _win_signal_handler)

    try:
        loop.run_until_complete(run(config))
    finally:
        loop.close()


def setup_logging(log_config: dict) -> None:
    import json as _json

    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            return _json.dumps({
                "time": self.formatTime(record),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            })

    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    handler = logging.StreamHandler()
    if log_config.get("format") == "json":
        handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler])


if __name__ == "__main__":
    main()
```

### 완료 기준

- [ ] `pip install -e .` 성공
- [ ] `python -m p4gitsync` 로 프로세스 시작/종료 정상
- [ ] 구조화 JSON 로깅 출력 확인
- [ ] 단위 테스트 통과

## Step 1-2: P4Client 구현

### 작업 내용

p4python 공식 바인딩을 사용하는 `P4Client` 클래스 구현. p4python은 dict/list 형태로 결과를 반환하므로 별도의 파싱 로직이 불필요하다.

```python
from dataclasses import dataclass, field
from P4 import P4, P4Exception


@dataclass
class P4FileAction:
    depot_path: str
    action: str          # add, edit, delete, move/add, move/delete, integrate, branch
    file_type: str       # text, binary, ...
    revision: int


@dataclass
class P4ChangeInfo:
    changelist: int
    user: str
    description: str
    timestamp: int
    files: list[P4FileAction] = field(default_factory=list)


class P4Client:
    def __init__(self, config: dict) -> None:
        self._p4 = P4()
        self._p4.port = config["p4"]["port"]
        self._p4.user = config["p4"]["user"]
        self._p4.client = config["p4"]["workspace"]

    def connect(self) -> None:
        self._p4.connect()

    def disconnect(self) -> None:
        self._p4.disconnect()

    def get_changes_after(self, stream: str, after_cl: int) -> list[int]:
        """지정 CL 이후의 submitted changelist 목록 조회 (오름차순)"""
        results = self._p4.run_changes(
            "-s", "submitted",
            "-e", str(after_cl + 1),
            f"{stream}/...",
        )
        # run_changes는 내림차순 반환 → 역순 정렬
        return sorted(int(r["change"]) for r in results)

    def describe(self, changelist: int) -> P4ChangeInfo:
        """changelist 상세 정보 (파일 목록, action, 설명, 작성자)"""
        results = self._p4.run_describe("-s", str(changelist))
        desc = results[0]
        files = [
            P4FileAction(
                depot_path=desc["depotFile"][i],
                action=desc["action"][i],
                file_type=desc["type"][i],
                revision=int(desc["rev"][i]),
            )
            for i in range(len(desc.get("depotFile", [])))
        ]
        return P4ChangeInfo(
            changelist=int(desc["change"]),
            user=desc["user"],
            description=desc["desc"],
            timestamp=int(desc["time"]),
            files=files,
        )

    def print_file(self, depot_path: str, revision: int, output_path: str) -> None:
        """특정 리비전의 파일 내용을 로컬 경로에 출력"""
        self._p4.run_print(
            "-o", output_path,
            f"{depot_path}#{revision}",
        )

    def sync(self, workspace: str, changelist: int) -> None:
        """워크스페이스를 특정 CL 시점으로 sync"""
        self._p4.run_sync(f"//{workspace}/...@{changelist}")
```

### p4python 배치 호출

p4python은 단일 연결 내에서 여러 명령을 순차 실행하므로 프로세스 생성 오버헤드가 없다. `run_filelog`에 다수의 depot path를 한 번에 전달하여 integration 분석을 효율적으로 수행할 수 있다.

> **배치 청크 제한**: `run_filelog`에 한 번에 수천 개의 depot path를 전달하면 P4 서버 메모리 부하 및 타임아웃이 발생할 수 있다. 대규모 CL(파일 수백~수천 개)의 경우 **100~500개 단위로 청크를 분할**하여 배치 호출한다. 청크 크기는 P4 서버 사양과 네트워크 환경에 따라 조정하며, `config.toml`의 `filelog_batch_size` (기본값: 200)로 설정 가능하게 한다.

```python
# 다중 파일 filelog 일괄 조회
filelogs = p4.run_filelog(
    "//ProjectSTAR/main/src/foo.py",
    "//ProjectSTAR/main/src/bar.py",
    "//ProjectSTAR/main/src/baz.py",
)
for filelog in filelogs:
    for rev in filelog.revisions:
        for integ in rev.integrations:
            print(f"{filelog.depotFile} <- {integ.file}")
```

### P4 obliterate 대응

`p4 print` 실패 시 (obliterate 등으로 파일이 삭제된 경우) 해당 파일을 건너뛰고 경고를 기록한다. CL 전체를 실패 처리하지 않고, 가능한 파일만 추출하여 commit을 생성한다.

```python
def print_file_safe(self, depot_path: str, revision: int, output_path: str) -> bool:
    """파일 추출 시도. obliterate 등으로 실패하면 False 반환."""
    try:
        self.print_file(depot_path, revision, output_path)
        return True
    except P4Exception as e:
        logger.warning(
            "파일 추출 실패 (obliterate?): %s#%d — %s",
            depot_path, revision, e,
        )
        return False
```

### User Mapping 기본값

매핑 테이블에 없는 P4 사용자는 `{p4user}@{company-domain}` 형식으로 Git author 정보를 자동 생성한다.

### User Mapping 초기 구축 절차

초기 import 전에 P4 사용자 → Git author 매핑 테이블을 구축한다. `p4 users` 명령으로 전체 사용자 목록을 조회하고, StateStore의 `user_mappings` 테이블에 일괄 등록한다.

```python
def build_initial_user_mapping(self, company_domain: str) -> int:
    """P4 전체 사용자 목록을 조회하여 기본 매핑 생성. 등록 건수 반환."""
    users = self._p4.run_users()
    count = 0
    for u in users:
        p4_user = u["User"]
        full_name = u.get("FullName", p4_user)
        email = u.get("Email", f"{p4_user}@{company_domain}")
        # StateStore에 upsert (Step 1-3 참조)
        self._state_store.upsert_user_mapping(p4_user, full_name, email)
        count += 1
    return count
```

### 완료 기준

- [ ] `get_changes_after` — CL 번호 리스트 정상 반환 (오름차순)
- [ ] `describe` — 파일 목록, action, 메타데이터 정상 반환 (dict 기반)
- [ ] `print_file` — 파일 내용 정상 추출
- [ ] User Mapping 초기 구축 완료
- [ ] 단위 테스트 통과 (test_p4_client.py)

## Step 1-3: StateStore 구현

### 작업 내용

Python 내장 `sqlite3` 기반 상태 관리. 스키마는 [Ref-Architecture.md](Ref-Architecture.md) 참조.

SQLite 초기화 시 `PRAGMA journal_mode=WAL` 설정을 적용하여 동시 읽기 성능을 확보한다.

```python
import sqlite3
from dataclasses import dataclass
from typing import Protocol


@dataclass
class StreamMapping:
    stream: str
    branch: str
    parent_stream: str | None = None
    branch_point_cl: int | None = None


class StateStore:
    """SQLite 기반 상태 관리.

    Note: Phase 1에서는 동기(def) 구현으로 시작한다.
    Phase 2에서 async def로 전환 예정 (asyncio 기반 Worker와 통합).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        """스키마 생성 + WAL 모드 설정"""
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS sync_state (
                stream      TEXT PRIMARY KEY,
                last_cl     INTEGER NOT NULL,
                commit_sha  TEXT NOT NULL,
                updated_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS cl_commit_map (
                changelist      INTEGER NOT NULL,
                commit_sha      TEXT NOT NULL,
                stream          TEXT NOT NULL,
                branch          TEXT NOT NULL,
                has_integration INTEGER DEFAULT 0,
                git_push_status TEXT DEFAULT 'pending',  -- pending / pushed / failed
                created_at      TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (changelist, stream)
            );
            CREATE TABLE IF NOT EXISTS user_mappings (
                p4_user     TEXT PRIMARY KEY,
                git_name    TEXT NOT NULL,
                git_email   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_errors (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                changelist  INTEGER NOT NULL,
                stream      TEXT NOT NULL,
                error_msg   TEXT,
                retry_count INTEGER DEFAULT 0,
                resolved    INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS stream_registry (
                stream              TEXT PRIMARY KEY,
                branch              TEXT NOT NULL,
                parent_stream       TEXT,
                branch_point_cl     INTEGER
            );
        """)
        self._conn.commit()

    def get_last_synced_cl(self, stream: str) -> int:
        """마지막 동기화 CL. 없으면 0 반환."""
        row = self._conn.execute(
            "SELECT last_cl FROM sync_state WHERE stream = ?", (stream,)
        ).fetchone()
        return row["last_cl"] if row else 0

    def set_last_synced_cl(self, stream: str, cl: int, commit_sha: str) -> None:
        self._conn.execute(
            """INSERT INTO sync_state (stream, last_cl, commit_sha)
               VALUES (?, ?, ?)
               ON CONFLICT(stream) DO UPDATE SET
                   last_cl = excluded.last_cl,
                   commit_sha = excluded.commit_sha,
                   updated_at = datetime('now')""",
            (stream, cl, commit_sha),
        )
        self._conn.commit()

    def get_commit_sha(self, changelist: int) -> str | None:
        """CL -> SHA 조회"""
        row = self._conn.execute(
            "SELECT commit_sha FROM cl_commit_map WHERE changelist = ?", (changelist,)
        ).fetchone()
        return row["commit_sha"] if row else None

    def record_commit(
        self,
        cl: int,
        sha: str,
        stream: str,
        branch: str,
        has_integration: bool = False,
    ) -> None:
        self._conn.execute(
            """INSERT INTO cl_commit_map (changelist, commit_sha, stream, branch, has_integration)
               VALUES (?, ?, ?, ?, ?)""",
            (cl, sha, stream, branch, int(has_integration)),
        )
        self._conn.commit()

    def get_git_author(self, p4_user: str) -> tuple[str, str]:
        """P4 사용자 -> (name, email) 조회. 없으면 기본값 반환."""
        row = self._conn.execute(
            "SELECT git_name, git_email FROM user_mappings WHERE p4_user = ?",
            (p4_user,),
        ).fetchone()
        if row:
            return (row["git_name"], row["git_email"])
        return (p4_user, f"{p4_user}@company.com")

    def upsert_user_mapping(self, p4_user: str, git_name: str, git_email: str) -> None:
        """사용자 매핑 등록/갱신"""
        self._conn.execute(
            """INSERT INTO user_mappings (p4_user, git_name, git_email)
               VALUES (?, ?, ?)
               ON CONFLICT(p4_user) DO UPDATE SET
                   git_name = excluded.git_name,
                   git_email = excluded.git_email""",
            (p4_user, git_name, git_email),
        )
        self._conn.commit()

    def verify_consistency(self, branch: str, git_head_sha: str) -> bool:
        """서비스 시작 시 Git 최신 commit의 SHA와 StateStore 교차 검증"""
        row = self._conn.execute(
            "SELECT commit_sha FROM sync_state WHERE stream IN "
            "(SELECT stream FROM stream_registry WHERE branch = ?)",
            (branch,),
        ).fetchone()
        if row is None:
            return True  # 초기 상태
        return row["commit_sha"] == git_head_sha

    def get_last_commit_before(self, stream: str, before_cl: int) -> str | None:
        """특정 CL 직전의 commit SHA (Phase 2 분기점 매핑용)"""
        row = self._conn.execute(
            """SELECT commit_sha FROM cl_commit_map
               WHERE stream = ? AND changelist < ?
               ORDER BY changelist DESC LIMIT 1""",
            (stream, before_cl),
        ).fetchone()
        return row["commit_sha"] if row else None

    def register_stream(self, mapping: StreamMapping) -> None:
        """stream 등록 (분기점 포함)"""
        self._conn.execute(
            """INSERT INTO stream_registry (stream, branch, parent_stream, branch_point_cl)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(stream) DO UPDATE SET
                   branch = excluded.branch,
                   parent_stream = excluded.parent_stream,
                   branch_point_cl = excluded.branch_point_cl""",
            (mapping.stream, mapping.branch, mapping.parent_stream, mapping.branch_point_cl),
        )
        self._conn.commit()

    def record_sync_error(self, changelist: int, stream: str, error_msg: str) -> int:
        """동기화 에러 기록. retry_count 반환."""
        row = self._conn.execute(
            "SELECT id, retry_count FROM sync_errors WHERE changelist = ? AND stream = ? AND resolved = 0",
            (changelist, stream),
        ).fetchone()
        if row:
            new_count = row["retry_count"] + 1
            self._conn.execute(
                "UPDATE sync_errors SET retry_count = ?, error_msg = ? WHERE id = ?",
                (new_count, error_msg, row["id"]),
            )
            self._conn.commit()
            return new_count
        else:
            self._conn.execute(
                "INSERT INTO sync_errors (changelist, stream, error_msg) VALUES (?, ?, ?)",
                (changelist, stream, error_msg),
            )
            self._conn.commit()
            return 1

    def close(self) -> None:
        if self._conn:
            self._conn.close()
```

### 완료 기준

- [ ] DB 초기화 및 스키마 생성 (WAL 모드 포함)
- [ ] CL <-> Commit 매핑 CRUD
- [ ] `verify_consistency`로 Git-DB 정합성 검증
- [ ] User Mapping upsert 동작 확인
- [ ] 단위 테스트 통과 (test_state_store.py)

## Step 1-4: GitOperator 구현

### 작업 내용

Python `Protocol`을 통해 `GitOperator`를 추상화하고, 두 가지 구현 전략을 제공한다.

- `Pygit2GitOperator`: pygit2 기반 (기본, 초기 import에 적합)
- `GitCliOperator`: Git CLI 기반 (pygit2 호환성 문제 시 fallback)

```python
from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass
class CommitMetadata:
    author_name: str
    author_email: str
    author_timestamp: int         # Unix timestamp
    message: str
    p4_changelist: int            # commit message에 P4CL 태그 포함용


class GitOperator(Protocol):
    """Git 조작 인터페이스 (Protocol 기반 structural subtyping)"""

    def create_commit(
        self,
        branch: str,
        parent_sha: str | None,
        metadata: CommitMetadata,
        working_dir: str,
    ) -> str:
        """일반 commit 생성 (parent 1개). commit SHA 반환."""
        ...

    def create_merge_commit(
        self,
        branch: str,
        parent_shas: Sequence[str],
        metadata: CommitMetadata,
        working_dir: str,
    ) -> str:
        """merge commit 생성 (parent 2개 이상). commit SHA 반환."""
        ...

    def push(self, branch: str) -> None:
        """remote push"""
        ...
```

### Pygit2GitOperator 구현

```python
import os
import pygit2


class Pygit2GitOperator:
    def __init__(self, repo_path: str, remote_url: str) -> None:
        self._repo_path = repo_path
        self._remote_url = remote_url
        self._repo: pygit2.Repository | None = None
        self._commit_count = 0

    def open(self) -> None:
        if os.path.exists(self._repo_path):
            self._repo = pygit2.Repository(self._repo_path)
        else:
            self._repo = pygit2.init_repository(self._repo_path, bare=False)

    def create_commit(
        self,
        branch: str,
        parent_sha: str | None,
        metadata: CommitMetadata,
        working_dir: str,
    ) -> str:
        """워킹 디렉토리의 파일 -> Tree 생성 -> Commit 생성 -> ref 업데이트"""
        tree_oid = self._build_tree(working_dir)
        parents = [pygit2.Oid(hex=parent_sha)] if parent_sha else []

        author = pygit2.Signature(
            metadata.author_name,
            metadata.author_email,
            metadata.author_timestamp,
        )
        committer = author  # 동기화 도구이므로 동일하게 설정

        message = f"{metadata.message}\n\n[P4CL: {metadata.p4_changelist}]"
        ref = f"refs/heads/{branch}"

        oid = self._repo.create_commit(ref, author, committer, message, tree_oid, parents)
        self._commit_count += 1
        return str(oid)

    def create_merge_commit(
        self,
        branch: str,
        parent_shas: Sequence[str],
        metadata: CommitMetadata,
        working_dir: str,
    ) -> str:
        tree_oid = self._build_tree(working_dir)
        parents = [pygit2.Oid(hex=sha) for sha in parent_shas]

        author = pygit2.Signature(
            metadata.author_name,
            metadata.author_email,
            metadata.author_timestamp,
        )
        committer = author  # 동기화 도구이므로 동일하게 설정
        message = f"{metadata.message}\n\n[P4CL: {metadata.p4_changelist}]"
        ref = f"refs/heads/{branch}"

        oid = self._repo.create_commit(ref, author, committer, message, tree_oid, parents)
        self._commit_count += 1
        return str(oid)

    def push(self, branch: str) -> None:
        remote = self._repo.remotes["origin"]
        remote.push([f"refs/heads/{branch}"])

    def _build_tree(self, working_dir: str) -> pygit2.Oid:
        """워킹 디렉토리의 파일을 재귀적으로 TreeBuilder에 추가하여 Tree 생성

        [Critical 성능 경고] 이 메서드는 매 commit마다 전체 디렉토리를 순회하여
        Tree를 처음부터 재구성한다. 파일 수가 많은 대규모 리포지토리에서는
        commit당 O(전체 파일 수)의 비용이 발생하여 초기 import 시 심각한 병목이 된다.

        개선 방향 (incremental tree update):
          이전 commit의 tree를 기반으로, 변경된 파일의 경로만 추적하여
          해당 subtree만 재구성하는 방식으로 전환해야 한다.
          예: 변경 파일 목록 → 경로별 TreeBuilder 캐시 → 변경된 subtree만 rebuild

        현재는 incremental 동기화(실시간 폴링, 소량 CL)에서만 사용하고,
        초기 대규모 import에는 반드시 git fast-import를 사용할 것.
        """
        return self._add_dir_to_tree(working_dir, working_dir)

    def _add_dir_to_tree(self, base_dir: str, current_dir: str) -> pygit2.Oid:
        # [Critical 성능 경고] 이 메서드는 매 호출 시 current_dir 아래의 모든 파일과
        # 서브디렉토리를 순회한다. 매 commit마다 전체 working directory를 재귀 순회하므로,
        # 파일 10만 개 이상의 리포지토리에서는 commit당 수십 초가 소요될 수 있다.
        #
        # TODO: incremental tree update 구현
        #   - 이전 commit의 tree OID를 캐시로 유지
        #   - 변경된 파일의 경로에 해당하는 subtree만 재구성
        #   - 변경이 없는 subtree는 이전 tree OID를 그대로 재사용
        #   예시:
        #     prev_tree = repo.get(prev_tree_oid)
        #     for changed_path in changed_files:
        #         # changed_path의 부모 디렉토리 chain만 TreeBuilder로 rebuild
        #         # 나머지 subtree는 prev_tree에서 OID를 복사
        tb = self._repo.TreeBuilder()
        for entry in sorted(os.listdir(current_dir)):
            full_path = os.path.join(current_dir, entry)
            if os.path.isfile(full_path):
                # create_blob_fromdisk() 사용 권장:
                # create_blob(f.read())는 파일 전체를 메모리에 올리므로
                # 대용량 바이너리 파일에서 OOM 위험이 있다.
                # create_blob_fromdisk()는 스트리밍으로 처리하여 메모리 사용을 최소화한다.
                blob_oid = self._repo.create_blob_fromdisk(full_path)
                tb.insert(entry, blob_oid, pygit2.GIT_FILEMODE_BLOB)
            elif os.path.isdir(full_path) and entry != ".git":
                subtree_oid = self._add_dir_to_tree(base_dir, full_path)
                tb.insert(entry, subtree_oid, pygit2.GIT_FILEMODE_TREE)
        return tb.write()

    @property
    def commit_count(self) -> int:
        return self._commit_count
```

### pygit2 메모리 관리

대규모 import 시 pygit2의 메모리 사용량이 증가할 수 있다. 매 1,000 commit마다 `Repository` 객체를 재생성하거나, 설정된 간격(`git_gc_interval`)마다 `git gc`를 실행하여 메모리와 저장소 크기를 관리한다.

```python
import subprocess

def maybe_run_gc(self, gc_interval: int) -> None:
    """설정된 간격마다 git gc 실행"""
    if self._commit_count > 0 and self._commit_count % gc_interval == 0:
        subprocess.run(
            ["git", "gc", "--auto"],
            cwd=self._repo_path,
            check=True,
        )
        # Repository 객체 재생성으로 메모리 해제
        self._repo = pygit2.Repository(self._repo_path)
```

### 완료 기준

- [ ] 로컬 Git repo에 commit 생성 확인
- [ ] `git log --oneline` 으로 P4 메타데이터(`[P4CL: ...]` 태그) 확인
- [ ] push 정상 동작
- [ ] 단위 테스트 통과 (test_git_operator.py)

## Step 1-5: SyncOrchestrator 구현

### 작업 내용

전체 동기화 루프를 제어하는 오케스트레이터. asyncio 기반 루프로 구현한다.

```
루프 (polling_interval_seconds 마다):
  1. StateStore에서 마지막 동기화 CL 조회
  2. P4Client.get_changes_after로 신규 CL 목록 조회
  3. 각 CL에 대해 순차 처리:
     a. P4Client.describe(cl) -> 변경 파일 목록/action 확인
     b. 파일별 action에 따라 처리:
        - add/edit: P4Client.print_file(depot_path, revision, output_path)
        - delete: 파일 삭제
        - move/add + move/delete: rename 처리
     c. GitOperator.create_commit(...)
     d. StateStore.record_commit(...)
  4. (설정에 따라) GitOperator.push
```

```python
import asyncio
import logging

logger = logging.getLogger("p4gitsync.orchestrator")


class SyncOrchestrator:
    def __init__(self, config: dict) -> None:
        self._config = config
        self._sync_config = config["sync"]
        self._p4_client: P4Client | None = None
        self._git_operator: Pygit2GitOperator | None = None
        self._state_store: StateStore | None = None
        self._running = False

    async def start(self) -> None:
        """서비스 시작: 초기화 -> 정합성 검증 -> 폴링 루프"""
        self._initialize_components()
        self._verify_on_startup()
        self._running = True

        logger.info("동기화 시작: stream=%s", self._config["p4"]["stream"])

        while self._running:
            try:
                await self._poll_and_sync()
            except Exception:
                logger.exception("폴링 루프 에러")
            await asyncio.sleep(self._sync_config["polling_interval_seconds"])

    async def stop(self) -> None:
        self._running = False
        if self._p4_client:
            self._p4_client.disconnect()
        if self._state_store:
            self._state_store.close()

    def _initialize_components(self) -> None:
        self._state_store = StateStore(self._config["state"]["db_path"])
        self._state_store.initialize()

        self._p4_client = P4Client(self._config)
        self._p4_client.connect()

        self._git_operator = Pygit2GitOperator(
            self._config["git"]["repo_path"],
            self._config["git"]["remote_url"],
        )
        self._git_operator.open()

    def _verify_on_startup(self) -> None:
        """서비스 시작 시 Git HEAD와 StateStore 정합성 검증"""
        branch = self._config["git"]["default_branch"]
        try:
            repo = pygit2.Repository(self._config["git"]["repo_path"])
            head_sha = str(repo.head.target)
        except Exception:
            logger.info("Git HEAD 없음 (초기 상태)")
            return

        if not self._state_store.verify_consistency(branch, head_sha):
            logger.error(
                "정합성 불일치! Git HEAD=%s vs StateStore. 수동 확인 필요.", head_sha
            )
            raise RuntimeError("Git-StateStore 정합성 불일치. 수동 확인 후 재시작하세요.")

    async def _poll_and_sync(self) -> None:
        stream = self._config["p4"]["stream"]
        branch = self._config["git"]["default_branch"]
        last_cl = self._state_store.get_last_synced_cl(stream)

        changes = self._p4_client.get_changes_after(stream, last_cl)
        if not changes:
            return

        batch_size = self._sync_config["batch_size"]
        for cl in changes[:batch_size]:
            try:
                await self._process_changelist(cl, stream, branch)
            except Exception as e:
                retry_count = self._state_store.record_sync_error(cl, stream, str(e))
                logger.error("CL %d 처리 실패 (retry=%d): %s", cl, retry_count, e)
                if retry_count >= self._sync_config.get("error_retry_threshold", 3):
                    await self._send_slack_alert(cl, stream, str(e))
                if self._should_skip(cl, stream):
                    logger.warning("CL %d 건너뜀", cl)
                    continue
                else:
                    logger.error("CL %d는 건너뛸 수 없음 (blocking error). 중단.", cl)
                    break

        # push 설정에 따라 일괄 push
        if not self._sync_config.get("push_after_every_commit", False):
            self._git_operator.push(branch)

    async def _process_changelist(self, cl: int, stream: str, branch: str) -> None:
        """단일 CL 처리: 파일 추출 -> commit 생성 -> 상태 기록"""
        info = self._p4_client.describe(cl)
        threshold = self._sync_config.get("print_to_sync_threshold", 50)

        # 적응형 파일 추출
        if len(info.files) > threshold:
            self._extract_via_sync(cl)
        else:
            self._extract_via_print(info)

        # Git commit
        name, email = self._state_store.get_git_author(info.user)
        metadata = CommitMetadata(
            author_name=name,
            author_email=email,
            author_timestamp=info.timestamp,
            message=info.description,
            p4_changelist=cl,
        )
        parent_sha = self._state_store.get_commit_sha(
            self._state_store.get_last_synced_cl(stream)
        )
        sha = self._git_operator.create_commit(
            branch, parent_sha, metadata, self._working_dir
        )

        # 상태 기록
        self._state_store.record_commit(cl, sha, stream, branch)
        self._state_store.set_last_synced_cl(stream, cl, sha)

        # git gc 체크
        gc_interval = self._sync_config.get("git_gc_interval", 5000)
        self._git_operator.maybe_run_gc(gc_interval)

        # commit별 push
        if self._sync_config.get("push_after_every_commit", False):
            self._git_operator.push(branch)

        logger.info("CL %d -> commit %s", cl, sha[:8])
```

### 적응형 파일 추출

기본 파일 추출 방식은 `p4 print` (p4python의 `run_print`)이다. 변경 파일 수가 임계값(`print_to_sync_threshold`, 기본 50)을 초과하는 CL은 `p4 sync` (`run_sync`)로 전환하는 하이브리드 전략을 사용한다.

```toml
[sync]
file_extraction_mode = "print"       # "print" | "sync"
print_to_sync_threshold = 50
git_gc_interval = 5000
```

### 서비스 시작 시 정합성 확인

서비스 시작 시 `StateStore.verify_consistency`를 호출하여 Git HEAD의 P4CL 태그와 StateStore의 마지막 동기화 CL이 일치하는지 검증한다. 불일치 시 경고를 기록하고 수동 확인을 요구한다.

### 에러 처리

```
CL 처리 실패 시:
  1. sync_errors 테이블에 기록
  2. 해당 CL 건너뛰고 다음 CL 처리 (configurable)
  3. retry_count가 error_retry_threshold(기본 3) 초과 시 Slack 알림
  4. 수동 해결 후 resolved 플래그로 재시도
```

### 에러 건너뛰기 조건

```
CL 건너뛰기 가능 조건:
  1. 다른 stream의 merge source가 아닌 CL
  2. integration record에서 참조되지 않는 CL

건너뛰기 불가 (blocking error):
  1. 다른 stream에서 merge source로 참조되는 CL
  -> Slack 알림 + 수동 해결 필수
```

### Slack 알림

```python
from slack_sdk.webhook import WebhookClient


async def _send_slack_alert(self, cl: int, stream: str, error: str) -> None:
    webhook_url = self._config.get("slack", {}).get("webhook_url")
    if not webhook_url:
        return
    client = WebhookClient(webhook_url)
    client.send(text=f":warning: P4GitSync 에러\nCL: {cl}\nStream: {stream}\n```{error}```")
```

### 완료 기준

- [ ] 단일 stream의 CL이 순차적으로 Git commit으로 변환
- [ ] 서비스 재시작 시 마지막 CL부터 재개
- [ ] 에러 발생 시 건너뛰기 + 기록 + Slack 알림
- [ ] 단위 테스트 통과

## Step 1-5.1: 통합 테스트 환경 구성

> **[Critical] 테스트 전략 보강**
>
> 현재 단위 테스트 3개(test_p4_client.py, test_state_store.py, test_git_operator.py)만
> 계획되어 있으나, E2E 통합 테스트 없이는 전체 동기화 파이프라인의 안정성을 보장할 수 없다.
> Phase 1 완료 전에 반드시 통합 테스트 환경을 구축해야 한다.

### 작업 내용

#### 1. Docker Compose 기반 테스트 환경

테스트 실행 시 격리된 환경에서 전체 파이프라인을 검증하기 위해 Docker Compose로 테스트 인프라를 구성한다.

```yaml
# docker-compose.test.yml
services:
  p4d:
    image: perforce/helix-core:latest
    ports:
      - "1666:1666"
    environment:
      - P4PORT=1666
    volumes:
      - ./fixtures:/fixtures

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  git-server:
    image: gitea/gitea:latest
    ports:
      - "3000:3000"
      - "2222:22"
    environment:
      - GITEA__database__DB_TYPE=sqlite3
```

#### 2. P4 Mock 데이터 관리

```
fixtures/
├── sample_changelists.json      # CL 메타데이터 (action, 파일 목록)
├── sample_depot_structure.txt   # depot 디렉토리 구조
├── setup_p4_test_data.py        # P4 test server에 mock 데이터 주입 스크립트
└── scenarios/
    ├── basic_sync.py            # 기본 동기화 시나리오 (add/edit/delete)
    ├── rename_move.py           # rename/move 시나리오
    ├── large_cl.py              # 대규모 CL (파일 1000+) 시나리오
    └── error_recovery.py        # obliterate, 연결 실패 등 에러 시나리오
```

#### 3. 통합 테스트 항목

```python
# tests/integration/test_e2e_sync.py
class TestE2ESync:
    """P4 → Git 전체 동기화 파이프라인 E2E 테스트"""

    def test_basic_sync_flow(self):
        """P4에 CL 제출 → 동기화 → Git commit 생성 → 내용 일치 확인"""

    def test_delete_file_sync(self):
        """P4에서 파일 삭제 → Git에서 파일 삭제 확인"""

    def test_rename_file_sync(self):
        """P4에서 파일 rename → Git에서 rename 확인"""

    def test_large_changelist(self):
        """파일 1000개 이상의 대규모 CL 동기화"""

    def test_resume_after_interruption(self):
        """동기화 중단 후 재시작 시 마지막 CL부터 재개"""

    def test_error_skip_and_continue(self):
        """특정 CL 에러 시 건너뛰기 후 다음 CL 정상 처리"""


# tests/integration/test_initial_import.py
class TestInitialImport:
    """초기 히스토리 import E2E 테스트"""

    def test_full_history_import(self):
        """전체 히스토리 import 후 CL 수 == commit 수 확인"""

    def test_checkpoint_resume(self):
        """import 중단 후 체크포인트 기반 재개"""

    def test_fast_import_correctness(self):
        """fast-import 결과와 pygit2 결과의 동일성 검증"""


# tests/integration/test_merge_analyzer.py (Phase 2 대비)
class TestMergeAnalyzer:
    """MergeAnalyzer 회귀 테스트"""

    def test_simple_merge(self):
        """단순 merge CL의 Git merge commit 생성"""

    def test_cherry_pick_merge(self):
        """cherry-pick 스타일 merge 처리"""


# tests/integration/test_circuit_breaker.py
class TestCircuitBreaker:
    """CircuitBreaker 회귀 테스트"""

    def test_integrity_failure_triggers_open(self):
        """무결성 검증 실패 시 circuit breaker OPEN 전환"""

    def test_reset_allows_sync_resume(self):
        """reset 후 동기화 재개"""
```

#### 4. CI 파이프라인 자동 실행

```yaml
# .github/workflows/test.yml (또는 CI 시스템에 맞게 조정)
name: P4GitSync Tests
on: [push, pull_request]
jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -k "not integration" --tb=short

  integration-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker compose -f tests/docker-compose.test.yml up -d
      - run: pip install -e ".[dev]"
      - run: pytest tests/integration/ --tb=short
      - run: docker compose -f tests/docker-compose.test.yml down
```

### 완료 기준

- [ ] Docker Compose 테스트 환경 구동 확인 (P4 test server + Redis + Git bare repo)
- [ ] P4 mock 데이터 주입 스크립트 동작 확인
- [ ] E2E 동기화 테스트 통과 (basic sync, delete, rename, large CL)
- [ ] 초기 import 테스트 통과 (전체 히스토리, 체크포인트 재개)
- [ ] MergeAnalyzer, CircuitBreaker 회귀 테스트 통과
- [ ] CI 파이프라인에서 자동 실행 확인

## Step 1-6: 초기 히스토리 Import

최초 실행 시 기존 히스토리를 일괄 import하는 모드.

> **중요**: Git으로 완전 전환이 목표이므로, `git blame`/`git bisect`/`git log`가
> 과거 히스토리에서도 유의미하게 동작해야 한다. **전체 히스토리 import(옵션 A)를 기본으로 한다.**

```
옵션 A: 전체 히스토리 import (기본값, 권장)
  - p4.run_changes("-s", "submitted", "//stream/...") 로 전체 CL 목록
  - CL 1번부터 순차 replay
  - 시간 오래 걸림 (CL 수에 비례)
  - git blame, bisect가 전체 히스토리에서 동작
  - 전환 목적에서 유일하게 완전한 옵션

옵션 B: 특정 시점부터 시작 (마이그레이션에는 부적합)
  - 현재 stream의 최신 상태를 initial commit으로 생성
  - 이후 CL부터 incremental 동기화
  - 과거 히스토리 소실 -> 전환 후 git blame 불완전
  - 임시 미러링 용도에만 적합

옵션 C: 하이브리드 (차선책)
  - 최근 N개월 히스토리만 import
  - 그 이전은 하나의 initial commit으로 압축
  - git blame이 압축 구간에서 무의미
  - 전체 import가 현실적으로 불가능할 때만 사용
```

### git fast-import (초기 import 기본 전략)

> **[Critical] fast-import는 초기 import의 기본 전략이다.**
>
> pygit2의 `create_commit` + `_build_tree` 방식은 매 commit마다 전체 디렉토리를
> 순회하여 Tree를 재구성하므로 대규모 히스토리 import에서 심각한 성능 병목이 된다
> (Step 1-4 `_add_dir_to_tree` 성능 경고 참조). `git fast-import`는 스트림 기반으로
> 변경분만 전달하면 되므로, 초기 import에서는 반드시 fast-import를 사용해야 한다.
> pygit2 방식은 incremental 동기화(실시간 폴링)에서만 사용한다.

대규모 초기 import 시 `git fast-import`를 사용한다. pygit2의 `create_commit`은 commit별로 Tree를 구성하는 반면, `git fast-import`는 스트림 기반으로 한 번에 다수의 commit을 생성한다.

```python
import subprocess


class FastImporter:
    """git fast-import를 통한 대규모 히스토리 일괄 import"""

    def __init__(self, repo_path: str) -> None:
        self._repo_path = repo_path
        self._proc: subprocess.Popen | None = None
        self._mark = 0

    def start(self) -> None:
        self._proc = subprocess.Popen(
            ["git", "fast-import", "--force", "--quiet"],
            stdin=subprocess.PIPE,
            cwd=self._repo_path,
        )

    def add_commit(
        self,
        branch: str,
        metadata: CommitMetadata,
        files: list[tuple[str, bytes]],        # (path, content)
        deletes: list[str] | None = None,
    ) -> int:
        self._mark += 1
        lines = [
            f"commit refs/heads/{branch}",
            f"mark :{self._mark}",
            f"author {metadata.author_name} <{metadata.author_email}> {metadata.author_timestamp} +0000",
            f"committer {metadata.author_name} <{metadata.author_email}> {metadata.author_timestamp} +0000",
        ]
        msg = f"{metadata.message}\n\n[P4CL: {metadata.p4_changelist}]"
        msg_bytes = msg.encode("utf-8")
        lines.append(f"data {len(msg_bytes)}")

        self._write("\n".join(lines) + "\n")
        self._proc.stdin.write(msg_bytes + b"\n")

        for path, content in files:
            self._write(f"M 100644 inline {path}\n")
            self._write(f"data {len(content)}\n")
            self._proc.stdin.write(content + b"\n")

        for path in (deletes or []):
            self._write(f"D {path}\n")

        self._write("\n")
        return self._mark

    def checkpoint(self) -> None:
        """체크포인트 생성 — 중단 시 이 지점부터 재개 가능"""
        self._write("checkpoint\n\n")
        self._proc.stdin.flush()

    def finish(self) -> None:
        self._proc.stdin.close()
        self._proc.wait()

    def _write(self, data: str) -> None:
        self._proc.stdin.write(data.encode("utf-8"))
```

### 체크포인트 기반 재개 메커니즘

초기 import가 중단되었을 때 처음부터 다시 시작하지 않고 마지막 체크포인트부터 재개할 수 있어야 한다.

```
재개 절차:
  1. import 시작 전 StateStore에서 마지막 처리된 CL 조회
  2. 해당 CL 이후부터 import 재개
  3. git fast-import의 checkpoint 명령으로 주기적 저장 (매 1,000 CL)
  4. checkpoint마다 StateStore에 진행 상태 기록
  5. 중단 후 재시작 시:
     a. StateStore에서 마지막 checkpoint CL 조회
     b. Git repo에서 해당 commit 존재 여부 확인
     c. 일치하면 해당 CL 이후부터 재개
     d. 불일치하면 마지막 유효 checkpoint로 rollback 후 재개
```

```python
class InitialImporter:
    CHECKPOINT_INTERVAL = 1000

    def __init__(self, config: dict, p4_client: P4Client,
                 state_store: StateStore) -> None:
        self._config = config
        self._p4 = p4_client
        self._state = state_store
        self._import_config = config.get("initial_import", {})

    def run(self, stream: str, branch: str) -> None:
        """전체 히스토리 import 실행 (재개 지원)"""
        last_cl = self._state.get_last_synced_cl(stream)
        all_changes = self._p4.get_changes_after(stream, last_cl)

        logger.info(
            "초기 import 시작: stream=%s, 대상 CL=%d개, 재개 시점=CL %d",
            stream, len(all_changes), last_cl,
        )

        fast_importer = FastImporter(self._config["git"]["repo_path"])
        fast_importer.start()

        for i, cl in enumerate(all_changes):
            info = self._p4.describe(cl)
            files, deletes = self._extract_files(info)
            name, email = self._state.get_git_author(info.user)

            metadata = CommitMetadata(
                author_name=name,
                author_email=email,
                author_timestamp=info.timestamp,
                message=info.description,
                p4_changelist=cl,
            )
            mark = fast_importer.add_commit(branch, metadata, files, deletes)

            # 체크포인트
            if (i + 1) % self.CHECKPOINT_INTERVAL == 0:
                fast_importer.checkpoint()
                self._state.set_last_synced_cl(stream, cl, f"mark:{mark}")
                logger.info("체크포인트: CL %d (%d/%d)", cl, i + 1, len(all_changes))

        fast_importer.finish()
        logger.info("초기 import 완료: %d CL 처리", len(all_changes))
```

### 소요 시간 예측 및 벤치마크

> **[Critical] 초기 import 소요 시간에 대한 현실적 평가**
>
> 아래 예상치는 P4 서버의 throttle 정책, 네트워크 지연, 대규모 CL의 파일 수 편차,
> P4 서버 동시 사용자 부하 등을 종합적으로 고려한 값이다.
> 초기 추정("1~4주")은 이상적 조건에서의 낙관적 수치로 판명되었으며,
> 실제 운영 환경에서는 아래의 현실적 예상을 기준으로 계획해야 한다.

```
초기 import 시간은 CL 수에 비례한다. Phase 1 시작 시 반드시 벤치마크를 수행.

측정 항목:
  - CL 1개당 평균 처리 시간 (p4 run_print + git fast-import)
  - p4python run_print 방식 vs run_sync 방식 비교
  - git fast-import vs pygit2 create_commit 성능 비교
  - P4 서버 부하 (p4 monitor show)
  - P4 서버 throttle 정책에 의한 대기 시간 측정
  - 네트워크 지연 (P4 서버 ↔ Worker 간 RTT, 대역폭)
  - 메모리 사용량 추이 (tracemalloc)

현실적 예상 (P4 throttle/네트워크 지연/서버 부하 고려):
  CL 1개당 평균 2~10초 (파일 수에 따라 편차 큼)
  대규모 CL (파일 1000개+)은 건당 수분 소요 가능
  P4 서버 throttle에 의한 대기: CL당 추가 1~5초 (피크 시간대)
  네트워크 지연: CL당 추가 0.5~2초 (서버 위치에 따라)
  CL 10만 개 기준: 4~12주 (P4 throttle/네트워크 지연/서버 부하 포함)
  fast-import 사용 시 30~50% 단축 기대

벤치마크 결과에 따라 옵션 A/C 최종 결정.
```

### P4 서버 부하 관리

#### P4 Replica/Edge 서버 활용

초기 import 시 P4 master 서버에 대한 부하를 최소화하기 위해, 가능한 경우 **P4 Replica 또는 Edge 서버**를 통해 파일을 추출한다. p4python 연결 시 Replica/Edge 서버의 주소를 지정하면 된다.

```python
# Replica 서버를 통한 읽기 전용 접근 (초기 import용)
p4_replica = P4()
p4_replica.port = "ssl:p4-replica:1666"   # Replica/Edge 서버
p4_replica.user = "p4sync-service"
p4_replica.connect()
```

#### 부하 모니터링 및 자동 throttle

import 중 `p4 monitor show`로 서버 부하를 주기적으로 확인한다. 과부하가 감지되면 자동으로 throttle하여 P4 서버 영향을 최소화한다.

```python
import time


def check_server_load(p4: P4Client) -> bool:
    """P4 서버 부하 확인. 과부하 시 True 반환."""
    try:
        monitors = p4._p4.run_monitor("show")
        active_commands = len([m for m in monitors if m.get("status") == "R"])
        return active_commands > 50  # 임계값 (환경에 따라 조정)
    except P4Exception:
        return False


def throttled_import(p4: P4Client, changes: list[int]) -> None:
    for cl in changes:
        while check_server_load(p4):
            logger.warning("P4 서버 과부하 감지. 60초 대기.")
            time.sleep(60)
        # ... CL 처리
```

### git gc 자동 실행

매 N commit(설정 가능, 기본 `git_gc_interval` = 5000)마다 `git gc`를 자동 실행하여 Git 저장소 크기를 관리한다.

### LFS 적용 (Step 1-0에서 결정)

```
LFS 적용 시:
  - 최초 commit에 .gitattributes 포함
  - LFS 대상 확장자의 바이너리 파일은 LFS pointer로 변환하여 commit
  - .gitattributes 내용은 설정 파일에서 관리
```

### 전역 CL 정렬 안정성

여러 stream을 대상으로 import할 때 동일 CL 번호가 여러 stream에서 참조될 수 있다. **동일 CL 번호의 이벤트가 여러 개 존재할 경우, stream 이름을 2차 정렬 키로 사용**하여 정렬 안정성(stable sort)을 보장한다. 이를 통해 재실행 시에도 동일한 순서로 commit이 생성된다.

```python
# 여러 stream의 변경사항을 병합 정렬할 때
all_events = []
for stream in streams:
    changes = p4_client.get_changes_after(stream, last_cl)
    for cl in changes:
        all_events.append((cl, stream))

# CL 번호 1차, stream 이름 2차 정렬 (안정적)
all_events.sort(key=lambda x: (x[0], x[1]))
```

### 설정

```toml
[initial_import]
mode = "full_history"               # "full_history" | "from_changelist" | "hybrid"
start_changelist = 1
batch_size = 100
resume_on_restart = true
checkpoint_interval = 1000          # fast-import checkpoint 간격
use_fast_import = true              # git fast-import 사용 여부
replica_port = ""                   # P4 Replica 서버 (비어있으면 기본 서버 사용)
```

### 완료 기준

- [ ] 전체 CL 히스토리 import 완료 (또는 벤치마크 기반으로 옵션 C 결정)
- [ ] import 후 incremental 동기화로 자동 전환
- [ ] `git log --oneline | wc -l` 과 `p4 changes -s submitted //stream/... | wc -l` 일치 확인
- [ ] 중단 후 재시작 시 체크포인트 기반 재개 동작 확인
- [ ] 단위 테스트 통과
- [ ] 통합 테스트 통과 (Step 1-5.1의 E2E 테스트 항목)
