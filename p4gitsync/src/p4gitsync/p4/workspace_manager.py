from __future__ import annotations

import logging
import re

from p4gitsync.p4.p4_client import P4Client

logger = logging.getLogger("p4gitsync.workspace_manager")

_WORKSPACE_PREFIX = "p4gitsync"


class WorkspaceManager:
    """P4 workspace(client) 생성/관리.

    multi-stream 환경에서 stream별 workspace를 관리한다.
    현재는 p4 print 방식을 사용하므로 workspace sync가 필수는 아니지만,
    sync 방식 전환 시 활용된다.
    """

    def __init__(self, p4_client: P4Client, server_id: str = "") -> None:
        self._p4 = p4_client
        self._server_id = server_id or "default"
        self._workspace_cache: dict[str, str] = {}

    def get_or_create_workspace(self, stream: str) -> str:
        """stream 전용 workspace 이름 반환. 없으면 생성."""
        if stream in self._workspace_cache:
            return self._workspace_cache[stream]

        ws_name = self._make_workspace_name(stream)
        self._workspace_cache[stream] = ws_name
        logger.info("workspace 매핑: %s -> %s", stream, ws_name)
        return ws_name

    def sync_workspace(self, workspace: str, changelist: int) -> None:
        """workspace를 특정 CL로 sync. (sync 방식 사용 시)."""
        logger.info("workspace sync: %s @%d", workspace, changelist)
        self._p4.sync(changelist)

    def cleanup_inactive(self, active_streams: set[str]) -> list[str]:
        """비활성 stream의 workspace를 캐시에서 제거. 삭제된 workspace 이름 반환."""
        removed = []
        inactive = [s for s in self._workspace_cache if s not in active_streams]
        for stream in inactive:
            ws_name = self._workspace_cache.pop(stream)
            removed.append(ws_name)
            logger.info("비활성 workspace 정리: %s (%s)", ws_name, stream)
        return removed

    def _make_workspace_name(self, stream: str) -> str:
        """stream 경로를 workspace 이름으로 변환.

        //depot/main -> p4gitsync-default-depot-main
        """
        clean = stream.strip("/").replace("/", "-")
        clean = re.sub(r"[^a-zA-Z0-9_\-.]", "_", clean)
        return f"{_WORKSPACE_PREFIX}-{self._server_id}-{clean}"
