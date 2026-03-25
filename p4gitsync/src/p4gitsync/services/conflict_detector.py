from __future__ import annotations

import logging
from dataclasses import dataclass, field

from p4gitsync.git.commit_metadata import CommitMetadata
from p4gitsync.git.git_change_detector import CONFLICT_BRANCH_PREFIX
from p4gitsync.git.git_operator import GitOperator
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.services.conflict_detector")


@dataclass
class ConflictInfo:
    """충돌 정보."""

    branch: str
    conflict_files: list[str]
    p4_changelists: list[int]
    git_commits: list[str]


class ConflictDetector:
    """양방향 변경 파일의 교집합을 계산하여 충돌을 감지한다."""

    def __init__(
        self,
        git_operator: GitOperator,
        p4_client: P4Client,
        state_store: StateStore,
    ) -> None:
        self._git = git_operator
        self._p4 = p4_client
        self._state = state_store

    def detect(
        self,
        branch: str,
        p4_changes: list[tuple[int, list[str]]],
        git_commits: list[dict],
    ) -> ConflictInfo | None:
        """충돌 감지.

        Args:
            branch: 대상 branch
            p4_changes: [(changelist, [depot_path, ...])] P4 변경 목록
            git_commits: [{"sha", ...}] Git commit 목록

        Returns:
            충돌 시 ConflictInfo, 없으면 None
        """
        if not p4_changes or not git_commits:
            return None

        p4_files: set[str] = set()
        p4_cls: list[int] = []
        for cl, files in p4_changes:
            p4_cls.append(cl)
            for f in files:
                p4_files.add(self._normalize_path(f))

        git_files: set[str] = set()
        git_shas: list[str] = []
        for commit in git_commits:
            git_shas.append(commit["sha"])
            file_changes, deletes = self._git.get_commit_files(commit["sha"])
            for path, _ in file_changes:
                git_files.add(self._normalize_path(path))
            for path in deletes:
                git_files.add(self._normalize_path(path))

        conflicted = p4_files & git_files
        if not conflicted:
            return None

        logger.warning(
            "충돌 감지: branch=%s, 파일 %d개, P4 CL=%s, Git commits=%s",
            branch,
            len(conflicted),
            p4_cls,
            [s[:12] for s in git_shas],
        )

        return ConflictInfo(
            branch=branch,
            conflict_files=sorted(conflicted),
            p4_changelists=p4_cls,
            git_commits=git_shas,
        )

    def create_conflict_branch(
        self,
        conflict: ConflictInfo,
        stream: str,
    ) -> str:
        """P4 변경사항으로 충돌 branch를 생성하고 push한다.

        Returns:
            생성된 충돌 branch 이름
        """
        primary_cl = conflict.p4_changelists[0]
        conflict_branch = f"{CONFLICT_BRANCH_PREFIX}{conflict.branch}/CL{primary_cl}"

        # 현재 branch의 HEAD에서 충돌 branch 생성
        head_sha = self._git.get_head_sha(conflict.branch)
        if head_sha is None:
            raise RuntimeError(
                f"branch '{conflict.branch}'의 HEAD를 찾을 수 없음"
            )

        self._git.create_branch(conflict_branch, head_sha)

        # P4 변경사항을 충돌 branch에 commit
        all_file_changes = []
        all_deletes = []
        for cl in conflict.p4_changelists:
            info = self._p4.describe(cl)
            from p4gitsync.p4.p4_file_action import P4FileAction

            for fa in info.files:
                if fa.action in ("delete", "move/delete", "purge"):
                    all_deletes.append(fa.depot_path.split("//")[-1].split("/", 1)[-1])
                else:
                    content = self._p4.print_file_to_bytes(fa.depot_path, fa.revision)
                    if content is not None:
                        git_path = fa.depot_path.split("//")[-1].split("/", 1)[-1]
                        all_file_changes.append((git_path, content))

        if all_file_changes or all_deletes:
            metadata = CommitMetadata(
                author_name="P4GitSync",
                author_email="p4gitsync@system",
                author_timestamp=int(__import__("time").time()),
                message=f"Conflict: P4 CL {conflict.p4_changelists} vs Git commits\n\n"
                        f"Conflicting files:\n"
                        + "\n".join(f"  - {f}" for f in conflict.conflict_files),
                p4_changelist=primary_cl,
            )

            self._git.create_commit(
                branch=conflict_branch,
                parent_sha=head_sha,
                metadata=metadata,
                file_changes=all_file_changes,
                deletes=all_deletes,
            )

        # push
        self._git.push(conflict_branch)

        # StateStore에 충돌 기록
        self._state.record_conflict(
            branch=conflict.branch,
            conflict_branch=conflict_branch,
            p4_changelists=conflict.p4_changelists,
            git_commits=conflict.git_commits,
            conflict_files=conflict.conflict_files,
        )

        logger.info(
            "충돌 branch 생성 완료: %s (파일 %d개)",
            conflict_branch, len(conflict.conflict_files),
        )
        return conflict_branch

    def _normalize_path(self, path: str) -> str:
        """depot path와 git path를 비교 가능한 형태로 정규화.

        //depot/stream/dir/file.cpp → dir/file.cpp
        dir/file.cpp → dir/file.cpp
        """
        if path.startswith("//"):
            parts = path.split("/")
            # //depot/stream/rest... → rest...
            if len(parts) > 4:
                return "/".join(parts[4:])
        return path
