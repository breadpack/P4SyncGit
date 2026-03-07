"""State DB 자동 백업: sqlite3 backup API, 일 1회, 30일 보존."""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("p4gitsync.db_backup")


class DatabaseBackup:
    """SQLite DB 일 1회 백업, 30일 초과 자동 삭제."""

    def __init__(
        self,
        db_path: str,
        backup_dir: str | None = None,
        retention_days: int = 30,
    ) -> None:
        self._db_path = Path(db_path)
        self._backup_dir = Path(backup_dir) if backup_dir else self._db_path.parent / "backups"
        self._retention_days = retention_days
        self._last_backup_date: str | None = None

    def maybe_backup(self) -> bool:
        """오늘 백업이 아직 없으면 백업 수행. 수행 시 True 반환."""
        today = datetime.now().strftime("%Y%m%d")
        if self._last_backup_date == today:
            return False

        self._backup_dir.mkdir(parents=True, exist_ok=True)

        backup_file = self._backup_dir / f"state_{today}.db"
        if backup_file.exists():
            self._last_backup_date = today
            return False

        self._perform_backup(backup_file)
        self._last_backup_date = today
        self._cleanup_old_backups()
        return True

    def _perform_backup(self, backup_path: Path) -> None:
        """sqlite3.Connection.backup()으로 온라인 백업."""
        source = sqlite3.connect(str(self._db_path))
        dest = sqlite3.connect(str(backup_path))
        try:
            source.backup(dest)
            logger.info("DB 백업 완료: %s", backup_path)
        finally:
            dest.close()
            source.close()

    def _cleanup_old_backups(self) -> None:
        """보존 기간 초과 백업 파일 삭제."""
        cutoff = datetime.now() - timedelta(days=self._retention_days)
        removed = 0
        for f in self._backup_dir.glob("state_*.db"):
            try:
                date_str = f.stem.split("_", 1)[1]
                file_date = datetime.strptime(date_str, "%Y%m%d")
                if file_date < cutoff:
                    f.unlink()
                    removed += 1
            except (ValueError, IndexError):
                continue

        if removed > 0:
            logger.info("오래된 백업 %d건 삭제 (보존: %d일)", removed, self._retention_days)
