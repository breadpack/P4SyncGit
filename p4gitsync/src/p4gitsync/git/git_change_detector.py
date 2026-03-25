from __future__ import annotations

import logging

from p4gitsync.git.commit_metadata import parse_p4cl_from_message
from p4gitsync.git.git_operator import GitOperator
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.git.change_detector")

CONFLICT_BRANCH_PREFIX = "conflict/"


class GitChangeDetector:
    """Git remote를 fetch하여 새 commit을 감지한다."""

    def __init__(
        self,
        git_operator: GitOperator,
        state_store: StateStore,
        remote: str = "origin",
    ) -> None:
        self._git = git_operator
        self._state = state_store
        self._remote = remote

    def fetch(self) -> None:
        """remote에서 fetch."""
        self._git.fetch(self._remote)

    def detect_new_commits(self, branch: str) -> list[dict]:
        """branch에서 아직 처리하지 않은 새 Git commit 목록을 반환한다.

        P4CL trailer가 있는 commit(P4에서 온 것)은 제외한다.
        반환: [{"sha", "message", "author_name", "author_email", "timestamp", "parents"}]
        """
        last_sha = self._state.get_last_reverse_sync_sha(branch)
        all_commits = self._git.get_log_after(branch, last_sha, self._remote)

        new_commits = []
        for commit in all_commits:
            p4cl = parse_p4cl_from_message(commit["message"])
            if p4cl is not None:
                logger.debug(
                    "P4CL 마커 발견, 스킵: %s (P4CL: %d)", commit["sha"][:12], p4cl,
                )
                continue
            # StateStore에 이미 기록된 commit도 스킵
            existing = self._state.get_commit_sha_by_sha(commit["sha"])
            if existing is not None:
                logger.debug("이미 동기화된 commit, 스킵: %s", commit["sha"][:12])
                continue
            new_commits.append(commit)
        return new_commits

    def get_conflict_branches(self) -> list[str]:
        """현재 존재하는 conflict/ prefix remote branch 목록."""
        return self._git.list_remote_branches(
            self._remote, prefix=CONFLICT_BRANCH_PREFIX,
        )

    def is_conflict_resolved(self, conflict_branch: str) -> bool:
        """충돌 branch가 삭제되었는지(= 해결되었는지) 확인."""
        existing = self._git.list_remote_branches(
            self._remote, prefix=conflict_branch,
        )
        return conflict_branch not in existing

    def update_last_processed(self, branch: str, commit_sha: str) -> None:
        """마지막으로 처리한 commit SHA 업데이트."""
        self._state.set_last_reverse_sync_sha(branch, commit_sha)
