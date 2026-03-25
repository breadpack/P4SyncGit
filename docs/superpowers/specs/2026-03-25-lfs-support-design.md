# Git LFS Support Design

## Overview

P4GitSync에 Git LFS 지원을 추가하여 1TB 규모 P4 depot의 바이너리 에셋을 효율적으로 Git으로 동기화한다. 핵심은 `LfsObjectStore` 레이어를 도입하여 LFS object의 스트리밍 저장/조회를 캡슐화하는 것이다.

## Requirements

| 항목 | 결정 |
|------|------|
| LFS 저장소 | 로컬 bare repo `.git/lfs/objects/` + remote 내장 LFS |
| 메모리 관리 | P4에서 스트리밍 청크 처리 (OOM 방지) |
| 대상 판정 | 확장자 기반 (`LfsConfig.extensions`) |
| 역방향 동기화 | LFS 포인터 감지 → 실제 파일 복원 후 P4 제출 |
| Push 타이밍 | `git push` 시 `git lfs push` 일괄 실행 |
| 인증 | Git credential 기본, `config.toml`로 override 가능 |

## Architecture

### 데이터 흐름 (P4 → Git)

```
P4 Server
  ↓ p4 print (스트리밍 청크)
P4Client.print_file_stream(depot_path, revision)
  ↓ Iterable[bytes]
  ├─ LFS 대상 → LfsObjectStore.store_from_stream(chunks)
  │    ├─ SHA256 계산 (incremental)
  │    ├─ .git/lfs/objects/<oid[0:2]>/<oid[2:4]>/<oid> 에 atomic write
  │    └─ LfsPointer(oid, size, pointer_bytes) 반환
  │         ↓
  │    CommitBuilder → Git blob (pointer_bytes)
  │
  └─ 일반 파일 → 기존 흐름 (bytes → Git blob)
  ↓
GitOperator.create_commit()
  ↓
git push + git lfs push → Remote (Git + LFS)
```

### 데이터 흐름 (Git → P4, 역방향)

```
Git commit
  ↓
ReverseCommitBuilder.sync_commit()
  ↓ file_changes에서 LFS 포인터 감지
  ├─ LFS 포인터 → LfsObjectStore.retrieve(oid) → 실제 파일 경로
  │    ↓ 파일 내용 읽어서 P4에 제출
  └─ 일반 파일 → 기존 흐름
```

## Components

### 1. LfsObjectStore (신규)

**파일:** `p4gitsync/src/p4gitsync/lfs/lfs_object_store.py`

LFS object의 저장/조회를 담당하는 단일 책임 클래스.

```python
@dataclass(frozen=True)
class LfsPointer:
    oid: str          # sha256 hex
    size: int         # 원본 파일 크기 (bytes)
    pointer_bytes: bytes  # Git blob에 저장할 포인터 텍스트

class LfsObjectStore:
    def __init__(self, git_dir: Path):
        """git_dir: bare repo 경로 또는 .git 디렉토리"""

    def store_from_stream(self, chunks: Iterable[bytes]) -> LfsPointer:
        """스트리밍 저장. 청크를 순회하며 SHA256 + 임시파일 쓰기 → atomic move.
        이미 존재하면 임시파일 삭제 후 기존 pointer 반환."""

    def exists(self, oid: str) -> bool:
        """해당 OID의 object가 로컬에 존재하는지"""

    def retrieve(self, oid: str) -> Path:
        """OID로 실제 파일 경로 반환. 없으면 FileNotFoundError."""

    def object_path(self, oid: str) -> Path:
        """OID → .git/lfs/objects/<oid[0:2]>/<oid[2:4]>/<oid>"""
```

**저장 흐름:**
1. `lfs/tmp/` 하위에 임시 파일 생성 (`NamedTemporaryFile`, `delete=False`)
2. 청크마다 `hashlib.sha256.update()` + 임시 파일에 write, size 누적
3. 스트림 종료 → OID 확정 → 최종 경로로 `os.replace()` (atomic, cross-platform)
4. 이미 존재 시 임시 파일 삭제 (idempotent)

### 2. P4Client 스트리밍 API (신규 메서드)

**파일:** `p4gitsync/src/p4gitsync/p4/p4_client.py`

```python
def print_file_stream(self, depot_path: str, revision: int,
                      chunk_size: int = 4 * 1024 * 1024) -> Iterator[bytes]:
    """p4 print 결과를 chunk_size 단위로 yield.
    p4python의 handler API를 활용하여 메모리에 전체 로드하지 않음."""
```

**구현 전략:**
- `p4python`의 `P4.run_print()`는 전체 내용을 메모리에 반환하므로, `OutputHandler`를 사용하여 청크 단위로 처리
- `OutputHandler.outputBinary()` / `outputText()` 콜백에서 청크를 queue에 넣고, generator가 소비
- chunk_size 기본값 4MB (네트워크/메모리 균형)

**fallback:** p4python OutputHandler가 제한적인 경우, `p4 print -o <tmpfile>` CLI로 파일에 직접 출력 후 파일을 청크 단위로 읽기

### 3. CommitBuilder 변경

**파일:** `p4gitsync/src/p4gitsync/services/commit_builder.py`

현재: P4에서 전체 바이트를 받아 `LfsConfig.create_lfs_pointer(content)` 호출
변경: LFS 대상 파일은 스트리밍으로 `LfsObjectStore`에 저장하고 포인터만 받기

```python
# 변경 전 (현재)
content = self._p4.print_file_to_bytes(fa.depot_path, fa.revision)
if self._lfs and self._lfs.enabled and self._lfs.is_lfs_target(git_path):
    content = LfsConfig.create_lfs_pointer(content)

# 변경 후
if self._lfs and self._lfs.enabled and self._lfs.is_lfs_target(git_path):
    chunks = self._p4.print_file_stream(fa.depot_path, fa.revision)
    pointer = self._lfs_store.store_from_stream(chunks)
    content = pointer.pointer_bytes
else:
    content = self._p4.print_file_to_bytes(fa.depot_path, fa.revision)
```

- `__init__`에 `lfs_store: LfsObjectStore | None` 파라미터 추가
- batch print 경로도 동일하게 변경: LFS 대상은 개별 스트리밍, 나머지는 batch 유지

### 4. InitialImporter 변경

**파일:** `p4gitsync/src/p4gitsync/services/initial_importer.py`

CommitBuilder와 동일한 패턴 적용:
- `_extract_files()`에서 LFS 대상 파일은 스트리밍 → `LfsObjectStore` 저장 → 포인터
- fast-import에는 포인터 텍스트만 전달 (현재와 동일)

### 5. ReverseCommitBuilder LFS 지원

**파일:** `p4gitsync/src/p4gitsync/services/reverse_commit_builder.py`

```python
def _resolve_lfs_content(self, path: str, content: bytes) -> bytes:
    """LFS 포인터인지 판별하고, 맞으면 실제 파일 내용을 반환."""
    if not content.startswith(b"version https://git-lfs.github.com/spec/v1"):
        return content
    oid = parse_lfs_pointer_oid(content)
    real_path = self._lfs_store.retrieve(oid)
    return real_path.read_bytes()
```

- `sync_commit()`에서 file_changes 순회 시 `_resolve_lfs_content()` 적용
- 역방향에서는 파일 크기가 크지 않다는 가정은 불가 → 대용량 파일은 청크 단위로 P4에 제출 (향후 개선 가능, 1차에서는 전체 로드)

### 6. Git Push + LFS Push 통합

**파일:** `p4gitsync/src/p4gitsync/git/git_push.py` (기존) 또는 해당 push 로직

push 시퀀스:
```python
# 1. 일반 git push
git push origin <branch>

# 2. LFS push (LFS 활성화 시)
git lfs push origin <branch>
```

- `git lfs push`는 `.git/lfs/objects/`에서 아직 remote에 없는 object를 전송
- LFS 활성화 상태에서만 실행
- push 실패 시 retry 로직은 기존 push 정책 따름

### 7. LFS 인증 설정

**파일:** `p4gitsync/src/p4gitsync/config/lfs_config.py`

```python
@dataclass
class LfsConfig:
    # 기존 필드...
    enabled: bool = False
    extensions: list[str] = ...
    server_type: str = "builtin"
    server_url: str = ""

    # 신규 필드
    auth_type: str = "git-credential"  # "git-credential" | "token" | "basic"
    auth_token: str = ""               # token 방식일 때
    auth_username: str = ""            # basic 방식일 때
    auth_password: str = ""            # basic 방식일 때
```

- `auth_type = "git-credential"`: 기본값. Git credential helper에 위임. 추가 설정 불필요.
- `auth_type = "token"`: `.lfsconfig`에 `[lfs] access = token` 설정 + 환경변수로 토큰 전달
- `auth_type = "basic"`: `.lfsconfig`에 URL에 username 포함, password는 credential helper에 저장

`generate_lfsconfig()`에서 auth 설정 반영.

### 8. SyncOrchestrator 통합

**파일:** `p4gitsync/src/p4gitsync/services/sync_orchestrator.py`

```python
# LfsObjectStore 생성
if self._config.lfs.enabled:
    self._lfs_store = LfsObjectStore(git_dir=self._git_operator.repo_path)
else:
    self._lfs_store = None

# CommitBuilder에 전달
self._commit_builder = CommitBuilder(
    ...,
    lfs_config=self._config.lfs if self._config.lfs.enabled else None,
    lfs_store=self._lfs_store,
)
```

MultiStreamHandler도 동일 패턴.

## config.toml 예시

```toml
[lfs]
enabled = true
extensions = [".uasset", ".umap", ".fbx", ".png", ".jpg", ".exr", ".tga", ".wav", ".mp3", ".mp4"]
lockable_extensions = [".uasset", ".umap"]
server_type = "builtin"          # "builtin" (Gitea/GitLab 내장) | "self-hosted"
server_url = ""                  # self-hosted일 때만
auth_type = "git-credential"     # "git-credential" | "token" | "basic"
auth_token = ""
auth_username = ""
auth_password = ""
```

## 파일 구조 변경

```
p4gitsync/src/p4gitsync/
  lfs/                          # 신규 패키지
    __init__.py
    lfs_object_store.py         # LfsObjectStore + LfsPointer
    lfs_pointer_utils.py        # 포인터 파싱 유틸 (parse_lfs_pointer_oid 등)
  config/
    lfs_config.py               # auth 필드 추가
  p4/
    p4_client.py                # print_file_stream() 추가
  services/
    commit_builder.py           # LfsObjectStore 통합
    initial_importer.py         # LfsObjectStore 통합
    reverse_commit_builder.py   # LFS 포인터 → 실제 파일 복원
    sync_orchestrator.py        # LfsObjectStore 생성/주입
    multi_stream_sync.py        # LfsObjectStore 주입
  git/
    (push 로직에 git lfs push 추가)
```

## Error Handling

- **LFS 저장 실패:** 임시 파일 cleanup 보장 (try/finally). 저장 실패 시 해당 changelist 처리 중단, 에러 로깅.
- **LFS retrieve 실패 (역방향):** OID가 로컬에 없으면 `FileNotFoundError`. 역방향 동기화 해당 commit 스킵 + 경고 로깅.
- **git lfs push 실패:** push retry 정책 따름. 3회 실패 시 알림 발송.
- **P4 스트리밍 중단:** OutputHandler에서 예외 시 임시 파일 삭제, changelist 재시도 대상에 추가.
- **디스크 공간 부족:** `store_from_stream()`에서 write 실패 시 임시 파일 삭제 후 예외 전파.

## Testing Strategy

### Unit Tests
- `LfsObjectStore`: store/retrieve/exists, atomic write, 중복 저장 idempotent, 빈 파일, 대용량 청크
- `LfsPointer`: 포인터 포맷 검증, 파싱 round-trip
- `P4Client.print_file_stream()`: mock handler로 청크 생성 검증
- `CommitBuilder`: LFS 대상/비대상 분기, LfsObjectStore 호출 검증
- `ReverseCommitBuilder`: LFS 포인터 감지 및 실제 파일 복원

### Integration Tests
- 전체 P4→Git 동기화에서 LFS 파일이 포인터로 저장되고 object가 로컬에 존재하는지
- `git lfs push` 후 remote에서 LFS object를 pull할 수 있는지
- 역방향: Git LFS 파일 변경 → P4에 실제 바이너리로 제출되는지
