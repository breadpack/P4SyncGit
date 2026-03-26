from __future__ import annotations

import logging
from pathlib import Path

from p4gitsync.git.git_operator import GitOperator
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.lfs.lfs_pointer_utils import is_lfs_pointer, parse_lfs_pointer
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
        lfs_store: LfsObjectStore | None = None,
    ) -> None:
        self._git = git_operator
        self._submitter = p4_submitter
        self._state = state_store
        self._stream = stream
        self._user_mapper = user_mapper
        self._lfs_store = lfs_store

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

        resolved_changes = []
        for path, content in file_changes:
            resolved = self._resolve_lfs_content(path, content)
            resolved_changes.append((path, resolved))

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
            file_changes=resolved_changes,
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

    def _resolve_lfs_content(self, path: str, content: bytes) -> bytes | Path:
        """LFS 포인터면 실제 파일 경로 반환, 아니면 원본 content 반환."""
        if not self._lfs_store or not is_lfs_pointer(content):
            return content
        try:
            pointer = parse_lfs_pointer(content)
            return self._lfs_store.retrieve(pointer.oid)
        except (ValueError, FileNotFoundError) as e:
            logger.warning("LFS 파일 복원 실패 (%s): %s", path, e)
            return content

    def _build_description(self, commit: dict) -> str:
        """P4 changelist description을 생성한다.

        원본 commit message + GitCommit trailer.
        """
        message = commit["message"]
        # 기존 trailer가 있으면 그 앞에 삽입
        return f"{message}\n\nGitCommit: {commit['sha']}"
