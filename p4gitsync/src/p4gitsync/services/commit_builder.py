import logging
import os
from pathlib import Path

from p4gitsync.git.commit_metadata import CommitMetadata
from p4gitsync.git.git_operator import GitOperator
from p4gitsync.p4.p4_change_info import P4ChangeInfo
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.p4.p4_file_action import P4FileAction
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.commit_builder")


class CommitBuilder:
    """P4 changelist를 Git commit으로 변환."""

    def __init__(
        self,
        p4_client: P4Client,
        git_operator: GitOperator,
        state_store: StateStore,
        stream: str,
        stream_prefix_len: int | None = None,
    ) -> None:
        self._p4 = p4_client
        self._git = git_operator
        self._state = state_store
        self._stream = stream
        if stream_prefix_len is not None:
            self._stream_prefix_len = stream_prefix_len
        else:
            self._stream_prefix_len = len(stream) + 1

    def build_commit(
        self,
        info: P4ChangeInfo,
        branch: str,
        parent_sha: str | None,
    ) -> str:
        """P4 changelist 정보를 기반으로 Git commit을 생성하고 SHA 반환."""
        file_changes, deletes = self._extract_file_changes(info)

        name, email = self._state.get_git_author(info.user)
        metadata = CommitMetadata(
            author_name=name,
            author_email=email,
            author_timestamp=info.timestamp,
            message=info.description,
            p4_changelist=info.changelist,
        )

        sha = self._git.create_commit(
            branch, parent_sha, metadata, file_changes, deletes,
        )
        logger.info("CL %d -> commit %s", info.changelist, sha[:8])
        return sha

    def _extract_file_changes(
        self, info: P4ChangeInfo,
    ) -> tuple[list[tuple[str, bytes]], list[str]]:
        """changelist의 파일 변경 사항을 추출."""
        file_changes: list[tuple[str, bytes]] = []
        deletes: list[str] = []

        for fa in info.files:
            git_path = self._depot_to_git_path(fa.depot_path)
            if git_path is None:
                continue

            if fa.action in ("delete", "move/delete", "purge"):
                deletes.append(git_path)
            elif fa.action in ("add", "edit", "branch", "integrate", "move/add"):
                content = self._p4.print_file_to_bytes(fa.depot_path, fa.revision)
                if content is not None:
                    file_changes.append((git_path, content))
                else:
                    logger.warning(
                        "파일 내용 추출 실패, 건너뜀: %s#%d", fa.depot_path, fa.revision
                    )

        return file_changes, deletes

    def _depot_to_git_path(self, depot_path: str) -> str | None:
        """depot path를 Git 리포지토리 내의 상대 경로로 변환.

        예: //ProjectSTAR/main/src/foo.py -> src/foo.py
        """
        if not depot_path.startswith(self._stream + "/"):
            return None
        return depot_path[self._stream_prefix_len:]
