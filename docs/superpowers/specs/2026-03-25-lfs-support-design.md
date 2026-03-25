# Git LFS Support Design

## Overview

P4GitSync에 Git LFS 지원을 추가하여 1TB 규모 P4 depot의 바이너리 에셋을 효율적으로 Git으로 동기화한다. 핵심은 `LfsObjectStore` 레이어를 도입하여 LFS object의 스트리밍 저장/조회를 캡슐화하는 것이다.

## Requirements

| 항목 | 결정 |
|------|------|
| LFS 저장소 | 로컬 bare repo `.git/lfs/objects/` + remote 내장 LFS |
| 메모리 관리 | P4에서 스트리밍 청크 처리 (OOM 방지) |
| 대상 판정 | 확장자 기반만 사용 (`LfsConfig.extensions`). `size_threshold_bytes`는 의도적으로 미사용 — 확장자 기반이 예측 가능하고 스트리밍 도중 판정 변경이 불필요. |
| 역방향 동기화 | LFS 포인터 감지 → 실제 파일 복원 후 P4 제출 |
| Push 타이밍 | `git lfs push --all` 먼저 실행 후 `git push` (LFS object 선행 보장) |
| 인증 | Git credential 기본, `config.toml`로 override 가능 |

## Architecture

### 데이터 흐름 (P4 → Git)

```
P4 Server
  ↓ p4 print -o <tmpfile> (CLI 기반, 디스크 직접 출력)
P4Client.print_file_to_disk(depot_path, revision) → Path
  ↓ 임시 파일 경로
  ├─ LFS 대상 → LfsObjectStore.store_from_file(tmp_path)
  │    ├─ 청크 단위 읽기 + SHA256 계산 (incremental)
  │    ├─ .git/lfs/objects/<oid[0:2]>/<oid[2:4]>/<oid> 에 atomic move
  │    └─ LfsPointer(oid, size, pointer_bytes) 반환
  │         ↓
  │    CommitBuilder → Git blob (pointer_bytes)
  │
  └─ 일반 파일 → 기존 흐름 (bytes → Git blob)
  ↓
GitOperator.create_commit()
  ↓
git lfs push --all origin <branch>  (LFS objects 먼저)
git push origin <branch>            (refs 이후)
```

### 데이터 흐름 (Git → P4, 역방향)

```
Git commit
  ↓
ReverseCommitBuilder.sync_commit()
  ↓ file_changes에서 LFS 포인터 감지
  ├─ LFS 포인터 → LfsObjectStore.retrieve(oid) → 실제 파일 경로
  │    ↓ 청크 단위로 읽어서 P4에 제출
  └─ 일반 파일 → 기존 흐름
```

## Components

### 1. LfsObjectStore (신규)

**파일:** `p4gitsync/src/p4gitsync/lfs/lfs_object_store.py`

LFS object의 저장/조회를 담당하는 단일 책임 클래스. 스레드 안전하게 설계하여 MultiStreamHandler에서 단일 인스턴스를 공유한다 (LFS object는 content-addressed이므로 OID가 같으면 동일 파일).

```python
@dataclass(frozen=True)
class LfsPointer:
    oid: str          # sha256 hex
    size: int         # 원본 파일 크기 (bytes)
    pointer_bytes: bytes  # Git blob에 저장할 포인터 텍스트

class LfsObjectStore:
    def __init__(self, git_dir: Path):
        """git_dir: bare repo 경로 또는 .git 디렉토리"""

    def store_from_file(self, source_path: Path) -> LfsPointer:
        """파일을 청크 단위로 읽으며 SHA256 계산 → atomic move로 LFS 저장소에 배치.
        이미 존재하면 source 삭제 후 기존 pointer 반환. 스레드 안전."""

    def store_from_stream(self, chunks: Iterable[bytes]) -> LfsPointer:
        """Iterable[bytes]를 받아 임시 파일에 쓰며 SHA256 계산 → atomic move.
        store_from_file의 스트리밍 변형."""

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
5. 동일 OID에 대한 동시 쓰기는 `os.replace()`의 atomic 특성으로 안전 — 마지막 writer가 승리하되 내용은 동일

**포인터 생성:** `LfsPointer`의 `pointer_bytes`는 `LfsObjectStore` 내에서 생성. 기존 `LfsConfig.create_lfs_pointer()`는 deprecated 처리하고, 새 코드는 `LfsObjectStore`의 반환값만 사용.

### 2. LFS 포인터 유틸리티 (신규)

**파일:** `p4gitsync/src/p4gitsync/lfs/lfs_pointer_utils.py`

```python
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"

def is_lfs_pointer(content: bytes) -> bool:
    """콘텐츠가 LFS 포인터인지 판별. 최초 40바이트만 검사."""

def parse_lfs_pointer(content: bytes) -> LfsPointer:
    """포인터 텍스트에서 oid, size 추출. 포맷 불일치 시 ValueError."""

def format_lfs_pointer(oid: str, size: int) -> bytes:
    """oid + size → 표준 포인터 텍스트 생성. 유일한 포인터 포맷 생성 지점."""
```

`LfsConfig.create_lfs_pointer()`와 `LfsObjectStore` 모두 이 모듈의 `format_lfs_pointer()`를 사용하여 포맷 중복을 제거한다.

### 3. P4Client 파일 출력 API (신규 메서드)

**파일:** `p4gitsync/src/p4gitsync/p4/p4_client.py`

```python
def print_file_to_disk(self, depot_path: str, revision: int,
                       dest_dir: Path) -> Path:
    """p4 print -o 로 파일을 디스크에 직접 출력. 메모리 로드 없음.
    반환: 출력된 파일의 경로."""
```

**구현 전략 (CLI 기반 — primary):**
- `p4 print -o <dest_dir>/<filename> <depot_path>#<revision>` CLI 실행
- p4python의 `OutputHandler`는 청크 전달이 보장되지 않으므로 CLI를 1차 전략으로 채택
- 파일이 디스크에 직접 쓰여지므로 메모리 사용량은 파일 크기와 무관
- 비-LFS 파일은 기존 `print_file_to_bytes()` / `print_files_batch()` 유지

**p4python OutputHandler (optional optimization):**
- 향후 p4python의 `OutputHandler`가 안정적으로 청크를 전달하는 것이 확인되면 대체 가능
- 현 시점에서는 CLI 기반이 검증된 안전한 접근

### 4. CommitBuilder 변경

**파일:** `p4gitsync/src/p4gitsync/services/commit_builder.py`

현재: P4에서 전체 바이트를 받아 `LfsConfig.create_lfs_pointer(content)` 호출
변경: LFS 대상 파일은 디스크 출력 → `LfsObjectStore`에 저장하고 포인터만 받기

```python
# 변경 전 (현재)
content = self._p4.print_file_to_bytes(fa.depot_path, fa.revision)
if self._lfs and self._lfs.enabled and self._lfs.is_lfs_target(git_path):
    content = LfsConfig.create_lfs_pointer(content)

# 변경 후
if self._lfs and self._lfs.enabled and self._lfs.is_lfs_target(git_path):
    tmp_path = self._p4.print_file_to_disk(fa.depot_path, fa.revision, self._lfs_tmp_dir)
    pointer = self._lfs_store.store_from_file(tmp_path)
    content = pointer.pointer_bytes
else:
    content = self._p4.print_file_to_bytes(fa.depot_path, fa.revision)
```

- `__init__`에 `lfs_store: LfsObjectStore | None` 파라미터 추가
- batch print 경로: LFS 대상 파일은 batch에서 제외, 개별 `print_file_to_disk()` 사용. 비-LFS 파일은 기존 batch 유지.
  - CL 내 LFS 파일이 많아도 개별 CLI 호출이므로 성능 트레이드오프 존재. 단, LFS 파일은 대용량이라 네트워크 I/O가 지배적이므로 호출 오버헤드는 무시 가능.

### 5. InitialImporter 변경

**파일:** `p4gitsync/src/p4gitsync/services/initial_importer.py`

CommitBuilder와 동일한 패턴 적용:
- `_extract_files()`에서 LFS 대상 파일은 `print_file_to_disk()` → `LfsObjectStore.store_from_file()` → 포인터
- fast-import에는 포인터 텍스트만 전달 (현재와 동일)

### 6. ReverseCommitBuilder LFS 지원

**파일:** `p4gitsync/src/p4gitsync/services/reverse_commit_builder.py`

```python
def _resolve_lfs_content(self, path: str, content: bytes) -> bytes | Path:
    """LFS 포인터인지 판별하고, 맞으면 실제 파일 경로를 반환."""
    if not is_lfs_pointer(content):
        return content
    pointer = parse_lfs_pointer(content)
    file_path = self._lfs_store.retrieve(pointer.oid)
    return file_path
```

- `sync_commit()`에서 file_changes 순회 시 `_resolve_lfs_content()` 적용
- 반환값이 `Path`이면 P4 submit 시 파일 경로 기반으로 처리 (메모리 로드 없이)
- P4Submitter에 파일 경로 기반 제출 메서드 추가 필요: `submit_file_from_path(p4_path, local_path)`
- 역방향 LFS 파일 크기 상한: 없음 (파일 경로 기반이므로 메모리 무관)

### 7. Git Push + LFS Push 통합

**파일:** `p4gitsync/src/p4gitsync/git/git_push.py` (기존) 또는 해당 push 로직

push 시퀀스 (LFS objects 선행):
```python
# 1. LFS push 먼저 (LFS 활성화 시)
git lfs push --all origin <branch>

# 2. 일반 git push
git push origin <branch>
```

**순서가 중요한 이유:** `git push` 후 `git lfs push`가 실패하면, remote에 LFS 포인터만 있고 object가 없는 상태가 된다. clone/fetch 시 smudge filter 에러 발생. LFS objects를 먼저 올려서 이 실패 창을 제거한다.

- `git lfs push --all`은 `.git/lfs/objects/`에서 아직 remote에 없는 모든 object를 전송
- LFS 활성화 상태에서만 실행
- LFS push 실패 시 git push도 중단 → retry 정책 따름

### 8. LFS 인증 설정

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

**auth_type별 `.lfsconfig` 출력:**

- `"git-credential"` (기본값):
  ```ini
  # .lfsconfig 생성 안 함 (builtin) 또는 url만 설정 (self-hosted)
  [lfs]
      url = https://lfs.example.com
  ```

- `"token"`:
  ```ini
  [lfs]
      url = https://lfs.example.com
      access = basic
  # 실행 시 GIT_LFS_SKIP_PUSH=0, Authorization header는 credential helper에서 제공
  # config.toml의 auth_token을 git credential-store에 주입하는 초기화 단계 추가
  ```

- `"basic"`:
  ```ini
  [lfs]
      url = https://{auth_username}@lfs.example.com
  # auth_password는 git credential-store에 저장 (초기화 시)
  ```

`generate_lfsconfig()`가 `auth_type`에 따라 적절한 포맷 생성. 민감 정보(token, password)는 `.lfsconfig`에 포함하지 않고, 초기화 시 `git credential-store`에 주입.

**기존 `LfsConfig.create_lfs_pointer()` 처리:**
- `@deprecated` 데코레이터 추가, docstring에 `lfs_pointer_utils.format_lfs_pointer()` 사용 안내
- 내부 구현은 `format_lfs_pointer()` 호출로 대체하여 포맷 중복 제거
- 향후 제거 예정

### 9. .gitattributes 갱신 전략

현재 구현은 첫 commit에서만 `.gitattributes`를 주입한다 (`_lfs_initialized` 플래그). 설정 변경 시 갱신되지 않는 문제가 있다.

**해결:** CommitBuilder 초기화 시 현재 Git HEAD의 `.gitattributes` 내용과 `LfsConfig.generate_gitattributes()` 결과를 비교. 차이가 있으면 다음 commit에 `.gitattributes` 업데이트를 file_changes에 포함.

```python
def _check_gitattributes_update(self) -> tuple[str, bytes] | None:
    """현재 .gitattributes와 config 기반 생성 결과 비교. 다르면 업데이트 반환."""
    current = self._git.get_file_content("HEAD", ".gitattributes")
    expected = self._lfs.generate_gitattributes().encode("utf-8")
    if current != expected:
        return (".gitattributes", expected)
    return None
```

### 10. SyncOrchestrator 통합

**파일:** `p4gitsync/src/p4gitsync/services/sync_orchestrator.py`

```python
# LfsObjectStore 생성 (단일 인스턴스, 모든 stream에서 공유)
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

**MultiStreamHandler:** 동일한 `LfsObjectStore` 인스턴스를 모든 stream의 CommitBuilder에 주입. LFS object는 content-addressed이므로 stream 간 동일 파일은 자동 중복 제거된다.

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
    lfs_pointer_utils.py        # 포인터 파싱/생성/판별 유틸
  config/
    lfs_config.py               # auth 필드 추가, create_lfs_pointer() deprecated
  p4/
    p4_client.py                # print_file_to_disk() 추가
  services/
    commit_builder.py           # LfsObjectStore 통합, .gitattributes 갱신
    initial_importer.py         # LfsObjectStore 통합
    reverse_commit_builder.py   # LFS 포인터 → 실제 파일 경로 복원
    sync_orchestrator.py        # LfsObjectStore 생성/주입
    multi_stream_sync.py        # LfsObjectStore 공유 주입
  git/
    (push 로직에 git lfs push --all 선행 추가)
```

## Error Handling

- **LFS 저장 실패:** 임시 파일 cleanup 보장 (try/finally). 저장 실패 시 해당 changelist 처리 중단, 에러 로깅.
- **LFS retrieve 실패 (역방향):** OID가 로컬에 없으면 `FileNotFoundError`. 역방향 동기화 해당 commit 스킵 + 경고 로깅.
- **git lfs push 실패:** LFS push 실패 시 git push도 중단. retry 정책 따름. 3회 실패 시 알림 발송.
- **P4 print_file_to_disk 실패:** CLI 에러 시 changelist 재시도 대상에 추가. 임시 파일 cleanup.
- **디스크 공간 부족:** `store_from_file()`에서 write/move 실패 시 임시 파일 삭제 후 예외 전파.
- **LFS 포인터 파싱 실패:** 역방향에서 malformed pointer 발견 시 `ValueError` → 해당 파일 스킵 + 경고 로깅.

## Testing Strategy

### Unit Tests
- `LfsObjectStore`: store_from_file/store_from_stream/retrieve/exists, atomic write, 중복 저장 idempotent, 빈 파일, 대용량 파일 청크 처리, 동시 쓰기 안전성
- `lfs_pointer_utils`: format/parse round-trip, is_lfs_pointer 판별, malformed pointer 에러
- `P4Client.print_file_to_disk()`: CLI 호출 검증, 출력 파일 존재 확인
- `CommitBuilder`: LFS 대상/비대상 분기, LfsObjectStore 호출 검증, .gitattributes 갱신
- `ReverseCommitBuilder`: LFS 포인터 감지, 파일 경로 반환, malformed pointer 처리

### Negative Tests
- 손상된 LFS 포인터 텍스트 → `ValueError`
- OID 해시 불일치 (파일 내용 ≠ 포인터 OID) → 감지 및 에러
- 존재하지 않는 OID retrieve → `FileNotFoundError`
- 디스크 공간 부족 시 store → 임시 파일 cleanup 확인
- `git lfs push` 실패 시 git push 미실행 확인

### Integration Tests
- 전체 P4→Git 동기화에서 LFS 파일이 포인터로 저장되고 object가 로컬에 존재하는지
- `git lfs push` + `git push` 후 remote에서 LFS object를 pull할 수 있는지
- 역방향: Git LFS 파일 변경 → P4에 실제 바이너리로 제출되는지
- LFS 설정 변경 후 `.gitattributes` 자동 갱신되는지
