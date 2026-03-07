import logging
import os
import subprocess
from typing import Sequence

import pygit2

from p4gitsync.git.commit_metadata import CommitMetadata

logger = logging.getLogger("p4gitsync.git.pygit2")


class Pygit2GitOperator:
    """pygit2 기반 Git 조작 구현."""

    def __init__(self, repo_path: str, remote_url: str) -> None:
        self._repo_path = repo_path
        self._remote_url = remote_url
        self._repo: pygit2.Repository | None = None
        self._commit_count = 0

    def init_repo(self) -> None:
        if os.path.exists(os.path.join(self._repo_path, ".git")):
            self._repo = pygit2.Repository(self._repo_path)
        elif os.path.exists(self._repo_path) and os.path.exists(
            os.path.join(self._repo_path, "HEAD")
        ):
            self._repo = pygit2.Repository(self._repo_path)
        else:
            os.makedirs(self._repo_path, exist_ok=True)
            self._repo = pygit2.init_repository(self._repo_path, bare=False)

        if self._remote_url:
            try:
                self._repo.remotes["origin"]
            except KeyError:
                self._repo.remotes.create("origin", self._remote_url)

        logger.info("Git 리포지토리 초기화: %s", self._repo_path)

    def create_commit(
        self,
        branch: str,
        parent_sha: str | None,
        metadata: CommitMetadata,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> str:
        parents = [pygit2.Oid(hex=parent_sha)] if parent_sha else []
        return self._do_commit(branch, parents, metadata, file_changes, deletes)

    def create_merge_commit(
        self,
        branch: str,
        parent_shas: Sequence[str],
        metadata: CommitMetadata,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> str:
        parents = [pygit2.Oid(hex=sha) for sha in parent_shas]
        return self._do_commit(branch, parents, metadata, file_changes, deletes)

    def push(self, branch: str) -> None:
        """push는 항상 git CLI 사용 (pygit2의 인증 복잡성 회피)."""
        result = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=self._repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git push 실패: {result.stderr}")
        logger.info("push 완료: %s", branch)

    def get_head_sha(self, branch: str) -> str | None:
        ref_name = f"refs/heads/{branch}"
        try:
            ref = self._repo.references.get(ref_name)
            if ref is None:
                return None
            return str(ref.target)
        except Exception:
            return None

    def maybe_run_gc(self, gc_interval: int) -> None:
        if self._commit_count > 0 and self._commit_count % gc_interval == 0:
            subprocess.run(
                ["git", "gc", "--auto"],
                cwd=self._repo_path,
                check=True,
                capture_output=True,
            )
            self._repo = pygit2.Repository(self._repo_path)
            logger.info("git gc 실행 완료 (commit count: %d)", self._commit_count)

    @property
    def commit_count(self) -> int:
        return self._commit_count

    def _do_commit(
        self,
        branch: str,
        parents: list[pygit2.Oid],
        metadata: CommitMetadata,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> str:
        tree_oid = self._build_tree_incremental(branch, file_changes, deletes)

        author = pygit2.Signature(
            metadata.author_name,
            metadata.author_email,
            metadata.author_timestamp,
        )
        committer = author

        message = metadata.format_message()
        ref = f"refs/heads/{branch}"

        oid = self._repo.create_commit(
            ref, author, committer, message, tree_oid, parents
        )
        self._commit_count += 1
        return str(oid)

    def _build_tree_incremental(
        self,
        branch: str,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> pygit2.Oid:
        """이전 commit의 tree를 기반으로 변경분만 적용하여 새 tree 생성."""
        ref_name = f"refs/heads/{branch}"
        prev_tree = None
        try:
            ref = self._repo.references.get(ref_name)
            if ref is not None:
                commit = self._repo.get(ref.target)
                prev_tree = commit.tree
        except Exception:
            pass

        path_blobs: dict[str, pygit2.Oid] = {}
        for path, content in file_changes:
            blob_oid = self._repo.create_blob(content)
            path_blobs[path] = blob_oid

        delete_set = set(deletes) if deletes else set()

        return self._rebuild_tree(prev_tree, path_blobs, delete_set, "")

    def _rebuild_tree(
        self,
        prev_tree: pygit2.Tree | None,
        path_blobs: dict[str, pygit2.Oid],
        delete_set: set[str],
        prefix: str,
    ) -> pygit2.Oid:
        """prev_tree를 기반으로 변경/삭제를 적용한 새 Tree 구성.

        1. 이 prefix 아래의 직접 파일(blob)과 하위 디렉토리(tree)를 분류
        2. prev_tree의 기존 항목을 유지/수정/삭제
        3. 새로운 파일/디렉토리 추가
        """
        # 이 레벨에서 직접 추가/수정할 blob과 재귀할 하위 디렉토리 분류
        direct_blobs: dict[str, pygit2.Oid] = {}
        child_dirs: set[str] = set()

        for path, blob_oid in path_blobs.items():
            if not path.startswith(prefix):
                continue
            relative = path[len(prefix):]
            parts = relative.split("/")
            if len(parts) == 1:
                direct_blobs[parts[0]] = blob_oid
            else:
                child_dirs.add(parts[0])

        delete_names: set[str] = set()
        delete_child_dirs: set[str] = set()
        for path in delete_set:
            if not path.startswith(prefix):
                continue
            relative = path[len(prefix):]
            parts = relative.split("/")
            if len(parts) == 1:
                delete_names.add(parts[0])
            else:
                delete_child_dirs.add(parts[0])

        tb = self._repo.TreeBuilder()
        processed_names: set[str] = set()

        # 기존 tree의 항목 처리
        if prev_tree is not None:
            for entry in prev_tree:
                name = entry.name

                if entry.type_str == "blob":
                    if name in delete_names:
                        continue
                    if name in direct_blobs:
                        tb.insert(name, direct_blobs[name], pygit2.GIT_FILEMODE_BLOB)
                        processed_names.add(name)
                    else:
                        tb.insert(name, entry.id, entry.filemode)
                elif entry.type_str == "tree":
                    if name in child_dirs or name in delete_child_dirs:
                        child_tree = self._repo.get(entry.id)
                        child_prefix = f"{prefix}{name}/"
                        new_subtree = self._rebuild_tree(
                            child_tree, path_blobs, delete_set, child_prefix
                        )
                        tree_obj = self._repo.get(new_subtree)
                        if len(tree_obj) > 0:
                            tb.insert(name, new_subtree, pygit2.GIT_FILEMODE_TREE)
                        processed_names.add(name)
                    else:
                        tb.insert(name, entry.id, entry.filemode)

        # 새 blob 추가 (prev_tree에 없던 것)
        for name, blob_oid in direct_blobs.items():
            if name not in processed_names:
                tb.insert(name, blob_oid, pygit2.GIT_FILEMODE_BLOB)

        # 새 디렉토리 추가 (prev_tree에 없던 것)
        for dir_name in child_dirs:
            if dir_name not in processed_names:
                child_prefix = f"{prefix}{dir_name}/"
                new_subtree = self._rebuild_tree(
                    None, path_blobs, delete_set, child_prefix
                )
                tree_obj = self._repo.get(new_subtree)
                if len(tree_obj) > 0:
                    tb.insert(dir_name, new_subtree, pygit2.GIT_FILEMODE_TREE)

        return tb.write()
