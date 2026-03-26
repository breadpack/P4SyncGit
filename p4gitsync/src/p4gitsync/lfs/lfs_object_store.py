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
    """Git LFS object 저장소. .git/lfs/objects/ 하위에 content-addressed 파일 관리. 스레드 안전."""

    def __init__(self, git_dir: Path) -> None:
        self._objects_dir = Path(git_dir) / "lfs" / "objects"
        self._tmp_dir = Path(git_dir) / "lfs" / "tmp"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

    @property
    def tmp_dir(self) -> Path:
        """임시 파일 디렉토리. 외부 접근용."""
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
