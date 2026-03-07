from __future__ import annotations

import logging
import threading
import time
from types import TracebackType

from p4gitsync.config.sync_config import AppConfig
from p4gitsync.git.git_operator import GitOperator
from p4gitsync.notifications.daily_report import DailyReporter
from p4gitsync.notifications.notifier import SlackNotifier
from p4gitsync.notifications.silence_detector import SilenceDetector
from p4gitsync.p4.merge_analyzer import MergeAnalyzer
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.services.changelist_poller import ChangelistPoller
from p4gitsync.services.circuit_breaker import IntegrityCircuitBreaker
from p4gitsync.services.commit_builder import CommitBuilder
from p4gitsync.services.db_backup import DatabaseBackup
from p4gitsync.services.event_collector import EventCollector
from p4gitsync.services.event_consumer import EventConsumer
from p4gitsync.services.integrity_checker import IntegrityChecker
from p4gitsync.services.multi_stream_sync import MultiStreamHandler
from p4gitsync.services.stream_watcher import StreamWatcher
from p4gitsync.services.sync_maintenance import SyncMaintenanceRunner
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.orchestrator")


class SyncOrchestrator:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._p4_client: P4Client | None = None
        self._git_operator: GitOperator | None = None
        self._state_store: StateStore | None = None
        self._poller: ChangelistPoller | None = None
        self._commit_builder: CommitBuilder | None = None
        self._merge_analyzer: MergeAnalyzer | None = None
        self._stream_watcher: StreamWatcher | None = None
        self._notifier: SlackNotifier | None = None
        self._silence_detector: SilenceDetector | None = None
        self._daily_reporter: DailyReporter | None = None
        self._db_backup: DatabaseBackup | None = None
        self._event_consumer: EventConsumer | None = None
        self._consumer_thread: threading.Thread | None = None
        self._integrity_checker: IntegrityChecker | None = None
        self._circuit_breaker: IntegrityCircuitBreaker | None = None
        self._multi_stream: MultiStreamHandler | None = None
        self._maintenance: SyncMaintenanceRunner | None = None
        self._unpushed_commits = 0
        self._last_push_time: float = 0.0
        self._running = False

    def __enter__(self) -> SyncOrchestrator:
        self._initialize_components()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()

    @property
    def state_store(self) -> StateStore | None:
        return self._state_store

    @property
    def event_consumer(self) -> EventConsumer | None:
        return self._event_consumer

    @property
    def circuit_breaker(self) -> IntegrityCircuitBreaker | None:
        return self._circuit_breaker

    def start(self) -> None:
        """서비스 시작: 초기화 -> 정합성 검증 -> 이벤트 루프.

        Redis가 활성화되면 EventConsumer를 별도 스레드로 실행하고,
        메인 스레드에서는 폴링 fallback 루프를 유지한다.
        """
        if self._state_store is None:
            self._initialize_components()
        self._verify_on_startup()
        self._running = True

        if self._config.redis.enabled:
            self._start_redis_consumer()

        if self._is_multi_stream():
            self._start_multi_stream()
        else:
            self._start_single_stream()

    def _is_multi_stream(self) -> bool:
        mappings = self._state_store.get_all_registered_streams()
        return len(mappings) > 1

    def _start_single_stream(self) -> None:
        logger.info("동기화 시작 (단일 stream): stream=%s", self._config.p4.stream)
        while self._running:
            try:
                self._maintenance.run()
                self._poll_and_sync()
            except Exception:
                logger.exception("폴링 루프 에러")
            time.sleep(self._config.sync.polling_interval_seconds)

    def _start_multi_stream(self) -> None:
        mappings = self._state_store.get_all_registered_streams()
        stream_names = [m.stream for m in mappings]
        logger.info("동기화 시작 (다중 stream): streams=%s", stream_names)

        while self._running:
            try:
                self._maintenance.run()
                self._multi_stream.check_stream_changes(self._notifier)
                self._multi_stream.poll_and_sync(
                    self._notifier, self._circuit_breaker,
                )
            except Exception:
                logger.exception("다중 stream 폴링 루프 에러")
            time.sleep(self._config.sync.polling_interval_seconds)

    def stop(self) -> None:
        self._running = False
        if self._event_consumer:
            self._event_consumer.stop()
        self._cleanup_components()

    def _cleanup_components(self) -> None:
        if self._p4_client:
            try:
                self._p4_client.disconnect()
            except Exception:
                logger.exception("P4 클라이언트 종료 실패")
        if self._state_store:
            try:
                self._state_store.close()
            except Exception:
                logger.exception("StateStore 종료 실패")

    def _initialize_components(self) -> None:
        try:
            self._state_store = StateStore(self._config.state.db_path)
            self._state_store.initialize()

            self._p4_client = P4Client(
                port=self._config.p4.port,
                user=self._config.p4.user,
                workspace=self._config.p4.workspace,
            )
            self._p4_client.connect()

            self._git_operator = self._create_git_operator()
            self._git_operator.init_repo()

            self._poller = ChangelistPoller(self._p4_client, self._state_store)
            self._merge_analyzer = MergeAnalyzer(self._p4_client)
            self._commit_builder = CommitBuilder(
                p4_client=self._p4_client,
                git_operator=self._git_operator,
                state_store=self._state_store,
                stream=self._config.p4.stream,
                lfs_config=self._config.lfs if self._config.lfs.enabled else None,
                merge_analyzer=self._merge_analyzer,
            )

            event_collector = EventCollector(
                p4_client=self._p4_client,
                state_store=self._state_store,
                batch_size=self._config.sync.batch_size,
            )

            depot = MultiStreamHandler.extract_depot(self._config.p4.stream)
            self._stream_watcher = StreamWatcher(
                p4_client=self._p4_client,
                state_store=self._state_store,
                git_operator=self._git_operator,
                depot=depot,
                policy=self._config.stream_policy,
            )

            slack = self._config.slack
            self._notifier = SlackNotifier(
                webhook_url=slack.webhook_url,
                channel=slack.channel,
                alerts_webhook_url=slack.alerts_webhook_url,
                warnings_webhook_url=slack.warnings_webhook_url,
                info_webhook_url=slack.info_webhook_url,
            )
            self._silence_detector = SilenceDetector(
                threshold_minutes=slack.silence_threshold_minutes,
            )
            self._daily_reporter = DailyReporter(
                report_hour=slack.daily_report_hour,
            )

            self._db_backup = DatabaseBackup(self._config.state.db_path)

            if self._config.redis.enabled:
                self._event_consumer = EventConsumer(
                    redis_config=self._config.redis,
                    on_changelist=self._on_redis_changelist,
                    fallback_poll=self._poll_and_sync,
                )
                self._event_consumer.connect()

            self._integrity_checker = IntegrityChecker(
                p4_client=self._p4_client,
                repo_path=self._config.git.repo_path,
                stream=self._config.p4.stream,
            )
            self._circuit_breaker = IntegrityCircuitBreaker(
                integrity_checker=self._integrity_checker,
                notifier=self._notifier,
            )

            self._multi_stream = MultiStreamHandler(
                config=self._config,
                p4_client=self._p4_client,
                git_operator=self._git_operator,
                state_store=self._state_store,
                merge_analyzer=self._merge_analyzer,
                event_collector=event_collector,
                stream_watcher=self._stream_watcher,
            )

            self._maintenance = SyncMaintenanceRunner(
                config=self._config,
                state_store=self._state_store,
                p4_client=self._p4_client,
                db_backup=self._db_backup,
                circuit_breaker=self._circuit_breaker,
                notifier=self._notifier,
                silence_detector=self._silence_detector,
                daily_reporter=self._daily_reporter,
            )
        except Exception:
            self._cleanup_components()
            raise

    def _create_git_operator(self) -> GitOperator:
        backend = self._config.git.backend
        repo_path = self._config.git.repo_path
        remote_url = self._config.git.remote_url

        if backend == "cli":
            from p4gitsync.git.git_cli_operator import GitCliOperator
            return GitCliOperator(repo_path=repo_path, remote_url=remote_url)

        from p4gitsync.git.pygit2_git_operator import Pygit2GitOperator
        return Pygit2GitOperator(repo_path=repo_path, remote_url=remote_url)

    def _verify_on_startup(self) -> None:
        branch = self._config.git.default_branch
        head_sha = self._git_operator.get_head_sha(branch)

        if head_sha is None:
            logger.info("Git HEAD 없음 (초기 상태)")
            return

        if not self._state_store.verify_consistency(branch, head_sha):
            logger.error(
                "정합성 불일치! Git HEAD=%s vs StateStore. 수동 확인 필요.",
                head_sha,
            )
            if self._notifier:
                self._notifier.send_integrity_failure(
                    branch, f"Git HEAD={head_sha} vs StateStore 불일치"
                )
            raise RuntimeError("Git-StateStore 정합성 불일치. 수동 확인 후 재시작하세요.")

        pending = self._state_store.get_pending_pushes()
        if pending:
            logger.info("미완료 push %d건 발견. 재시도 진행.", len(pending))
            for item in pending:
                try:
                    self._git_operator.push(item["branch"])
                    self._state_store.update_push_status(
                        item["changelist"], item["stream"], "pushed"
                    )
                except Exception as e:
                    logger.error(
                        "push 재시도 실패: CL %d, %s", item["changelist"], e
                    )

    def _poll_and_sync(self) -> None:
        if self._circuit_breaker and not self._circuit_breaker.allow_sync():
            logger.warning("Circuit breaker OPEN: 동기화 건너뜀")
            return

        stream = self._config.p4.stream
        branch = self._config.git.default_branch
        batch_size = self._config.sync.batch_size

        changes = self._poller.poll(stream, batch_size)
        if not changes:
            return

        for cl in changes:
            try:
                self._process_changelist(cl, stream, branch)
            except Exception as e:
                retry_count = self._state_store.record_sync_error(cl, stream, str(e))
                logger.error("CL %d 처리 실패 (retry=%d): %s", cl, retry_count, e)
                if retry_count >= self._config.sync.error_retry_threshold:
                    self._notifier.send_error(cl, stream, str(e))
                break

        if not self._config.sync.push_after_every_commit:
            self._unpushed_commits += len(changes)
            if self._should_batch_push():
                try:
                    self._git_operator.push(branch)
                    self._mark_batch_pushed(changes, stream)
                    self._unpushed_commits = 0
                    self._last_push_time = time.monotonic()
                except Exception as e:
                    logger.error("일괄 push 실패: %s", e)

    def _should_batch_push(self) -> bool:
        sync = self._config.sync
        if self._unpushed_commits >= sync.push_batch_size:
            return True
        if self._last_push_time == 0.0:
            self._last_push_time = time.monotonic()
            return False
        elapsed = time.monotonic() - self._last_push_time
        return elapsed >= sync.push_interval_seconds and self._unpushed_commits > 0

    def _process_changelist(self, cl: int, stream: str, branch: str) -> None:
        info = self._p4_client.describe(cl)

        last_cl = self._state_store.get_last_synced_cl(stream)
        parent_sha = self._state_store.get_commit_sha(last_cl, stream) if last_cl > 0 else None

        sha = self._commit_builder.build_commit(info, branch, parent_sha)

        self._state_store.record_commit(
            cl, sha, stream, branch,
            has_integration=self._commit_builder.last_has_integration,
        )
        self._state_store.set_last_synced_cl(stream, cl, sha)

        if self._silence_detector:
            self._silence_detector.record_sync()
        if self._daily_reporter:
            self._daily_reporter.stats.record_sync(stream, 0)

        self._git_operator.maybe_run_gc(self._config.sync.git_gc_interval)

        if self._config.sync.push_after_every_commit:
            try:
                self._git_operator.push(branch)
                self._state_store.update_push_status(cl, stream, "pushed")
            except Exception as e:
                self._state_store.update_push_status(cl, stream, "failed")
                logger.error("push 실패 CL %d: %s", cl, e)

    def _mark_batch_pushed(self, changelists: list[int], stream: str) -> None:
        for cl in changelists:
            try:
                self._state_store.update_push_status(cl, stream, "pushed")
            except Exception:
                logger.exception("push 상태 업데이트 실패: CL %d", cl)

    def _start_redis_consumer(self) -> None:
        if self._event_consumer is None:
            return
        self._consumer_thread = threading.Thread(
            target=self._event_consumer.consume, daemon=True, name="redis-consumer",
        )
        self._consumer_thread.start()
        logger.info("Redis EventConsumer 스레드 시작")

    def _on_redis_changelist(self, changelist: int, user: str, stream: str = "") -> None:
        if not stream:
            stream = self._config.p4.stream

        mapping = self._state_store.get_stream_mapping(stream)
        branch = mapping.branch if mapping else self._config.git.default_branch

        existing_sha = self._state_store.get_commit_sha(changelist, stream)
        if existing_sha is not None:
            logger.debug("중복 CL 건너뛰기: CL %d (sha=%s)", changelist, existing_sha[:8])
            return

        try:
            if self._is_multi_stream() and mapping:
                commit_builder = self._multi_stream.get_commit_builder(stream)
                info = self._p4_client.describe(changelist)
                last_cl = self._state_store.get_last_synced_cl(stream)
                parent_sha = (
                    self._state_store.get_commit_sha(last_cl, stream)
                    if last_cl > 0
                    else None
                )
                sha = commit_builder.build_commit(info, branch, parent_sha)
                self._state_store.record_commit(
                    changelist, sha, stream, branch,
                    has_integration=commit_builder.last_has_integration,
                )
                self._state_store.set_last_synced_cl(stream, changelist, sha)
            else:
                self._process_changelist(changelist, stream, branch)
            logger.info("Redis 이벤트 처리 완료: CL %d, user=%s, stream=%s", changelist, user, stream)
        except Exception as e:
            retry_count = self._state_store.record_sync_error(changelist, stream, str(e))
            logger.error("Redis CL %d 처리 실패 (retry=%d): %s", changelist, retry_count, e)
            if retry_count >= self._config.sync.error_retry_threshold:
                self._notifier.send_error(changelist, stream, str(e))
