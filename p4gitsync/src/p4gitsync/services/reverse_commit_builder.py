from __future__ import annotations

import logging

from p4gitsync.git.git_operator import GitOperator
from p4gitsync.p4.p4_submitter import P4Submitter
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.services.reverse_commit_builder")


class ReverseCommitBuilder:
    """Git commit을 P4 changelist로 변환하여 submit한다."""

    def __init__(
        self,
        git_operator: GitOperator,
        p4_submitter: P4Submitter,
        state_store: StateStore,
        stream: str,
        user_mapper=None,
    ) -> None:
        self._git = git_operator
        self._submitter = p4_submitter
        self._state = state_store
        self._stream = stream
        self._user_mapper = user_mapper

    def sync_commit(self, commit: dict, branch: str) -> int:
        """단일 Git commit을 P4에 submit한다.

        Args:
            commit: GitChangeDetector에서 반환된 commit dict
                {"sha", "message", "author_name", "author_email", "timestamp", "parents"}
            branch: Git branch 이름

        Returns:
            submit된 P4 changelist 번호
        """
        sha = commit["sha"]
        logger.info("Git→P4 동기화 시작: %s (%s)", sha[:12], branch)

        file_changes, deletes = self._git.get_commit_files(sha)

        # UserMapper 플러그인 사용 시
        if self._user_mapper:
            p4_info = self._user_mapper.git_to_p4({
                "author_name": commit["author_name"],
                "author_email": commit["author_email"],
                "message": commit["message"],
            })
            p4_user = p4_info.get("user")
            workspace = p4_info.get("workspace")
            base_description = p4_info.get("description", commit["message"])
            description = f"{base_description}\n\nGitCommit: {sha}"
            if workspace:
                self._submitter.set_workspace(workspace)
        else:
            p4_user = self._state.get_p4_user(commit["author_email"])
            if p4_user is None:
                logger.warning(
                    "P4 사용자 매핑 없음: %s <%s> — 기본 사용자로 submit",
                    commit["author_name"], commit["author_email"],
                )
            description = self._build_description(commit)

        submitted_cl = self._submitter.submit_changes(
            description=description,
            file_changes=file_changes,
            deletes=deletes,
            p4_user=p4_user,
        )

        self._state.record_commit(
            cl=submitted_cl,
            sha=sha,
            stream=self._stream,
            branch=branch,
            sync_direction="git_to_p4",
        )

        logger.info(
            "Git→P4 동기화 완료: %s → CL %d", sha[:12], submitted_cl,
        )
        return submitted_cl

    def _build_description(self, commit: dict) -> str:
        """P4 changelist description을 생성한다.

        원본 commit message + GitCommit trailer.
        """
        message = commit["message"]
        # 기존 trailer가 있으면 그 앞에 삽입
        return f"{message}\n\nGitCommit: {commit['sha']}"
