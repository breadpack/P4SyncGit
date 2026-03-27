from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import TracebackType

from p4gitsync.config.sync_config import AppConfig
from p4gitsync.git.commit_metadata import parse_git_commit_from_description
from p4gitsync.git.git_change_detector import GitChangeDetector
from p4gitsync.git.git_operator import GitOperator
from p4gitsync.notifications.daily_report import DailyReporter
from p4gitsync.notifications.notifier import SlackNotifier
from p4gitsync.notifications.silence_detector import SilenceDetector
from p4gitsync.p4.merge_analyzer import MergeAnalyzer
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.p4.p4_submitter import P4Submitter
from p4gitsync.p4.virtual_stream_filter import VirtualStreamFilter
from p4gitsync.services.changelist_poller import ChangelistPoller
from p4gitsync.services.circuit_breaker import IntegrityCircuitBreaker
from p4gitsync.services.commit_builder import CommitBuilder
from p4gitsync.services.conflict_detector import ConflictDetector
from p4gitsync.services.db_backup import DatabaseBackup
from p4gitsync.services.event_collector import EventCollector
from p4gitsync.services.event_consumer import EventConsumer
from p4gitsync.services.integrity_checker import IntegrityChecker
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.services.multi_stream_sync import MultiStreamHandler
from p4gitsync.services.reverse_commit_builder import ReverseCommitBuilder
from p4gitsync.services.stream_watcher import StreamWatcher
from p4gitsync.services.sync_maintenance import SyncMaintenanceRunner
from p4gitsync.services.user_mapper import UserMapper
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
        self._git_change_detector: GitChangeDetector | None = None
        self._p4_submitter: P4Submitter | None = None
        self._reverse_builders: dict[str, ReverseCommitBuilder] = {}
        self._conflict_detector: ConflictDetector | None = None
        self._user_mapper: UserMapper | None = None
        self._lfs_store: LfsObjectStore | None = None
        self._poll_stream: str | None = None  # virtual stream이면 parent stream
        self._virtual_filter = None
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
        """서비스 시작: 초기화 -> 초기 import -> 정합성 검증 -> 이벤트 루프.

        Redis가 활성화되면 EventConsumer를 별도 스레드로 실행하고,
        메인 스레드에서는 폴링 fallback 루프를 유지한다.
        """
        if self._state_store is None:
            self._initialize_components()
        self._run_initial_import_if_needed()
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
        stream = self._config.p4.stream
        branch = self._config.git.default_branch
        logger.info("동기화 시작 (단일 stream): stream=%s", stream)
        while self._running:
            try:
                self._maintenance.run()
                direction = self._get_stream_direction(stream)

                # bidirectional이면 forward sync 전에 충돌 감지 수행
                if direction == "bidirectional":
                    has_conflict = self._check_and_handle_conflicts(stream, branch)
                    if not has_conflict:
                        self._poll_and_sync()
                        self._poll_reverse_sync(stream, branch)
                elif direction == "git_to_p4":
                    self._poll_reverse_sync(stream, branch)
                else:
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

    def _ensure_p4_workspaces(self) -> None:
        """config에 지정된 P4 workspace가 없으면 자동 생성."""
        p4ws_base = Path(self._config.git.repo_path) / ".p4ws"
        stream = self._config.p4.stream

        read_ws = self._config.p4.workspace
        read_root = str(p4ws_base / read_ws)
        Path(read_root).mkdir(parents=True, exist_ok=True)
        self._p4_client.ensure_workspace(read_ws, stream, read_root)

        submit_ws = self._config.p4.submit_workspace
        if submit_ws and submit_ws != read_ws:
            submit_root = str(p4ws_base / submit_ws)
            Path(submit_root).mkdir(parents=True, exist_ok=True)
            self._p4_client.ensure_workspace(submit_ws, stream, submit_root)

    def _resolve_virtual_stream(self) -> None:
        """virtual stream이면 parent stream과 exclude 필터를 설정."""
        stream = self._config.p4.stream
        parent, excludes = self._p4_client.resolve_virtual_stream(stream)
        if parent != stream:
            self._poll_stream = parent
            self._virtual_filter = VirtualStreamFilter(parent, excludes)
        else:
            self._poll_stream = None
            self._virtual_filter = None

    def _run_initial_import_if_needed(self) -> None:
        """미동기화 CL이 대량 존재하면 fast-import로 일괄 처리."""
        branch = self._config.git.default_branch
        stream = self._config.p4.stream
        poll_stream = self._poll_stream or stream

        last_cl = self._state_store.get_last_synced_cl(stream)
        remaining = self._p4_client.get_changes_after(poll_stream, last_cl)
        fast_import_threshold = max(self._config.sync.batch_size * 10, 500)
        if len(remaining) <= fast_import_threshold:
            return  # 소량이면 폴링 루프에서 처리

        logger.info(
            "대량 미동기화 감지: %d건 CL, fast-import로 일괄 처리 시작", len(remaining),
        )

        from p4gitsync.services.initial_importer import InitialImporter

        importer = InitialImporter(
            p4_client=self._p4_client,
            state_store=self._state_store,
            repo_path=self._config.git.repo_path,
            stream=stream,
            config=self._config.initial_import,
            lfs_config=self._config.lfs if self._config.lfs.enabled else None,
            lfs_store=self._lfs_store,
            virtual_filter=self._virtual_filter,
            p4_config=self._config.p4,
        )
        importer.run(branch)
        logger.info("초기 import 완료: stream=%s", stream)

    def _initialize_components(self) -> None:
        try:
            self._state_store = StateStore(self._config.state.db_path)
            self._state_store.initialize()

            self._p4_client = self._config.p4.create_client()
            self._p4_client.connect()

            self._ensure_p4_workspaces()
            self._resolve_virtual_stream()

            self._git_operator = self._create_git_operator()
            self._git_operator.init_repo()

            self._poller = ChangelistPoller(self._p4_client, self._state_store)
            self._merge_analyzer = MergeAnalyzer(self._p4_client)

            self._user_mapper = UserMapper(
                config=self._config.user_mapping,
                state_store=self._state_store,
            )

            if self._config.lfs.enabled:
                git_dir = Path(self._config.git.repo_path) / ".git"
                self._lfs_store = LfsObjectStore(git_dir=git_dir)
                self._config.lfs.inject_credentials(self._config.git.repo_path)

            self._commit_builder = CommitBuilder(
                p4_client=self._p4_client,
                git_operator=self._git_operator,
                state_store=self._state_store,
                stream=self._config.p4.stream,
                lfs_config=self._config.lfs if self._config.lfs.enabled else None,
                lfs_store=self._lfs_store,
                merge_analyzer=self._merge_analyzer,
                user_mapper=self._user_mapper,
                virtual_filter=self._virtual_filter,
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
                lfs_store=self._lfs_store,
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

            self._initialize_bidirectional()
        except Exception:
            self._cleanup_components()
            raise

    def _create_git_operator(self) -> GitOperator:
        backend = self._config.git.backend
        repo_path = self._config.git.repo_path
        remote_url = self._config.git.remote_url
        bare = self._config.git.bare

        if backend == "cli":
            from p4gitsync.git.git_cli_operator import GitCliOperator
            return GitCliOperator(repo_path=repo_path, remote_url=remote_url, bare=bare)

        from p4gitsync.git.pygit2_git_operator import Pygit2GitOperator
        return Pygit2GitOperator(repo_path=repo_path, remote_url=remote_url, bare=bare)

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
                    self._git_operator.push(item["branch"], lfs_enabled=self._config.lfs.enabled)
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

        changes = self._poller.poll(stream, batch_size, poll_stream=self._poll_stream)
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
                    self._git_operator.push(branch, lfs_enabled=self._config.lfs.enabled)
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

        # Git에서 역방향으로 submit된 CL이면 스킵
        if parse_git_commit_from_description(info.description):
            logger.debug("GitCommit 마커 발견, 스킵: CL %d", cl)
            self._state_store.set_last_synced_cl(stream, cl, "")
            return

        last_cl = self._state_store.get_last_synced_cl(stream)
        parent_sha = self._state_store.get_commit_sha(last_cl, stream) if last_cl > 0 else None

        sha = self._commit_builder.build_commit(info, branch, parent_sha)

        if sha:
            self._state_store.record_commit(
                cl, sha, stream, branch,
                has_integration=self._commit_builder.last_has_integration,
            )
        else:
            logger.debug("빈 CL 스킵 (파일 0개): CL %d", cl)
        self._state_store.set_last_synced_cl(stream, cl, sha)

        if self._silence_detector:
            self._silence_detector.record_sync()
        if self._daily_reporter:
            self._daily_reporter.stats.record_sync(stream, 0)

        self._git_operator.maybe_run_gc(self._config.sync.git_gc_interval)

        if self._config.sync.push_after_every_commit:
            try:
                self._git_operator.push(branch, lfs_enabled=self._config.lfs.enabled)
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

    # ── 양방향 동기화 ──

    def _has_bidirectional_streams(self) -> bool:
        """bidirectional 또는 git_to_p4 방향 stream이 있는지 확인."""
        policy = self._config.stream_policy
        for sd in policy.sync_directions:
            if sd.direction in ("bidirectional", "git_to_p4"):
                return True
        return False

    def _initialize_bidirectional(self) -> None:
        """양방향 동기화 컴포넌트 초기화. bidirectional stream이 없으면 스킵."""
        if not self._has_bidirectional_streams():
            return

        self._git_change_detector = GitChangeDetector(
            git_operator=self._git_operator,
            state_store=self._state_store,
            remote=self._config.git.watch_remote,
        )

        submit_ws = self._config.p4.submit_workspace or self._config.p4.workspace
        self._p4_submitter = P4Submitter(
            p4_client=self._p4_client,
            workspace=submit_ws,
            submit_as_user=self._config.p4.submit_as_user,
        )
        self._p4_submitter.initialize()

        self._conflict_detector = ConflictDetector(
            git_operator=self._git_operator,
            p4_client=self._p4_client,
            state_store=self._state_store,
        )

        if not self._config.git.remote_url:
            logger.warning(
                "remote_url이 설정되지 않음: 양방향 동기화에서 fetch를 스킵하고 "
                "로컬 branch HEAD 변경만 감지합니다."
            )

        logger.info("양방향 동기화 컴포넌트 초기화 완료")

    def _get_stream_direction(self, stream: str) -> str:
        return self._config.stream_policy.get_direction(stream)

    def _get_reverse_builder(self, stream: str) -> ReverseCommitBuilder:
        if stream not in self._reverse_builders:
            self._reverse_builders[stream] = ReverseCommitBuilder(
                git_operator=self._git_operator,
                p4_submitter=self._p4_submitter,
                state_store=self._state_store,
                stream=stream,
                user_mapper=self._user_mapper,
                lfs_store=self._lfs_store,
            )
        return self._reverse_builders[stream]

    def _check_and_handle_conflicts(self, stream: str, branch: str) -> bool:
        """Forward sync 전에 P4/Git 양쪽 변경을 비교하여 충돌을 감지한다.

        Returns:
            True이면 충돌 발생 (forward/reverse 모두 스킵해야 함).
        """
        if self._git_change_detector is None or self._conflict_detector is None:
            return False

        # 이미 충돌 상태이면 해결 여부만 확인
        conflict = self._state_store.get_conflict(branch)
        if conflict:
            self._check_conflict_resolved(branch, conflict)
            return True

        # Git 쪽 새 commit 감지 (fetch 포함)
        if self._config.git.remote_url:
            try:
                self._git_change_detector.fetch()
            except Exception as e:
                logger.error("git fetch 실패 (충돌 감지 단계): %s", e)
                return False

        new_commits = self._git_change_detector.detect_new_commits(branch)
        if not new_commits:
            return False

        # P4 쪽 변경 수집
        p4_changes = self._collect_p4_changes_with_files(stream)
        if not p4_changes:
            return False

        # 충돌 검사
        conflict_info = self._conflict_detector.detect(
            branch, p4_changes, new_commits,
        )
        if conflict_info is not None:
            self._handle_conflict(conflict_info, stream)
            return True

        return False

    def _poll_reverse_sync(self, stream: str, branch: str) -> None:
        """Git→P4 역방향 동기화 폴링."""
        if self._git_change_detector is None:
            return

        direction = self._get_stream_direction(stream)
        if direction not in ("bidirectional", "git_to_p4"):
            return

        # 충돌 상태 확인
        conflict = self._state_store.get_conflict(branch)
        if conflict:
            self._check_conflict_resolved(branch, conflict)
            return

        if self._config.git.remote_url:
            try:
                self._git_change_detector.fetch()
            except Exception as e:
                logger.error("git fetch 실패: %s", e)
                return

        new_commits = self._git_change_detector.detect_new_commits(branch)
        if not new_commits:
            return

        # 양방향이면 충돌 검사
        if direction == "bidirectional":
            p4_changes = self._collect_p4_changes_with_files(stream)
            if p4_changes:
                conflict_info = self._conflict_detector.detect(
                    branch, p4_changes, new_commits,
                )
                if conflict_info is not None:
                    self._handle_conflict(conflict_info, stream)
                    return

        # 역방향 동기화 실행
        reverse_builder = self._get_reverse_builder(stream)
        for commit in new_commits:
            try:
                reverse_builder.sync_commit(commit, branch)
                self._git_change_detector.update_last_processed(
                    branch, commit["sha"],
                )
            except Exception as e:
                logger.error(
                    "Git→P4 동기화 실패: %s — %s", commit["sha"][:12], e,
                )
                break

    def _collect_p4_changes_with_files(
        self, stream: str,
    ) -> list[tuple[int, list[str]]]:
        """P4 변경사항을 파일 목록과 함께 수집."""
        changes = self._poller.poll(stream, self._config.sync.batch_size, poll_stream=self._poll_stream)
        result = []
        for cl in changes:
            info = self._p4_client.describe(cl)
            # GitCommit 마커가 있으면 스킵
            if parse_git_commit_from_description(info.description):
                continue
            files = [fa.depot_path for fa in info.files]
            result.append((cl, files))
        return result

    def _handle_conflict(self, conflict_info, stream: str) -> None:
        """충돌 처리: 충돌 branch 생성, 알림."""
        try:
            conflict_branch = self._conflict_detector.create_conflict_branch(
                conflict_info, stream,
            )
            if self._notifier:
                self._notifier.send_conflict_alert(
                    branch=conflict_info.branch,
                    conflict_branch=conflict_branch,
                    conflict_files=conflict_info.conflict_files,
                    p4_changelists=conflict_info.p4_changelists,
                    git_commits=conflict_info.git_commits,
                )
            logger.warning(
                "충돌 처리 완료: branch=%s, 충돌 branch=%s",
                conflict_info.branch, conflict_branch,
            )
        except Exception:
            logger.exception("충돌 branch 생성 실패")

    def _check_conflict_resolved(self, branch: str, conflict: dict) -> None:
        """충돌 branch 삭제 여부를 확인하여 해결 판정."""
        if self._git_change_detector is None:
            return

        try:
            self._git_change_detector.fetch()
        except Exception:
            return

        conflict_branch = conflict["conflict_branch"]
        if self._git_change_detector.is_conflict_resolved(conflict_branch):
            self._state_store.resolve_conflict(branch)
            logger.info("충돌 해결 감지: branch=%s (충돌 branch '%s' 삭제됨)", branch, conflict_branch)
            if self._notifier:
                self._notifier.send_info(
                    f"충돌 해결됨: {branch} (충돌 branch '{conflict_branch}' 삭제됨)"
                )
