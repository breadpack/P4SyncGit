import logging

from p4gitsync.p4.p4_client import P4Client
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.poller")


class ChangelistPoller:
    """P4 changelist 폴링."""

    def __init__(self, p4_client: P4Client, state_store: StateStore) -> None:
        self._p4 = p4_client
        self._state = state_store

    def poll(self, stream: str, batch_size: int = 50) -> list[int]:
        """마지막 동기화 CL 이후의 신규 CL 목록 조회 (batch_size 제한)."""
        last_cl = self._state.get_last_synced_cl(stream)
        changes = self._p4.get_changes_after(stream, last_cl)
        if changes:
            logger.info(
                "신규 CL %d건 발견 (after CL %d, batch %d)",
                len(changes), last_cl, min(len(changes), batch_size),
            )
        return changes[:batch_size]
