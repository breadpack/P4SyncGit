from __future__ import annotations

import logging

from p4gitsync.p4.p4_client import P4Client
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.poller")


class ChangelistPoller:
    """P4 changelist 폴링."""

    def __init__(self, p4_client: P4Client, state_store: StateStore) -> None:
        self._p4 = p4_client
        self._state = state_store

    def poll(self, stream: str, batch_size: int = 50, poll_stream: str | None = None) -> list[int]:
        """마지막 동기화 CL 이후의 신규 CL 목록 조회 (batch_size 제한).

        Args:
            stream: 상태 조회용 stream (virtual stream 이름).
            poll_stream: CL 조회용 stream. None이면 stream과 동일.
        """
        last_cl = self._state.get_last_synced_cl(stream)
        changes = self._p4.get_changes_after(poll_stream or stream, last_cl)
        if changes:
            remaining = len(changes)
            batch = min(remaining, batch_size)
            logger.info(
                "미동기화 CL %d건 (batch %d건 처리 예정, last CL %d)",
                remaining, batch, last_cl,
            )
        return changes[:batch_size]
