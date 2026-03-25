from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from p4gitsync.config.sync_config import AppConfig
from p4gitsync.git.git_operator import GitOperator
from p4gitsync.notifications.notifier import SlackNotifier
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.services.circuit_breaker import IntegrityCircuitBreaker
from p4gitsync.services.commit_builder import CommitBuilder
from p4gitsync.services.integrity_checker import IntegrityChecker
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.cutover")


class CutoverPhase(Enum):
    NOT_STARTED = "not_started"
    FREEZE_CHECK = "freeze_check"
    FINAL_SYNC = "final_sync"
    INTEGRITY_VERIFY = "integrity_verify"
    FINAL_PUSH = "final_push"
    SWITCH_SOURCE = "switch_source"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class CutoverResult:
    success: bool
    phase: CutoverPhase
    message: str
    details: list[str] = field(default_factory=list)


class CutoverManager:
    """P4→Git 컷오버를 관리한다.

    Phase A: Freeze & Final Sync
      1. P4 submit 차단 확인
      2. 잔여 CL 처리
      3. total_lag=0 확인
      4. 최종 무결성 검증
      5. 최종 push

    Phase B: Git remote 공식 소스 지정, 동기화 서비스 종료
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._p4_client: P4Client | None = None
        self._git_operator: GitOperator | None = None
        self._state_store: StateStore | None = None
        self._notifier: SlackNotifier | None = None
        self._integrity_checker: IntegrityChecker | None = None
        self._phase = CutoverPhase.NOT_STARTED

    def dry_run(self) -> CutoverResult:
        """컷오버를 시뮬레이션한다. 실제 변경 없이 준비 상태를 확인."""
        logger.info("=== 컷오버 Dry Run 시작 ===")
        try:
            self._initialize()
            checks: list[str] = []

            # 1. P4 freeze 상태 확인
            freeze_ok = self._check_p4_frozen()
            checks.append(
                f"P4 freeze: {'OK' if freeze_ok else 'WARN - submit 차단 미확인'}"
            )

            # 2. 잔여 CL 확인
            pending_cls = self._get_pending_changelists()
            checks.append(f"잔여 CL: {len(pending_cls)}건")

            # 3. 미완료 push 확인
            pending_pushes = self._state_store.get_pending_pushes()
            checks.append(f"미완료 push: {len(pending_pushes)}건")

            # 4. 미해결 에러 확인
            errors = self._state_store.get_unresolved_errors()
            checks.append(f"미해결 에러: {len(errors)}건")

            # 5. 무결성 검증 (샘플)
            integrity_result = self._integrity_checker.verify_sample(50)
            checks.append(
                f"무결성 검증 (50개 샘플): "
                f"{'PASS' if integrity_result.passed else 'FAIL'} "
                f"({integrity_result.checked_files}개 확인, "
                f"{len(integrity_result.mismatched_files)}개 불일치)"
            )

            # 준비 여부 판정
            ready = (
                len(pending_cls) == 0
                and len(pending_pushes) == 0
                and len(errors) == 0
                and integrity_result.passed
            )

            for check in checks:
                logger.info("  %s", check)

            status = "READY" if ready else "NOT READY"
            logger.info("=== Dry Run 결과: %s ===", status)

            return CutoverResult(
                success=ready,
                phase=CutoverPhase.NOT_STARTED,
                message=f"Dry run 완료: {status}",
                details=checks,
            )
        except Exception as e:
            logger.exception("Dry run 실패")
            return CutoverResult(
                success=False,
                phase=CutoverPhase.FAILED,
                message=f"Dry run 실패: {e}",
            )
        finally:
            self._cleanup()

    def execute(self) -> CutoverResult:
        """컷오버를 실행한다."""
        logger.info("=== 컷오버 실행 시작 ===")
        try:
            self._initialize()
            details: list[str] = []

            # Phase A-1: P4 freeze 확인
            self._phase = CutoverPhase.FREEZE_CHECK
            if not self._check_p4_frozen():
                logger.warning("P4 submit 차단이 확인되지 않음 - 계속 진행")
                details.append("WARN: P4 submit 차단 미확인 상태로 진행")

            # Phase A-2: 잔여 CL 처리
            self._phase = CutoverPhase.FINAL_SYNC
            synced_count = self._sync_remaining()
            details.append(f"잔여 CL {synced_count}건 처리 완료")

            # total_lag=0 확인
            pending_pushes = self._state_store.get_pending_pushes()
            if pending_pushes:
                return CutoverResult(
                    success=False,
                    phase=CutoverPhase.FINAL_SYNC,
                    message=f"total_lag != 0: 미완료 push {len(pending_pushes)}건",
                    details=details,
                )
            details.append("total_lag=0 확인")

            # Phase A-3: 최종 무결성 검증
            self._phase = CutoverPhase.INTEGRITY_VERIFY
            integrity_result = self._integrity_checker.verify_full()
            if not integrity_result.passed:
                return CutoverResult(
                    success=False,
                    phase=CutoverPhase.INTEGRITY_VERIFY,
                    message=(
                        f"최종 무결성 검증 실패: "
                        f"{len(integrity_result.mismatched_files)}개 파일 불일치"
                    ),
                    details=details,
                )
            details.append(
                f"무결성 검증 통과: {integrity_result.checked_files}개 파일 확인"
            )

            # Phase A-4: 최종 push
            self._phase = CutoverPhase.FINAL_PUSH
            branch = self._config.git.default_branch
            self._git_operator.push(branch)
            details.append(f"최종 push 완료: {branch}")

            # 모든 branch push
            mappings = self._state_store.get_all_registered_streams()
            for m in mappings:
                if m.branch != branch:
                    try:
                        self._git_operator.push(m.branch)
                        details.append(f"branch push: {m.branch}")
                    except Exception as e:
                        logger.warning("branch push 실패: %s (%s)", m.branch, e)

            # Phase B: 공식 소스 전환
            self._phase = CutoverPhase.SWITCH_SOURCE
            details.append("Git이 공식 소스로 지정됨")
            details.append("동기화 서비스 종료 준비 완료")

            if self._notifier:
                self._notifier.send_info(
                    "P4 -> Git 컷오버 완료. Git이 공식 소스입니다."
                )

            self._phase = CutoverPhase.COMPLETED
            logger.info("=== 컷오버 완료 ===")

            return CutoverResult(
                success=True,
                phase=CutoverPhase.COMPLETED,
                message="컷오버 완료: Git이 공식 소스입니다",
                details=details,
            )
        except Exception as e:
            logger.exception("컷오버 실행 실패 (phase=%s)", self._phase.value)
            if self._notifier:
                self._notifier.send_error(
                    0, "cutover", f"컷오버 실패 (phase={self._phase.value}): {e}"
                )
            return CutoverResult(
                success=False,
                phase=self._phase,
                message=f"컷오버 실패: {e}",
            )
        finally:
            self._cleanup()

    def _initialize(self) -> None:
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

        slack = self._config.slack
        self._notifier = SlackNotifier(
            webhook_url=slack.webhook_url,
            channel=slack.channel,
            alerts_webhook_url=slack.alerts_webhook_url,
            warnings_webhook_url=slack.warnings_webhook_url,
            info_webhook_url=slack.info_webhook_url,
        )

        self._integrity_checker = IntegrityChecker(
            p4_client=self._p4_client,
            repo_path=self._config.git.repo_path,
            stream=self._config.p4.stream,
        )

    def _cleanup(self) -> None:
        if self._p4_client:
            try:
                self._p4_client.disconnect()
            except Exception:
                pass
        if self._state_store:
            try:
                self._state_store.close()
            except Exception:
                pass

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

    def _check_p4_frozen(self) -> bool:
        """P4 서버에 submit이 차단되었는지 확인.

        P4 triggers에서 submit을 차단하는 설정이 있는지 간접 확인한다.
        실제로는 운영팀이 P4 admin으로 freeze를 설정해야 한다.
        """
        try:
            stream = self._config.p4.stream
            pending = self._p4_client.get_changes_after(
                stream, self._state_store.get_last_synced_cl(stream)
            )
            return len(pending) == 0
        except Exception:
            logger.warning("P4 freeze 상태 확인 실패")
            return False

    def _get_pending_changelists(self) -> list[int]:
        """동기화되지 않은 잔여 CL 목록."""
        stream = self._config.p4.stream
        last_cl = self._state_store.get_last_synced_cl(stream)
        return self._p4_client.get_changes_after(stream, last_cl)

    def _sync_remaining(self) -> int:
        """잔여 CL을 모두 동기화한다."""
        from p4gitsync.p4.merge_analyzer import MergeAnalyzer

        stream = self._config.p4.stream
        branch = self._config.git.default_branch
        pending = self._get_pending_changelists()

        if not pending:
            return 0

        logger.info("잔여 CL %d건 동기화 시작", len(pending))

        merge_analyzer = MergeAnalyzer(self._p4_client)
        commit_builder = CommitBuilder(
            p4_client=self._p4_client,
            git_operator=self._git_operator,
            state_store=self._state_store,
            stream=stream,
            lfs_config=self._config.lfs if self._config.lfs.enabled else None,
            merge_analyzer=merge_analyzer,
        )

        for cl in pending:
            info = self._p4_client.describe(cl)
            last_cl = self._state_store.get_last_synced_cl(stream)
            parent_sha = (
                self._state_store.get_commit_sha(last_cl, stream)
                if last_cl > 0 else None
            )
            sha = commit_builder.build_commit(info, branch, parent_sha)
            self._state_store.record_commit(cl, sha, stream, branch)
            self._state_store.set_last_synced_cl(stream, cl, sha)

        # push
        self._git_operator.push(branch)
        for cl in pending:
            self._state_store.update_push_status(cl, stream, "pushed")

        logger.info("잔여 CL %d건 동기화 및 push 완료", len(pending))
        return len(pending)
