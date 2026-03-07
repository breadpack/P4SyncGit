from __future__ import annotations

import logging

from p4gitsync.p4.p4_client import P4Client
from p4gitsync.services.sync_event import BranchCreateEvent, ChangelistEvent, SyncEvent
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.event_collector")


class EventCollector:
    """모든 등록된 stream에서 미동기화 이벤트를 수집하고 전역 정렬."""

    def __init__(
        self,
        p4_client: P4Client,
        state_store: StateStore,
        batch_size: int = 50,
    ) -> None:
        self._p4 = p4_client
        self._state = state_store
        self._batch_size = batch_size

    def collect(self) -> list[SyncEvent]:
        """등록된 모든 stream에서 이벤트를 수집하고 (cl, priority) 기준 정렬 반환."""
        events: list[SyncEvent] = []
        mappings = self._state.get_all_registered_streams()

        if not mappings:
            logger.debug("등록된 stream 없음")
            return events

        for mapping in mappings:
            stream = mapping.stream
            branch = mapping.branch

            if not self._state.is_stream_synced(stream) and mapping.parent_stream:
                events.append(
                    BranchCreateEvent(
                        cl=mapping.branch_point_cl or 0,
                        stream=stream,
                        parent_stream=mapping.parent_stream,
                        branch=branch,
                    )
                )

            last_cl = self._state.get_last_synced_cl(stream)
            changes = self._p4.get_changes_after(stream, last_cl)

            for cl in changes:
                events.append(ChangelistEvent(cl=cl, stream=stream, branch=branch))

        events.sort(key=lambda e: e.sort_key())

        total = len(events)
        if total > self._batch_size:
            events = events[: self._batch_size]

        if events:
            logger.info(
                "이벤트 수집 완료: 총 %d건 (배치 %d건), stream %d개",
                total, len(events), len(mappings),
            )

        return events
