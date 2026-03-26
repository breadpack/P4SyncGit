from __future__ import annotations

import logging

from p4gitsync.config.sync_config import AppConfig
from p4gitsync.git.git_operator import GitOperator
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.p4.merge_analyzer import MergeAnalyzer
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.services.commit_builder import CommitBuilder
from p4gitsync.services.event_collector import EventCollector
from p4gitsync.services.stream_watcher import StreamWatcher
from p4gitsync.services.sync_event import BranchCreateEvent, ChangelistEvent
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.multi_stream")


class MultiStreamHandler:
    """다중 stream 이벤트 수집, 분기점 매핑, CL 처리를 담당."""

    def __init__(
        self,
        config: AppConfig,
        p4_client: P4Client,
        git_operator: GitOperator,
        state_store: StateStore,
        merge_analyzer: MergeAnalyzer,
        event_collector: EventCollector,
        stream_watcher: StreamWatcher | None,
        lfs_store: LfsObjectStore | None = None,
    ) -> None:
        self._config = config
        self._p4 = p4_client
        self._git = git_operator
        self._state = state_store
        self._merge_analyzer = merge_analyzer
        self._event_collector = event_collector
        self._stream_watcher = stream_watcher
        self._lfs_store = lfs_store
        self._commit_builders: dict[str, CommitBuilder] = {}

    def poll_and_sync(
        self,
        notifier,
        circuit_breaker,
    ) -> None:
        """다중 stream에서 이벤트를 수집하여 전역 순서대로 처리."""
        if circuit_breaker and not circuit_breaker.allow_sync():
            logger.warning("Circuit breaker OPEN: 다중 stream 동기화 건너뜀")
            return

        events = self._event_collector.collect()
        if not events:
            return

        processed_branches: set[str] = set()

        for event in events:
            try:
                if isinstance(event, BranchCreateEvent):
                    self.handle_branch_create(event)
                    processed_branches.add(event.branch)
                elif isinstance(event, ChangelistEvent):
                    self.handle_changelist_event(event)
                    processed_branches.add(event.branch)
            except Exception as e:
                retry_count = self._state.record_sync_error(
                    event.cl, event.stream, str(e),
                )
                logger.error(
                    "이벤트 처리 실패 (CL %d, stream %s, retry=%d): %s",
                    event.cl, event.stream, retry_count, e,
                )
                if retry_count >= self._config.sync.error_retry_threshold:
                    notifier.send_error(event.cl, event.stream, str(e))
                break

        if not self._config.sync.push_after_every_commit and processed_branches:
            for branch in processed_branches:
                try:
                    self._git.push(branch, lfs_enabled=self._config.lfs.enabled)
                except Exception as e:
                    logger.error("일괄 push 실패 (branch %s): %s", branch, e)

    def handle_branch_create(self, event: BranchCreateEvent) -> None:
        """새 stream의 Git branch를 parent stream의 분기점에서 생성."""
        parent_sha = self._state.get_last_commit_before(
            event.parent_stream, event.cl + 1,
        )
        if parent_sha is None:
            parent_sha = self._state.get_commit_sha(
                self._state.get_last_synced_cl(event.parent_stream),
                event.parent_stream,
            )

        if parent_sha is None:
            raise RuntimeError(
                f"분기점 SHA를 찾을 수 없음: stream={event.stream}, "
                f"parent={event.parent_stream}, cl={event.cl}"
            )

        self._git.create_branch(event.branch, parent_sha)
        logger.info(
            "Branch 생성: %s (from %s at CL %d, sha=%s)",
            event.branch, event.parent_stream, event.cl, parent_sha[:8],
        )

    def handle_changelist_event(self, event: ChangelistEvent) -> None:
        """단일 CL 이벤트 처리: 해당 stream의 CommitBuilder로 커밋 생성."""
        commit_builder = self.get_commit_builder(event.stream)
        info = self._p4.describe(event.cl)

        last_cl = self._state.get_last_synced_cl(event.stream)
        parent_sha = (
            self._state.get_commit_sha(last_cl, event.stream)
            if last_cl > 0
            else None
        )

        sha = commit_builder.build_commit(info, event.branch, parent_sha)

        self._state.record_commit(
            event.cl, sha, event.stream, event.branch,
            has_integration=commit_builder.last_has_integration,
        )
        self._state.set_last_synced_cl(event.stream, event.cl, sha)

        self._git.maybe_run_gc(self._config.sync.git_gc_interval)

        if self._config.sync.push_after_every_commit:
            try:
                self._git.push(event.branch, lfs_enabled=self._config.lfs.enabled)
                self._state.update_push_status(event.cl, event.stream, "pushed")
            except Exception as e:
                self._state.update_push_status(event.cl, event.stream, "failed")
                logger.error("push 실패 CL %d: %s", event.cl, e)

    def get_commit_builder(self, stream: str) -> CommitBuilder:
        """stream별 CommitBuilder를 캐시하여 반환."""
        if stream not in self._commit_builders:
            self._commit_builders[stream] = CommitBuilder(
                p4_client=self._p4,
                git_operator=self._git,
                state_store=self._state,
                stream=stream,
                lfs_config=self._config.lfs if self._config.lfs.enabled else None,
                lfs_store=self._lfs_store,
                merge_analyzer=self._merge_analyzer,
            )
        return self._commit_builders[stream]

    def check_stream_changes(self, notifier) -> None:
        """StreamWatcher로 stream 생성/삭제를 감지하고 처리."""
        if self._stream_watcher is None:
            return

        try:
            changes = self._stream_watcher.detect_changes()
        except Exception:
            logger.exception("stream 변경 감지 실패")
            return

        for info in changes.created:
            try:
                self._stream_watcher.handle_created_stream(info)
                logger.info("새 stream 처리 완료: %s", info.stream)
                if notifier:
                    notifier.send_new_stream(info.stream)
            except Exception:
                logger.exception("새 stream 처리 실패: %s", info.stream)

        for info in changes.deleted:
            try:
                self._stream_watcher.handle_deleted_stream(info.stream)
            except Exception:
                logger.exception("삭제된 stream 처리 실패: %s", info.stream)

    @staticmethod
    def extract_depot(stream: str) -> str:
        """stream 경로에서 depot 경로 추출. //depot/main -> //depot"""
        parts = stream.strip("/").split("/")
        if len(parts) >= 2:
            return f"//{parts[0]}"
        return stream
