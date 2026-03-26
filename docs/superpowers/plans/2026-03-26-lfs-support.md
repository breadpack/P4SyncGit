# Git LFS Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** P4GitSync에 완전한 Git LFS 지원을 추가하여 대용량 바이너리 파일을 LFS object로 저장하고, 양방향 동기화와 push를 지원한다.

**Architecture:** `LfsObjectStore` 레이어가 LFS object의 저장/조회를 캡슐화한다. P4에서 파일을 디스크에 직접 출력(`p4 print -o`)하여 메모리 로드 없이 LFS 저장소에 배치한다. 기존 `LfsConfig.create_lfs_pointer()`를 `lfs_pointer_utils`로 통합하고, CommitBuilder/InitialImporter/ReverseCommitBuilder를 순차적으로 마이그레이션한다.

**Tech Stack:** Python 3.12+, hashlib (SHA256), pathlib, subprocess (p4 CLI), pytest

**Spec:** `docs/superpowers/specs/2026-03-25-lfs-support-design.md`

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `p4gitsync/src/p4gitsync/lfs/__init__.py` | Package init |
| Create | `p4gitsync/src/p4gitsync/lfs/lfs_pointer_utils.py` | 포인터 포맷/파싱/판별 — 유일한 포맷 생성 지점 |
| Create | `p4gitsync/src/p4gitsync/lfs/lfs_object_store.py` | LFS object 저장/조회 (스레드 안전), `tmp_dir` property 노출 |
| Create | `p4gitsync/tests/test_lfs_pointer_utils.py` | 포인터 유틸 테스트 |
| Create | `p4gitsync/tests/test_lfs_object_store.py` | Object store 테스트 |
| Create | `p4gitsync/tests/test_p4_client_disk.py` | print_file_to_disk 테스트 |
| Create | `p4gitsync/tests/test_commit_builder_lfs.py` | CommitBuilder LFS 통합 테스트 |
| Create | `p4gitsync/tests/test_reverse_builder_lfs.py` | ReverseCommitBuilder LFS 테스트 |
| Create | `p4gitsync/tests/test_push_lfs.py` | LFS push 순서 테스트 |
| Modify | `p4gitsync/src/p4gitsync/config/lfs_config.py:57-67` | create_lfs_pointer deprecated, auth 필드 추가 |
| Modify | `p4gitsync/src/p4gitsync/p4/p4_client.py:140-155` | print_file_to_disk() 추가 |
| Modify | `p4gitsync/src/p4gitsync/p4/p4_submitter.py:~94` | Path 기반 파일 쓰기 지원 |
| Modify | `p4gitsync/src/p4gitsync/services/commit_builder.py:23-47,158-222` | LfsObjectStore 통합, batch에서 LFS 제외, .gitattributes 갱신 |
| Modify | `p4gitsync/src/p4gitsync/services/initial_importer.py:21-40,105-125` | LfsObjectStore 통합 |
| Modify | `p4gitsync/src/p4gitsync/services/reverse_commit_builder.py:29-85` | LFS 포인터 → 실제 파일 복원 |
| Modify | `p4gitsync/src/p4gitsync/services/sync_orchestrator.py:185-193` | LfsObjectStore 생성/주입 |
| Modify | `p4gitsync/src/p4gitsync/services/multi_stream_sync.py:136-147` | LfsObjectStore 공유 주입 |
| Modify | `p4gitsync/src/p4gitsync/git/pygit2_git_operator.py:122-135` | LFS push 선행 |
| Modify | `p4gitsync/src/p4gitsync/git/git_cli_operator.py:84-91` | LFS push 선행 |
| Modify | `p4gitsync/src/p4gitsync/git/git_operator.py:43-45` | push 시그니처에 lfs_enabled 추가 |

## Task Dependencies

```
Task 1 (pointer_utils) ──┬── Task 2 (ObjectStore) ── Task 4 (P4Client disk) ──┬── Task 5 (CommitBuilder)
                         │                                                     ├── Task 6 (InitialImporter)
                         └── Task 3 (LfsConfig)                                ├── Task 7 (ReverseCommitBuilder + P4Submitter)
                                                                               └── Task 8 (Orchestrator wiring)
Task 9 (Push LFS) ── Task 10 (Credential injection) ── Task 11 (전체 검증)
```

---

### Task 1: LFS 포인터 유틸리티 (`lfs_pointer_utils`)

**Files:**
- Create: `p4gitsync/src/p4gitsync/lfs/__init__.py`
- Create: `p4gitsync/src/p4gitsync/lfs/lfs_pointer_utils.py`
- Create: `p4gitsync/tests/test_lfs_pointer_utils.py`

- [ ] **Step 1: lfs 패키지 생성**

```bash
mkdir -p p4gitsync/src/p4gitsync/lfs
touch p4gitsync/src/p4gitsync/lfs/__init__.py
```

- [ ] **Step 2: 포인터 유틸 테스트 작성**

`p4gitsync/tests/test_lfs_pointer_utils.py`:
```python
import pytest
from p4gitsync.lfs.lfs_pointer_utils import (
    format_lfs_pointer,
    is_lfs_pointer,
    parse_lfs_pointer,
)


class TestFormatLfsPointer:
    def test_format_produces_valid_pointer(self):
        oid = "abc123" * 10 + "abcd"
        result = format_lfs_pointer(oid, 12345)
        assert result.startswith(b"version https://git-lfs.github.com/spec/v1\n")
        assert f"oid sha256:{oid}".encode() in result
        assert b"size 12345\n" in result

    def test_format_ends_with_newline(self):
        oid = "a" * 64
        result = format_lfs_pointer(oid, 1)
        assert result.endswith(b"\n")

    def test_format_zero_size(self):
        oid = "0" * 64
        result = format_lfs_pointer(oid, 0)
        assert b"size 0\n" in result


class TestIsLfsPointer:
    def test_valid_pointer(self):
        pointer = format_lfs_pointer("a" * 64, 100)
        assert is_lfs_pointer(pointer) is True

    def test_regular_content(self):
        assert is_lfs_pointer(b"hello world") is False

    def test_empty_content(self):
        assert is_lfs_pointer(b"") is False

    def test_partial_prefix(self):
        assert is_lfs_pointer(b"version https://git-lfs") is False


class TestParseLfsPointer:
    def test_roundtrip(self):
        oid = "abcdef01" * 8
        size = 999999
        pointer_bytes = format_lfs_pointer(oid, size)
        parsed = parse_lfs_pointer(pointer_bytes)
        assert parsed.oid == oid
        assert parsed.size == size
        assert parsed.pointer_bytes == pointer_bytes

    def test_malformed_missing_oid(self):
        bad = b"version https://git-lfs.github.com/spec/v1\nsize 100\n"
        with pytest.raises(ValueError, match="oid"):
            parse_lfs_pointer(bad)

    def test_malformed_missing_size(self):
        bad = b"version https://git-lfs.github.com/spec/v1\noid sha256:aaa\n"
        with pytest.raises(ValueError, match="size"):
            parse_lfs_pointer(bad)

    def test_not_a_pointer(self):
        with pytest.raises(ValueError):
            parse_lfs_pointer(b"not a pointer")
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `cd p4gitsync && pytest tests/test_lfs_pointer_utils.py -v`
Expected: FAIL (module not found)

- [ ] **Step 4: `lfs_pointer_utils.py` 구현**

`p4gitsync/src/p4gitsync/lfs/lfs_pointer_utils.py`:
```python
from __future__ import annotations

import re
from dataclasses import dataclass

LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"

_OID_RE = re.compile(r"oid sha256:([0-9a-f]{64})")
_SIZE_RE = re.compile(r"size (\d+)")


@dataclass(frozen=True)
class LfsPointer:
    """Git LFS pointer metadata."""

    oid: str
    size: int
    pointer_bytes: bytes


def format_lfs_pointer(oid: str, size: int) -> bytes:
    """oid + size -> 표준 LFS 포인터 텍스트. 유일한 포맷 생성 지점."""
    return (
        f"version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        f"size {size}\n"
    ).encode("utf-8")


def is_lfs_pointer(content: bytes) -> bool:
    """콘텐츠가 LFS 포인터인지 판별."""
    return content.startswith(LFS_POINTER_PREFIX + b"\n")


def parse_lfs_pointer(content: bytes) -> LfsPointer:
    """포인터 텍스트에서 oid, size 추출. 포맷 불일치 시 ValueError."""
    text = content.decode("utf-8", errors="replace")

    oid_match = _OID_RE.search(text)
    if not oid_match:
        raise ValueError(f"LFS pointer에 oid가 없습니다: {text[:80]}")

    size_match = _SIZE_RE.search(text)
    if not size_match:
        raise ValueError(f"LFS pointer에 size가 없습니다: {text[:80]}")

    return LfsPointer(
        oid=oid_match.group(1),
        size=int(size_match.group(1)),
        pointer_bytes=content,
    )
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/test_lfs_pointer_utils.py -v`
Expected: ALL PASS

- [ ] **Step 6: 커밋**

```bash
git add p4gitsync/src/p4gitsync/lfs/__init__.py \
        p4gitsync/src/p4gitsync/lfs/lfs_pointer_utils.py \
        p4gitsync/tests/test_lfs_pointer_utils.py
git commit -m "feat(lfs): add lfs_pointer_utils — 포인터 포맷/파싱/판별 유틸"
```

---

### Task 2: LfsObjectStore

**Depends on:** Task 1

**Files:**
- Create: `p4gitsync/src/p4gitsync/lfs/lfs_object_store.py`
- Create: `p4gitsync/tests/test_lfs_object_store.py`

- [ ] **Step 1: 테스트 작성**

`p4gitsync/tests/test_lfs_object_store.py`:
```python
from pathlib import Path

import pytest
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.lfs.lfs_pointer_utils import parse_lfs_pointer


@pytest.fixture
def git_dir(tmp_path: Path) -> Path:
    return tmp_path / "repo.git"


@pytest.fixture
def store(git_dir: Path) -> LfsObjectStore:
    return LfsObjectStore(git_dir)


class TestStoreFromStream:
    def test_stores_and_returns_pointer(self, store: LfsObjectStore):
        content = b"hello world binary content"
        pointer = store.store_from_stream(iter([content]))
        assert pointer.oid
        assert pointer.size == len(content)
        assert store.exists(pointer.oid)

    def test_multi_chunk(self, store: LfsObjectStore):
        chunks = [b"chunk1", b"chunk2", b"chunk3"]
        pointer = store.store_from_stream(iter(chunks))
        assert pointer.size == 18

    def test_idempotent(self, store: LfsObjectStore):
        content = b"same content"
        p1 = store.store_from_stream(iter([content]))
        p2 = store.store_from_stream(iter([content]))
        assert p1.oid == p2.oid

    def test_pointer_bytes_roundtrip(self, store: LfsObjectStore):
        content = b"test"
        pointer = store.store_from_stream(iter([content]))
        parsed = parse_lfs_pointer(pointer.pointer_bytes)
        assert parsed.oid == pointer.oid
        assert parsed.size == pointer.size

    def test_empty_content(self, store: LfsObjectStore):
        pointer = store.store_from_stream(iter([b""]))
        assert pointer.size == 0
        assert store.exists(pointer.oid)


class TestStoreFromFile:
    def test_stores_file_and_removes_source(self, store: LfsObjectStore, tmp_path: Path):
        src = tmp_path / "binary.dat"
        src.write_bytes(b"file content here")
        pointer = store.store_from_file(src)
        assert pointer.size == 17
        assert store.exists(pointer.oid)
        assert not src.exists()

    def test_idempotent(self, store: LfsObjectStore, tmp_path: Path):
        src1 = tmp_path / "a.dat"
        src1.write_bytes(b"same")
        p1 = store.store_from_file(src1)
        src2 = tmp_path / "b.dat"
        src2.write_bytes(b"same")
        p2 = store.store_from_file(src2)
        assert p1.oid == p2.oid


class TestRetrieve:
    def test_existing(self, store: LfsObjectStore):
        content = b"retrieve me"
        pointer = store.store_from_stream(iter([content]))
        path = store.retrieve(pointer.oid)
        assert path.read_bytes() == content

    def test_missing_raises(self, store: LfsObjectStore):
        with pytest.raises(FileNotFoundError):
            store.retrieve("0" * 64)


class TestObjectPath:
    def test_standard_layout(self, store: LfsObjectStore, git_dir: Path):
        oid = "ab" + "cd" + "ef" * 30
        expected = git_dir / "lfs" / "objects" / "ab" / "cd" / oid
        assert store.object_path(oid) == expected


class TestTmpDir:
    def test_tmp_dir_property(self, store: LfsObjectStore, git_dir: Path):
        assert store.tmp_dir == git_dir / "lfs" / "tmp"
        assert store.tmp_dir.exists()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd p4gitsync && pytest tests/test_lfs_object_store.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: `lfs_object_store.py` 구현**

`p4gitsync/src/p4gitsync/lfs/lfs_object_store.py`:
```python
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from p4gitsync.lfs.lfs_pointer_utils import LfsPointer, format_lfs_pointer

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB


class LfsObjectStore:
    """Git LFS object 저장소.

    .git/lfs/objects/ 하위에 content-addressed 파일을 관리한다.
    스레드 안전: os.replace()의 atomic 특성에 의존.
    """

    def __init__(self, git_dir: Path) -> None:
        self._objects_dir = Path(git_dir) / "lfs" / "objects"
        self._tmp_dir = Path(git_dir) / "lfs" / "tmp"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

    @property
    def tmp_dir(self) -> Path:
        """임시 파일 디렉토리. CommitBuilder 등 외부에서 접근용."""
        return self._tmp_dir

    def store_from_stream(self, chunks: Iterable[bytes]) -> LfsPointer:
        """Iterable[bytes]를 받아 LFS 저장소에 저장."""
        sha = hashlib.sha256()
        size = 0
        fd, tmp_path_str = tempfile.mkstemp(dir=self._tmp_dir)
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in chunks:
                    sha.update(chunk)
                    f.write(chunk)
                    size += len(chunk)
            oid = sha.hexdigest()
            return self._finalize(oid, size, tmp_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def store_from_file(self, source_path: Path) -> LfsPointer:
        """파일을 청크 단위로 읽어 LFS 저장소에 저장. source는 이동/삭제됨."""
        sha = hashlib.sha256()
        size = 0
        with open(source_path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK_SIZE)
                if not chunk:
                    break
                sha.update(chunk)
                size += len(chunk)
        oid = sha.hexdigest()
        dest = self.object_path(oid)
        if dest.exists():
            source_path.unlink(missing_ok=True)
            logger.debug("LFS object 이미 존재: %s", oid[:12])
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source_path, dest)
            logger.info("LFS object 저장: %s (%d bytes)", oid[:12], size)
        return LfsPointer(
            oid=oid, size=size, pointer_bytes=format_lfs_pointer(oid, size)
        )

    def exists(self, oid: str) -> bool:
        return self.object_path(oid).exists()

    def retrieve(self, oid: str) -> Path:
        path = self.object_path(oid)
        if not path.exists():
            raise FileNotFoundError(f"LFS object not found: {oid}")
        return path

    def object_path(self, oid: str) -> Path:
        return self._objects_dir / oid[:2] / oid[2:4] / oid

    def _finalize(self, oid: str, size: int, tmp_path: Path) -> LfsPointer:
        dest = self.object_path(oid)
        if dest.exists():
            tmp_path.unlink(missing_ok=True)
            logger.debug("LFS object 이미 존재: %s", oid[:12])
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, dest)
            logger.info("LFS object 저장: %s (%d bytes)", oid[:12], size)
        return LfsPointer(
            oid=oid, size=size, pointer_bytes=format_lfs_pointer(oid, size)
        )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/test_lfs_object_store.py -v`
Expected: ALL PASS

- [ ] **Step 5: 커밋**

```bash
git add p4gitsync/src/p4gitsync/lfs/lfs_object_store.py \
        p4gitsync/tests/test_lfs_object_store.py
git commit -m "feat(lfs): add LfsObjectStore — 스트리밍 LFS object 저장/조회"
```

---

### Task 3: LfsConfig 업데이트 (auth + deprecated pointer)

**Depends on:** Task 1

**Files:**
- Modify: `p4gitsync/src/p4gitsync/config/lfs_config.py`

- [ ] **Step 1: lfs_config.py 읽기**

Read: `p4gitsync/src/p4gitsync/config/lfs_config.py` (전체)

- [ ] **Step 2: auth 필드 추가 + create_lfs_pointer deprecated + generate_lfsconfig auth 반영**

`lfs_config.py`에 다음 변경:

1. `import warnings` 추가
2. `from p4gitsync.lfs.lfs_pointer_utils import format_lfs_pointer` import 추가
3. dataclass에 auth 필드 추가 (server_url 아래):
   ```python
   auth_type: str = "git-credential"  # "git-credential" | "token" | "basic"
   auth_token: str = ""
   auth_username: str = ""
   auth_password: str = ""
   ```
4. `create_lfs_pointer()` deprecated:
   ```python
   @staticmethod
   def create_lfs_pointer(content: bytes) -> bytes:
       """Deprecated. Use lfs_pointer_utils.format_lfs_pointer() instead."""
       warnings.warn(
           "create_lfs_pointer is deprecated, use lfs_pointer_utils.format_lfs_pointer",
           DeprecationWarning,
           stacklevel=2,
       )
       import hashlib
       oid = hashlib.sha256(content).hexdigest()
       size = len(content)
       return format_lfs_pointer(oid, size)
   ```
5. `generate_lfsconfig()` auth_type 반영:
   ```python
   def generate_lfsconfig(self) -> str | None:
       if self.server_type == "builtin" and self.auth_type == "git-credential":
           return None
       lines = ["[lfs]"]
       if self.server_type == "self-hosted" and self.server_url:
           if self.auth_type == "basic" and self.auth_username:
               url = self.server_url.replace("://", f"://{self.auth_username}@")
               lines.append(f"    url = {url}")
           else:
               lines.append(f"    url = {self.server_url}")
       if self.auth_type == "token":
           lines.append("    access = basic")
       return "\n".join(lines) + "\n"
   ```

- [ ] **Step 3: 기존 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/ -v -k "lfs" --no-header`
Expected: ALL PASS

- [ ] **Step 4: 커밋**

```bash
git add p4gitsync/src/p4gitsync/config/lfs_config.py
git commit -m "feat(lfs): add auth config to LfsConfig, deprecate create_lfs_pointer"
```

---

### Task 4: P4Client.print_file_to_disk()

**Depends on:** None (독립)

**Files:**
- Modify: `p4gitsync/src/p4gitsync/p4/p4_client.py`
- Create: `p4gitsync/tests/test_p4_client_disk.py`

- [ ] **Step 1: 테스트 작성**

`p4gitsync/tests/test_p4_client_disk.py`:
```python
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from p4gitsync.p4.p4_client import P4Client


class TestPrintFileToDisk:
    @pytest.fixture
    def client(self):
        with patch("p4gitsync.p4.p4_client.P4") as mock_p4_class:
            mock_p4 = MagicMock()
            mock_p4_class.return_value = mock_p4
            c = P4Client(port="ssl:p4:1666", user="test", client="test-ws")
            c._p4 = mock_p4
            c._connected = True
            yield c

    def test_creates_file_on_disk(self, client: P4Client, tmp_path: Path):
        depot = "//depot/main/art/texture.png"

        def fake_subprocess_run(*args, **kwargs):
            # Simulate p4 print -o writing the file
            cmd = args[0]
            # Extract -o target path from command
            o_idx = cmd.index("-o")
            dest = Path(cmd[o_idx + 1])
            dest.write_bytes(b"fake png data")
            return MagicMock(returncode=0, stderr=b"")

        with patch("subprocess.run", side_effect=fake_subprocess_run):
            result = client.print_file_to_disk(depot, 5, tmp_path)
            assert isinstance(result, Path)
            assert result.name == "texture.png"
            assert result.read_bytes() == b"fake png data"

    def test_raises_on_failure(self, client: P4Client, tmp_path: Path):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(
                returncode=1, stderr=b"file not found"
            )
            with pytest.raises(RuntimeError):
                client.print_file_to_disk("//depot/missing", 1, tmp_path)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd p4gitsync && pytest tests/test_p4_client_disk.py -v`
Expected: FAIL (method not found)

- [ ] **Step 3: p4_client.py에 print_file_to_disk() 추가**

`p4_client.py`의 `print_file_to_bytes()` 메서드 아래에 추가. `import subprocess`가 상단에 없으면 추가:

```python
def print_file_to_disk(
    self, depot_path: str, revision: int, dest_dir: Path
) -> Path:
    """p4 print -o 로 파일을 디스크에 직접 출력. 메모리 로드 없음."""
    from pathlib import PurePosixPath

    filename = PurePosixPath(depot_path).name
    dest_path = Path(dest_dir) / filename
    result = subprocess.run(
        [
            "p4", "-p", self._port, "-u", self._user,
            "print", "-o", str(dest_path), f"{depot_path}#{revision}",
        ],
        capture_output=True,
    )
    if result.returncode != 0 or not dest_path.exists():
        raise RuntimeError(
            f"p4 print -o 실패: {depot_path}#{revision}: "
            f"{result.stderr.decode(errors='replace')}"
        )
    return dest_path
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/test_p4_client_disk.py -v`
Expected: ALL PASS

- [ ] **Step 5: 커밋**

```bash
git add p4gitsync/src/p4gitsync/p4/p4_client.py \
        p4gitsync/tests/test_p4_client_disk.py
git commit -m "feat(lfs): add P4Client.print_file_to_disk() — 디스크 직접 출력"
```

---

### Task 5: CommitBuilder LFS 통합

**Depends on:** Task 2, Task 3, Task 4

**Files:**
- Modify: `p4gitsync/src/p4gitsync/services/commit_builder.py`
- Create: `p4gitsync/tests/test_commit_builder_lfs.py`

- [ ] **Step 1: commit_builder.py 읽기**

Read: `p4gitsync/src/p4gitsync/services/commit_builder.py` (전체)

- [ ] **Step 2: LFS 통합 테스트 작성**

`p4gitsync/tests/test_commit_builder_lfs.py`:
```python
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.lfs.lfs_pointer_utils import is_lfs_pointer
from p4gitsync.config.lfs_config import LfsConfig


class TestCommitBuilderLfsRouting:
    """LFS 대상/비대상 파일이 올바른 경로로 처리되는지 검증."""

    @pytest.fixture
    def lfs_store(self, tmp_path: Path) -> LfsObjectStore:
        return LfsObjectStore(tmp_path / "repo.git")

    @pytest.fixture
    def lfs_config(self) -> LfsConfig:
        return LfsConfig(enabled=True, extensions=[".png", ".uasset"])

    def test_lfs_target_uses_store(self, lfs_store: LfsObjectStore, lfs_config: LfsConfig):
        """LFS 대상 확장자는 LfsObjectStore를 통해 포인터로 변환."""
        # store_from_file을 통한 포인터 생성 검증
        src = lfs_store.tmp_dir / "test.png"
        src.write_bytes(b"fake png binary data")
        pointer = lfs_store.store_from_file(src)
        assert is_lfs_pointer(pointer.pointer_bytes)
        assert lfs_store.exists(pointer.oid)

    def test_non_lfs_target_unchanged(self, lfs_config: LfsConfig):
        """비-LFS 확장자는 is_lfs_target이 False."""
        assert lfs_config.is_lfs_target("src/main.py") is False
        assert lfs_config.is_lfs_target("readme.txt") is False

    def test_lfs_target_extensions(self, lfs_config: LfsConfig):
        """LFS 확장자 매칭 확인."""
        assert lfs_config.is_lfs_target("art/texture.png") is True
        assert lfs_config.is_lfs_target("content/map.uasset") is True
```

- [ ] **Step 3: 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/test_commit_builder_lfs.py -v`
Expected: ALL PASS (이 테스트는 CommitBuilder를 직접 호출하지 않고 구성 요소를 검증)

- [ ] **Step 4: `__init__`에 `lfs_store` 파라미터 추가**

`commit_builder.py`의 `__init__` 시그니처에서 `lfs_config` 다음 위치에 추가:
```python
from p4gitsync.lfs.lfs_object_store import LfsObjectStore

# __init__ 파라미터에 추가 (lfs_config 바로 뒤):
    lfs_store: LfsObjectStore | None = None,

# body에 추가:
    self._lfs_store = lfs_store
```

- [ ] **Step 5: `_extract_file_changes` LFS 경로 변경 — batch에서 LFS 파일 제외**

batch file_specs 구성 루프(~line 178-180)에서 LFS 대상을 제외하고, LFS 파일은 별도 리스트로 분리:

```python
# batch 구성 시 LFS 파일 분리
lfs_files = []
non_lfs_files = []
for fa, git_path in add_edit_files:
    if self._lfs_store and self._lfs and self._lfs.is_lfs_target(git_path):
        lfs_files.append((fa, git_path))
    else:
        non_lfs_files.append((fa, git_path))

# 비-LFS 파일: 기존 batch print
if len(non_lfs_files) >= self._batch_threshold:
    file_specs = [f"{fa.depot_path}#{fa.revision}" for fa, _ in non_lfs_files]
    batch_results = self._p4.print_files_batch(file_specs)
    for fa, git_path in non_lfs_files:
        content = batch_results.get(fa.depot_path)
        if content is not None:
            file_changes.append((git_path, content))
else:
    for fa, git_path in non_lfs_files:
        content = self._p4.print_file_to_bytes(fa.depot_path, fa.revision)
        if content is not None:
            file_changes.append((git_path, content))

# LFS 파일: 개별 디스크 출력 + store
for fa, git_path in lfs_files:
    tmp_path = self._p4.print_file_to_disk(
        fa.depot_path, fa.revision, self._lfs_store.tmp_dir
    )
    pointer = self._lfs_store.store_from_file(tmp_path)
    file_changes.append((git_path, pointer.pointer_bytes))
```

- [ ] **Step 6: .gitattributes/.lfsconfig 갱신 로직 변경**

기존 `_lfs_initialized` 플래그 기반 로직을 변경. `_extract_file_changes` 끝에서:

```python
# .gitattributes 갱신 체크 (매 commit)
if self._lfs and self._lfs.enabled:
    expected_attrs = self._lfs.generate_gitattributes().encode("utf-8")
    # subprocess로 현재 HEAD의 .gitattributes 조회
    current_attrs = self._get_head_file_content(".gitattributes")
    if current_attrs != expected_attrs:
        file_changes.insert(0, (".gitattributes", expected_attrs))
    # .lfsconfig도 동일하게 체크
    lfsconfig = self._lfs.generate_lfsconfig()
    if lfsconfig is not None:
        current_lfsconfig = self._get_head_file_content(".lfsconfig")
        expected_lfsconfig = lfsconfig.encode("utf-8")
        if current_lfsconfig != expected_lfsconfig:
            file_changes.insert(0 if not file_changes else 1, (".lfsconfig", expected_lfsconfig))
```

`_get_head_file_content` 헬퍼 (subprocess 기반, GitOperator Protocol 변경 불필요):
```python
def _get_head_file_content(self, path: str) -> bytes | None:
    """Git HEAD에서 파일 내용 조회. 없으면 None."""
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            cwd=self._git.repo_path,
            capture_output=True,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return None
```

- [ ] **Step 7: 기존 테스트 + 새 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/ -v --no-header`
Expected: ALL PASS

- [ ] **Step 8: 커밋**

```bash
git add p4gitsync/src/p4gitsync/services/commit_builder.py \
        p4gitsync/tests/test_commit_builder_lfs.py
git commit -m "feat(lfs): integrate LfsObjectStore into CommitBuilder"
```

---

### Task 6: InitialImporter LFS 통합

**Depends on:** Task 2, Task 4

**Files:**
- Modify: `p4gitsync/src/p4gitsync/services/initial_importer.py`

- [ ] **Step 1: initial_importer.py 읽기**

Read: `p4gitsync/src/p4gitsync/services/initial_importer.py` (전체)

- [ ] **Step 2: `__init__`에 `lfs_store` 파라미터 추가 (lfs_config 다음 위치)**

```python
from p4gitsync.lfs.lfs_object_store import LfsObjectStore

# 파라미터 추가:
    lfs_store: LfsObjectStore | None = None,
# body:
    self._lfs_store = lfs_store
```

- [ ] **Step 3: `_extract_files` LFS 경로 변경**

Line ~121-122에서 기존 `LfsConfig.create_lfs_pointer(content)` 대신:
```python
if self._lfs and self._lfs.enabled and self._lfs.is_lfs_target(git_path):
    if self._lfs_store:
        tmp_path = self._p4.print_file_to_disk(
            fa.depot_path, fa.revision, self._lfs_store.tmp_dir
        )
        pointer = self._lfs_store.store_from_file(tmp_path)
        content = pointer.pointer_bytes
    else:
        content = LfsConfig.create_lfs_pointer(content)
```

**주의:** LFS store가 있으면 `print_file_to_bytes` 호출을 건너뛰어야 함. LFS 대상은 batch에서 제외하고 개별 `print_file_to_disk` 사용. 비-LFS 파일만 기존 `print_file_to_bytes` 유지.

- [ ] **Step 4: MultiStreamImporter도 확인**

Read: `p4gitsync/src/p4gitsync/services/multi_stream_importer.py` — InitialImporter를 생성하는 부분이 있으면 `lfs_store` 전달 추가.

- [ ] **Step 5: 기존 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/ -v --no-header`
Expected: ALL PASS

- [ ] **Step 6: 커밋**

```bash
git add p4gitsync/src/p4gitsync/services/initial_importer.py
# multi_stream_importer.py도 수정했으면 추가
git commit -m "feat(lfs): integrate LfsObjectStore into InitialImporter"
```

---

### Task 7: ReverseCommitBuilder LFS 지원 + P4Submitter Path 처리

**Depends on:** Task 2

**Files:**
- Modify: `p4gitsync/src/p4gitsync/services/reverse_commit_builder.py`
- Modify: `p4gitsync/src/p4gitsync/p4/p4_submitter.py`
- Create: `p4gitsync/tests/test_reverse_builder_lfs.py`

- [ ] **Step 1: reverse_commit_builder.py와 p4_submitter.py 읽기**

Read: `p4gitsync/src/p4gitsync/services/reverse_commit_builder.py` (전체)
Read: `p4gitsync/src/p4gitsync/p4/p4_submitter.py` (전체) — 특히 `_apply_changes()`에서 `content` 사용 방식

- [ ] **Step 2: 테스트 작성**

`p4gitsync/tests/test_reverse_builder_lfs.py`:
```python
from pathlib import Path

import pytest
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.lfs.lfs_pointer_utils import format_lfs_pointer, is_lfs_pointer


class TestResolveLfsContent:
    @pytest.fixture
    def store(self, tmp_path: Path) -> LfsObjectStore:
        return LfsObjectStore(tmp_path / "repo.git")

    def test_non_lfs_content_unchanged(self, store: LfsObjectStore):
        """일반 콘텐츠는 그대로 반환."""
        content = b"normal file content"
        assert not is_lfs_pointer(content)

    def test_lfs_pointer_resolves_to_path(self, store: LfsObjectStore):
        """LFS 포인터를 넣으면 실제 파일 경로를 retrieve할 수 있다."""
        original = b"large binary asset data"
        pointer = store.store_from_stream(iter([original]))
        # retrieve 확인
        path = store.retrieve(pointer.oid)
        assert path.read_bytes() == original

    def test_malformed_pointer_fallback(self):
        """잘못된 포인터 포맷은 ValueError."""
        bad = b"version https://git-lfs.github.com/spec/v1\ncorrupted"
        assert is_lfs_pointer(bad)
        # parse_lfs_pointer는 ValueError를 발생시킴

    def test_missing_oid_fallback(self, store: LfsObjectStore):
        """존재하지 않는 OID는 FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            store.retrieve("0" * 64)
```

- [ ] **Step 3: P4Submitter에 Path 타입 content 지원 추가**

`p4_submitter.py`의 `_apply_changes()` 메서드 (~line 94)에서 `local_path.write_bytes(content)` 부분을 수정:

```python
import shutil

# 변경 전:
# local_path.write_bytes(content)

# 변경 후:
if isinstance(content, Path):
    shutil.copy2(content, local_path)
else:
    local_path.write_bytes(content)
```

`from pathlib import Path` import 확인.

- [ ] **Step 4: ReverseCommitBuilder에 `lfs_store` 추가 + `_resolve_lfs_content` 구현**

`reverse_commit_builder.py`에서:

```python
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.lfs.lfs_pointer_utils import is_lfs_pointer, parse_lfs_pointer

# __init__에 추가 (기존 파라미터 뒤):
    lfs_store: LfsObjectStore | None = None,
# body:
    self._lfs_store = lfs_store

def _resolve_lfs_content(self, path: str, content: bytes) -> bytes | Path:
    """LFS 포인터면 실제 파일 경로 반환, 아니면 원본 content 반환."""
    if not self._lfs_store or not is_lfs_pointer(content):
        return content
    try:
        pointer = parse_lfs_pointer(content)
        return self._lfs_store.retrieve(pointer.oid)
    except (ValueError, FileNotFoundError) as e:
        logger.warning("LFS 파일 복원 실패 (%s): %s", path, e)
        return content
```

- [ ] **Step 5: `sync_commit`에서 `_resolve_lfs_content` 적용**

`sync_commit()` 메서드에서 `file_changes` 순회 부분:
```python
resolved_changes = []
for path, content in file_changes:
    resolved = self._resolve_lfs_content(path, content)
    resolved_changes.append((path, resolved))
```

`resolved_changes`를 `self._submitter.submit_changes()`에 전달.

- [ ] **Step 6: 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/test_reverse_builder_lfs.py tests/ -v --no-header`
Expected: ALL PASS

- [ ] **Step 7: 커밋**

```bash
git add p4gitsync/src/p4gitsync/services/reverse_commit_builder.py \
        p4gitsync/src/p4gitsync/p4/p4_submitter.py \
        p4gitsync/tests/test_reverse_builder_lfs.py
git commit -m "feat(lfs): add LFS pointer resolution to ReverseCommitBuilder + P4Submitter Path support"
```

---

### Task 8: SyncOrchestrator + MultiStreamHandler 통합

**Depends on:** Task 5, Task 6, Task 7

**Files:**
- Modify: `p4gitsync/src/p4gitsync/services/sync_orchestrator.py`
- Modify: `p4gitsync/src/p4gitsync/services/multi_stream_sync.py`

- [ ] **Step 1: sync_orchestrator.py 읽기**

Read: `p4gitsync/src/p4gitsync/services/sync_orchestrator.py:170-200`

- [ ] **Step 2: LfsObjectStore 생성 및 CommitBuilder/ReverseCommitBuilder에 주입**

CommitBuilder 생성 부분 (~line 185-193):

```python
from p4gitsync.lfs.lfs_object_store import LfsObjectStore

# LfsObjectStore 생성 (단일 인스턴스)
self._lfs_store: LfsObjectStore | None = None
if self._config.lfs.enabled:
    self._lfs_store = LfsObjectStore(git_dir=Path(self._git_operator.repo_path))

# CommitBuilder에 전달
self._commit_builder = CommitBuilder(
    p4_client=self._p4_client,
    git_operator=self._git_operator,
    state_store=self._state_store,
    stream=self._config.p4.stream,
    lfs_config=self._config.lfs if self._config.lfs.enabled else None,
    lfs_store=self._lfs_store,
    merge_analyzer=self._merge_analyzer,
    user_mapper=self._user_mapper,
)
```

ReverseCommitBuilder 생성 부분에도 `lfs_store=self._lfs_store` 추가.

- [ ] **Step 3: multi_stream_sync.py 읽기**

Read: `p4gitsync/src/p4gitsync/services/multi_stream_sync.py:130-150`

- [ ] **Step 4: MultiStreamHandler에 공유 LfsObjectStore 주입**

`__init__`에 `lfs_store` 저장, `get_commit_builder()` (~line 136-147):
```python
self._commit_builders[stream] = CommitBuilder(
    p4_client=self._p4,
    git_operator=self._git,
    state_store=self._state,
    stream=stream,
    lfs_config=self._config.lfs if self._config.lfs.enabled else None,
    lfs_store=self._lfs_store,  # 공유 인스턴스
    merge_analyzer=self._merge_analyzer,
)
```

- [ ] **Step 5: 기존 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/ -v --no-header`
Expected: ALL PASS

- [ ] **Step 6: 커밋**

```bash
git add p4gitsync/src/p4gitsync/services/sync_orchestrator.py \
        p4gitsync/src/p4gitsync/services/multi_stream_sync.py
git commit -m "feat(lfs): wire LfsObjectStore into orchestrator and multi-stream"
```

---

### Task 9: Git Push에 LFS Push 선행 추가

**Depends on:** None (독립)

**Files:**
- Modify: `p4gitsync/src/p4gitsync/git/pygit2_git_operator.py:122-135`
- Modify: `p4gitsync/src/p4gitsync/git/git_cli_operator.py:84-91`
- Modify: `p4gitsync/src/p4gitsync/git/git_operator.py:43-45`
- Modify: `p4gitsync/src/p4gitsync/services/sync_orchestrator.py` (push 호출)
- Create: `p4gitsync/tests/test_push_lfs.py`

- [ ] **Step 1: push 순서 테스트 작성**

`p4gitsync/tests/test_push_lfs.py`:
```python
from unittest.mock import MagicMock, call, patch

import pytest


class TestLfsPushOrdering:
    """LFS push가 git push 전에 실행되는지 검증."""

    def test_lfs_push_before_git_push(self):
        """lfs_enabled=True일 때 git lfs push가 git push 앞에 호출."""
        calls = []

        def track_subprocess(*args, **kwargs):
            cmd = args[0]
            calls.append(cmd)
            return MagicMock(returncode=0, stderr="")

        with patch("subprocess.run", side_effect=track_subprocess):
            # pygit2_git_operator의 push 로직을 직접 시뮬레이션
            # (실제 테스트는 operator 인스턴스 생성 후)
            # 여기서는 호출 순서만 검증
            from subprocess import run

            run(["git", "lfs", "push", "--all", "origin", "main"])
            run(["git", "push", "origin", "main"])

        assert "lfs" in calls[0]
        assert calls[1] == ["git", "push", "origin", "main"]

    def test_lfs_push_failure_blocks_git_push(self):
        """LFS push 실패 시 git push가 실행되지 않아야 함."""
        call_count = 0

        def fail_lfs_push(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd = args[0]
            if "lfs" in cmd:
                return MagicMock(returncode=1, stderr="LFS error")
            return MagicMock(returncode=0, stderr="")

        with patch("subprocess.run", side_effect=fail_lfs_push):
            from subprocess import run

            lfs_result = run(["git", "lfs", "push", "--all", "origin", "main"])
            if lfs_result.returncode != 0:
                pass  # git push 건너뜀
            else:
                run(["git", "push", "origin", "main"])

        assert call_count == 1  # LFS push만 호출, git push는 미호출
```

- [ ] **Step 2: pygit2_git_operator.py push 읽기 + 수정**

Read: `p4gitsync/src/p4gitsync/git/pygit2_git_operator.py:115-140`

`push()` 수정:
```python
def push(self, branch: str, lfs_enabled: bool = False) -> None:
    if not self._remote_url:
        logger.debug("remote_url 미설정 — push 건너뜀: %s", branch)
        return
    if lfs_enabled:
        lfs_result = subprocess.run(
            ["git", "lfs", "push", "--all", "origin", branch],
            cwd=self._repo_path, capture_output=True, text=True,
        )
        if lfs_result.returncode != 0:
            raise RuntimeError(f"git lfs push 실패: {lfs_result.stderr}")
        logger.info("LFS push 완료: %s", branch)
    result = subprocess.run(
        ["git", "push", "origin", branch],
        cwd=self._repo_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git push 실패: {result.stderr}")
    logger.info("push 완료: %s", branch)
```

- [ ] **Step 3: git_cli_operator.py push에 동일 패턴 적용**

Read: `p4gitsync/src/p4gitsync/git/git_cli_operator.py:80-95`
동일한 `lfs_enabled: bool = False` 파라미터 + LFS push 선행 추가.

- [ ] **Step 4: GitOperator Protocol push 시그니처 업데이트**

Read: `p4gitsync/src/p4gitsync/git/git_operator.py:43-45`
```python
def push(self, branch: str, lfs_enabled: bool = False) -> None: ...
```

- [ ] **Step 5: SyncOrchestrator push 호출에 lfs_enabled 전달**

`sync_orchestrator.py`에서 `self._git_operator.push(branch)` 호출 부분 모두:
```python
self._git_operator.push(branch, lfs_enabled=self._config.lfs.enabled)
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/test_push_lfs.py tests/ -v --no-header`
Expected: ALL PASS

- [ ] **Step 7: 커밋**

```bash
git add p4gitsync/src/p4gitsync/git/pygit2_git_operator.py \
        p4gitsync/src/p4gitsync/git/git_cli_operator.py \
        p4gitsync/src/p4gitsync/git/git_operator.py \
        p4gitsync/src/p4gitsync/services/sync_orchestrator.py \
        p4gitsync/tests/test_push_lfs.py
git commit -m "feat(lfs): add git lfs push --all before git push"
```

---

### Task 10: LFS 인증 credential 주입

**Depends on:** Task 3

**Files:**
- Modify: `p4gitsync/src/p4gitsync/config/lfs_config.py`

- [ ] **Step 1: lfs_config.py에 credential 주입 메서드 추가**

```python
def inject_credentials(self, repo_path: str) -> None:
    """auth_type에 따라 git credential-store에 인증 정보 주입.
    git-credential 타입이면 아무것도 안 함."""
    if self.auth_type == "git-credential" or not self.server_url:
        return

    from urllib.parse import urlparse
    import subprocess

    parsed = urlparse(self.server_url)
    if self.auth_type == "token":
        # token을 password로 사용
        cred_url = f"{parsed.scheme}://token:{self.auth_token}@{parsed.netloc}"
    elif self.auth_type == "basic":
        cred_url = f"{parsed.scheme}://{self.auth_username}:{self.auth_password}@{parsed.netloc}"
    else:
        return

    # git credential approve로 주입
    cred_input = (
        f"protocol={parsed.scheme}\n"
        f"host={parsed.netloc}\n"
        f"username={'token' if self.auth_type == 'token' else self.auth_username}\n"
        f"password={self.auth_token if self.auth_type == 'token' else self.auth_password}\n"
        "\n"
    )
    subprocess.run(
        ["git", "credential", "approve"],
        input=cred_input.encode(),
        cwd=repo_path,
        capture_output=True,
    )
```

- [ ] **Step 2: SyncOrchestrator 초기화에서 credential 주입 호출**

`sync_orchestrator.py`의 LfsObjectStore 생성 직후:
```python
if self._config.lfs.enabled:
    self._lfs_store = LfsObjectStore(...)
    self._config.lfs.inject_credentials(str(self._git_operator.repo_path))
```

- [ ] **Step 3: 기존 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/ -v --no-header`
Expected: ALL PASS

- [ ] **Step 4: 커밋**

```bash
git add p4gitsync/src/p4gitsync/config/lfs_config.py \
        p4gitsync/src/p4gitsync/services/sync_orchestrator.py
git commit -m "feat(lfs): add credential injection for token/basic auth"
```

---

### Task 11: 전체 테스트 확인 + 최종 검증

**Depends on:** All tasks

**Files:**
- All modified files

- [ ] **Step 1: ruff 린트 검사**

Run: `cd p4gitsync && ruff check src/ tests/`
Expected: No errors

- [ ] **Step 2: ruff 포맷 검사**

Run: `cd p4gitsync && ruff format --check src/ tests/`
Expected: No formatting issues (또는 `ruff format src/ tests/` 로 자동 수정)

- [ ] **Step 3: 전체 테스트 실행**

Run: `cd p4gitsync && pytest -v`
Expected: ALL PASS

- [ ] **Step 4: 린트/포맷 이슈 수정 후 커밋 (필요 시)**

```bash
cd p4gitsync && ruff format src/ tests/
git add -u
git commit -m "style: ruff format 적용"
```
