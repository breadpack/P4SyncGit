from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
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

    def cleanup_tmp(self, older_than_seconds: float = 0.0) -> int:
        """tmp 디렉토리의 잔여 임시 파일을 정리. 삭제한 파일 수 반환.

        크래시로 남은 부분 추출 파일(.lfs.tmp 등)이 디스크를 잠식하지 않도록
        import 시작/종료 시 호출한다. ``older_than_seconds=0`` 이면 전체 삭제.
        진행 중인 추출과 겹치지 않는 시점(워커 시작 전/종료 후)에만 호출할 것.
        """
        removed = 0
        now = time.time()
        try:
            entries = list(self._tmp_dir.iterdir())
        except OSError:
            return 0
        for p in entries:
            try:
                if not p.is_file():
                    continue
                if older_than_seconds > 0:
                    if now - p.stat().st_mtime < older_than_seconds:
                        continue
                p.unlink()
                removed += 1
            except OSError:
                continue
        if removed:
            logger.info("LFS tmp 정리: %d개 파일 삭제 (%s)", removed, self._tmp_dir)
        return removed

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
