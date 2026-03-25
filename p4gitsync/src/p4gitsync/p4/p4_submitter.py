from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from p4gitsync.p4.p4_client import P4Client

logger = logging.getLogger("p4gitsync.p4.submitter")


class P4Submitter:
    """Git commit의 파일 변경사항을 P4에 submit한다."""

    def __init__(
        self,
        p4_client: P4Client,
        workspace: str,
        submit_as_user: bool = True,
    ) -> None:
        self._p4 = p4_client
        self._workspace = workspace
        self._submit_as_user = submit_as_user
        self._workspace_root: str | None = None

    def initialize(self) -> None:
        """workspace root 경로를 캐시."""
        self._workspace_root = self._p4.get_workspace_root(self._workspace)
        if not self._workspace_root:
            raise RuntimeError(
                f"P4 workspace '{self._workspace}'의 root를 찾을 수 없음"
            )
        logger.info(
            "P4Submitter 초기화: workspace=%s, root=%s",
            self._workspace, self._workspace_root,
        )

    def set_workspace(self, workspace: str) -> None:
        """동적으로 workspace를 변경한다 (UserMapper 플러그인용)."""
        if workspace != self._workspace:
            self._workspace = workspace
            self._workspace_root = self._p4.get_workspace_root(workspace)
            logger.debug("workspace 변경: %s (root=%s)", workspace, self._workspace_root)

    def submit_changes(
        self,
        description: str,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str],
        p4_user: str | None = None,
    ) -> int:
        """파일 변경사항을 P4에 submit한다.

        Args:
            description: changelist 설명 (GitCommit trailer 포함)
            file_changes: [(상대경로, content_bytes)] 추가/수정 파일
            deletes: [상대경로] 삭제 파일
            p4_user: submit할 P4 사용자 (submit_as_user=True 시)

        Returns:
            submit된 changelist 번호
        """
        user = p4_user if self._submit_as_user and p4_user else None
        cl = self._p4.create_changelist(description, user=user)

        try:
            self._apply_changes(cl, file_changes, deletes)
            submitted_cl = self._p4.submit_changelist(cl)
            logger.info("P4 submit 완료: CL %d (user=%s)", submitted_cl, user or "default")
            return submitted_cl
        except Exception:
            logger.exception("P4 submit 실패, changelist %d revert 중", cl)
            self._p4.revert_changelist(cl)
            self._p4.delete_changelist(cl)
            raise

    def _apply_changes(
        self,
        changelist: int,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str],
    ) -> None:
        """workspace에 파일 변경을 적용하고 P4에 등록."""
        root = Path(self._workspace_root)

        for rel_path, content in file_changes:
            local_path = root / rel_path
            local_path.parent.mkdir(parents=True, exist_ok=True)

            depot_path = self._to_depot_path(rel_path)
            is_new = not self._p4.file_exists(depot_path)

            local_path.write_bytes(content)

            if is_new:
                self._p4.p4_add(str(local_path), changelist)
            else:
                self._p4.p4_edit(str(local_path), changelist)

        for rel_path in deletes:
            depot_path = self._to_depot_path(rel_path)
            if self._p4.file_exists(depot_path):
                self._p4.p4_delete(depot_path, changelist)

    def _to_depot_path(self, rel_path: str) -> str:
        """상대 경로를 depot 경로로 변환.

        workspace view 매핑을 사용하여 변환한다.
        간단한 구현: workspace root 기반 로컬 경로를 p4 where로 조회.
        """
        local_path = os.path.join(self._workspace_root, rel_path)
        try:
            result = self._p4._p4.run_where(local_path)
            if result and isinstance(result[0], dict):
                return result[0].get("depotFile", local_path)
        except Exception:
            pass
        return local_path
