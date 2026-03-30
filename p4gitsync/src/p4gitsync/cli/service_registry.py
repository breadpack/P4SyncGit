"""서비스 레지스트리 — services.json CRUD 관리."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _default_registry_path() -> Path:
    """플랫폼별 기본 레지스트리 파일 경로를 반환한다."""
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Local" / "p4gitsync"
    else:
        base = Path.home() / ".p4gitsync"
    return base / "services.json"


class ServiceRegistry:
    """JSON 파일 기반 서비스 등록/조회/삭제."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_registry_path()
        self._data: dict[str, dict] = {}
        self._load()

    # -- public API --

    def add(self, name: str, *, config: str, platform: str) -> None:
        """서비스를 등록한다. installed_at은 자동 기록."""
        self._data[name] = {
            "config": config,
            "platform": platform,
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get(self, name: str) -> dict | None:
        """이름으로 서비스를 조회한다. 없으면 None."""
        return self._data.get(name)

    def remove(self, name: str) -> None:
        """서비스를 삭제한다."""
        self._data.pop(name, None)
        self._save()

    def list_all(self) -> dict[str, dict]:
        """등록된 모든 서비스를 반환한다."""
        return dict(self._data)

    # -- persistence --

    def _load(self) -> None:
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
