import asyncio
import logging

from p4gitsync.git.pygit2_git_operator import Pygit2GitOperator
from p4gitsync.notifications.notifier import SlackNotifier
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.services.changelist_poller import ChangelistPoller
from p4gitsync.services.commit_builder import CommitBuilder
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.orchestrator")


class SyncOrchestrator:
    def __init__(self, config: dict) -> None:
        self._config = config
        self._sync_config = config.get("sync", {})
        self._p4_client: P4Client | None = None
        self._git_operator: Pygit2GitOperator | None = None
        self._state_store: StateStore | None = None
        self._poller: ChangelistPoller | None = None
        self._commit_builder: CommitBuilder | None = None
        self._notifier: SlackNotifier | None = None
        self._running = False

    async def start(self) -> None:
        """서비스 시작: 초기화 -> 정합성 검증 -> 폴링 루프."""
        self._initialize_components()
        self._verify_on_startup()
        self._running = True

        stream = self._config["p4"]["stream"]
        logger.info("동기화 시작: stream=%s", stream)

        while self._running:
            try:
                await self._poll_and_sync()
            except Exception:
                logger.exception("폴링 루프 에러")
            await asyncio.sleep(self._sync_config.get("polling_interval_seconds", 30))

    async def stop(self) -> None:
        self._running = False
        if self._p4_client:
            self._p4_client.disconnect()
        if self._state_store:
            self._state_store.close()

    def _initialize_components(self) -> None:
        p4_cfg = self._config["p4"]
        git_cfg = self._config["git"]
        state_cfg = self._config["state"]
        slack_cfg = self._config.get("slack", {})

        self._state_store = StateStore(state_cfg["db_path"])
        self._state_store.initialize()

        self._p4_client = P4Client(
            port=p4_cfg["port"],
            user=p4_cfg["user"],
            workspace=p4_cfg["workspace"],
        )
        self._p4_client.connect()

        self._git_operator = Pygit2GitOperator(
            repo_path=git_cfg["repo_path"],
            remote_url=git_cfg.get("remote_url", ""),
        )
        self._git_operator.init_repo()

        stream = p4_cfg["stream"]
        self._poller = ChangelistPoller(self._p4_client, self._state_store)
        self._commit_builder = CommitBuilder(
            p4_client=self._p4_client,
            git_operator=self._git_operator,
            state_store=self._state_store,
            stream=stream,
        )

        self._notifier = SlackNotifier(
            webhook_url=slack_cfg.get("webhook_url", ""),
            channel=slack_cfg.get("channel", ""),
        )

    def _verify_on_startup(self) -> None:
        """서비스 시작 시 Git HEAD와 StateStore 정합성 검증."""
        branch = self._config["git"]["default_branch"]
        head_sha = self._git_operator.get_head_sha(branch)

        if head_sha is None:
            logger.info("Git HEAD 없음 (초기 상태)")
            return

        if not self._state_store.verify_consistency(branch, head_sha):
            logger.error(
                "정합성 불일치! Git HEAD=%s vs StateStore. 수동 확인 필요.",
                head_sha,
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

    async def _poll_and_sync(self) -> None:
        stream = self._config["p4"]["stream"]
        branch = self._config["git"]["default_branch"]
        batch_size = self._sync_config.get("batch_size", 50)

        changes = self._poller.poll(stream, batch_size)
        if not changes:
            return

        for cl in changes:
            try:
                await self._process_changelist(cl, stream, branch)
            except Exception as e:
                retry_count = self._state_store.record_sync_error(cl, stream, str(e))
                logger.error("CL %d 처리 실패 (retry=%d): %s", cl, retry_count, e)
                threshold = self._sync_config.get("error_retry_threshold", 3)
                if retry_count >= threshold:
                    self._notifier.send_error(cl, stream, str(e))
                break

        if not self._sync_config.get("push_after_every_commit", False):
            try:
                self._git_operator.push(branch)
                self._mark_batch_pushed(changes, stream)
            except Exception as e:
                logger.error("일괄 push 실패: %s", e)

    async def _process_changelist(self, cl: int, stream: str, branch: str) -> None:
        """단일 CL 처리: 파일 추출 -> commit 생성 -> 상태 기록."""
        info = self._p4_client.describe(cl)

        last_cl = self._state_store.get_last_synced_cl(stream)
        parent_sha = self._state_store.get_commit_sha(last_cl, stream) if last_cl > 0 else None

        sha = self._commit_builder.build_commit(info, branch, parent_sha)

        self._state_store.record_commit(cl, sha, stream, branch)
        self._state_store.set_last_synced_cl(stream, cl, sha)

        gc_interval = self._sync_config.get("git_gc_interval", 5000)
        self._git_operator.maybe_run_gc(gc_interval)

        if self._sync_config.get("push_after_every_commit", False):
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
                pass
