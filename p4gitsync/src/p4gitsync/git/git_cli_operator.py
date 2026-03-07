import logging
import os
import subprocess
import tempfile
from typing import Sequence

from p4gitsync.git.commit_metadata import CommitMetadata

logger = logging.getLogger("p4gitsync.git.cli")


class GitCliOperator:
    """Git CLI 기반 구현 (pygit2 호환성 문제 시 동등한 대안)."""

    def __init__(self, repo_path: str, remote_url: str) -> None:
        self._repo_path = repo_path
        self._remote_url = remote_url
        self._commit_count = 0

    def _run_git(self, *args: str, input_data: bytes | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self._repo_path,
            capture_output=True,
            input=input_data,
        )

    def init_repo(self) -> None:
        if not os.path.exists(self._repo_path):
            os.makedirs(self._repo_path, exist_ok=True)

        git_dir = os.path.join(self._repo_path, ".git")
        if not os.path.exists(git_dir):
            self._run_git("init")

        if self._remote_url:
            result = self._run_git("remote", "get-url", "origin")
            if result.returncode != 0:
                self._run_git("remote", "add", "origin", self._remote_url)

        logger.info("Git 리포지토리 초기화 (CLI): %s", self._repo_path)

    def create_commit(
        self,
        branch: str,
        parent_sha: str | None,
        metadata: CommitMetadata,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> str:
        parents = [parent_sha] if parent_sha else []
        return self._do_commit(branch, parents, metadata, file_changes, deletes)

    def create_merge_commit(
        self,
        branch: str,
        parent_shas: Sequence[str],
        metadata: CommitMetadata,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> str:
        return self._do_commit(branch, list(parent_shas), metadata, file_changes, deletes)

    def push(self, branch: str) -> None:
        result = self._run_git("push", "origin", branch)
        if result.returncode != 0:
            raise RuntimeError(f"git push 실패: {result.stderr.decode()}")
        logger.info("push 완료: %s", branch)

    def get_head_sha(self, branch: str) -> str | None:
        result = self._run_git("rev-parse", f"refs/heads/{branch}")
        if result.returncode != 0:
            return None
        return result.stdout.decode().strip()

    def maybe_run_gc(self, gc_interval: int) -> None:
        if self._commit_count > 0 and self._commit_count % gc_interval == 0:
            self._run_git("gc", "--auto")
            logger.info("git gc 실행 완료 (commit count: %d)", self._commit_count)

    @property
    def commit_count(self) -> int:
        return self._commit_count

    def _do_commit(
        self,
        branch: str,
        parent_shas: list[str],
        metadata: CommitMetadata,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> str:
        tree_sha = self._build_tree(parent_shas, file_changes, deletes)
        message = metadata.format_message()

        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = metadata.author_name
        env["GIT_AUTHOR_EMAIL"] = metadata.author_email
        env["GIT_AUTHOR_DATE"] = f"{metadata.author_timestamp} +0000"
        env["GIT_COMMITTER_NAME"] = metadata.author_name
        env["GIT_COMMITTER_EMAIL"] = metadata.author_email
        env["GIT_COMMITTER_DATE"] = f"{metadata.author_timestamp} +0000"

        cmd = ["git", "commit-tree", tree_sha]
        for p in parent_shas:
            cmd.extend(["-p", p])
        cmd.extend(["-m", message])

        result = subprocess.run(
            cmd,
            cwd=self._repo_path,
            capture_output=True,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git commit-tree 실패: {result.stderr.decode()}")

        commit_sha = result.stdout.decode().strip()

        self._run_git("update-ref", f"refs/heads/{branch}", commit_sha)
        self._commit_count += 1
        return commit_sha

    def _build_tree(
        self,
        parent_shas: list[str],
        file_changes: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> str:
        """mktree를 사용하여 tree 생성. 이전 tree 기반으로 incremental 적용."""
        existing: dict[str, tuple[str, str]] = {}

        if parent_shas:
            result = self._run_git("ls-tree", "-r", parent_shas[0])
            if result.returncode == 0:
                for line in result.stdout.decode().splitlines():
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        meta, path = parts
                        mode, obj_type, sha = meta.split()
                        existing[path] = (mode, sha)

        delete_set = set(deletes) if deletes else set()
        for path in delete_set:
            existing.pop(path, None)

        for path, content in file_changes:
            result = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=self._repo_path,
                input=content,
                capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"git hash-object 실패: {result.stderr.decode()}")
            blob_sha = result.stdout.decode().strip()
            existing[path] = ("100644", blob_sha)

        tree_input = ""
        for path, (mode, sha) in sorted(existing.items()):
            tree_input += f"{mode} blob {sha}\t{path}\n"

        result = subprocess.run(
            ["git", "mktree", "--missing"],
            cwd=self._repo_path,
            input=tree_input.encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git mktree 실패: {result.stderr.decode()}")

        return result.stdout.decode().strip()
