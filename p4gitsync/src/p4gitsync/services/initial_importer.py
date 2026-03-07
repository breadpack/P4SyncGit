import logging
import subprocess
import time

from p4gitsync.git.commit_metadata import CommitMetadata
from p4gitsync.git.fast_importer import FastImporter
from p4gitsync.p4.p4_change_info import P4ChangeInfo
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.initial_import")


class InitialImporter:
    """전체 히스토리 초기 import (fast-import 기반)."""

    def __init__(
        self,
        p4_client: P4Client,
        state_store: StateStore,
        repo_path: str,
        stream: str,
        config: dict | None = None,
    ) -> None:
        self._p4 = p4_client
        self._state = state_store
        self._repo_path = repo_path
        self._stream = stream
        self._stream_prefix_len = len(stream) + 1

        cfg = config or {}
        self._checkpoint_interval = cfg.get("checkpoint_interval", 1000)
        self._server_load_threshold = cfg.get("server_load_threshold", 50)
        self._throttle_wait_seconds = cfg.get("throttle_wait_seconds", 60)

    def run(self, branch: str) -> None:
        """전체 히스토리 import 실행 (재개 지원)."""
        last_cl = self._state.get_last_synced_cl(self._stream)
        all_changes = self._p4.get_changes_after(self._stream, last_cl)

        if not all_changes:
            logger.info("import 대상 CL 없음 (stream=%s)", self._stream)
            return

        logger.info(
            "초기 import 시작: stream=%s, 대상 CL=%d건, 재개 시점=CL %d",
            self._stream, len(all_changes), last_cl,
        )

        fast_importer = FastImporter(self._repo_path)
        fast_importer.start()

        try:
            for i, cl in enumerate(all_changes):
                self._throttle_if_needed()

                info = self._p4.describe(cl)
                files, deletes = self._extract_files(info)
                name, email = self._state.get_git_author(info.user)

                metadata = CommitMetadata(
                    author_name=name,
                    author_email=email,
                    author_timestamp=info.timestamp,
                    message=info.description,
                    p4_changelist=cl,
                )
                mark = fast_importer.add_commit(branch, metadata, files, deletes)

                if (i + 1) % self._checkpoint_interval == 0:
                    fast_importer.checkpoint()
                    self._state.set_last_synced_cl(
                        self._stream, cl, f"fast-import:mark:{mark}"
                    )
                    self._state.record_commit(
                        cl, f"fast-import:mark:{mark}", self._stream, branch
                    )
                    logger.info(
                        "체크포인트: CL %d (%d/%d)", cl, i + 1, len(all_changes)
                    )

                if (i + 1) % 100 == 0:
                    logger.info("진행: %d/%d CL 처리", i + 1, len(all_changes))

        finally:
            fast_importer.finish()

        self._post_import(branch, all_changes)
        logger.info("초기 import 완료: %d CL 처리", len(all_changes))

    def _extract_files(
        self, info: P4ChangeInfo,
    ) -> tuple[list[tuple[str, bytes]], list[str]]:
        files: list[tuple[str, bytes]] = []
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
                    files.append((git_path, content))

        return files, deletes

    def _depot_to_git_path(self, depot_path: str) -> str | None:
        if not depot_path.startswith(self._stream + "/"):
            return None
        return depot_path[self._stream_prefix_len:]

    def _throttle_if_needed(self) -> None:
        """P4 서버 과부하 시 대기."""
        try:
            if self._p4.check_server_load(self._server_load_threshold):
                logger.warning(
                    "P4 서버 과부하 감지. %d초 대기.", self._throttle_wait_seconds
                )
                time.sleep(self._throttle_wait_seconds)
        except Exception:
            pass

    def _post_import(self, branch: str, all_changes: list[int]) -> None:
        """import 완료 후 Git SHA 매핑 업데이트."""
        result = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{branch}"],
            cwd=self._repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            head_sha = result.stdout.strip()
            last_cl = all_changes[-1]
            self._state.set_last_synced_cl(self._stream, last_cl, head_sha)
            self._state.record_commit(last_cl, head_sha, self._stream, branch)
            logger.info("import 후 HEAD: %s (CL %d)", head_sha[:8], last_cl)

        subprocess.run(
            ["git", "gc"],
            cwd=self._repo_path,
            capture_output=True,
        )
        logger.info("import 후 git gc 완료")
