import logging
import os
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence

import pygit2

from p4gitsync.git.commit_metadata import CommitMetadata

logger = logging.getLogger("p4gitsync.git.pygit2")

_GC_WEEKLY_SECONDS = 7 * 24 * 3600
_LOOSE_OBJECT_THRESHOLD = 10000


@dataclass
class _TreeNode:
    """prefix별 사전 분류된 tree 변경 정보."""
    blobs: dict[str, pygit2.Oid] = field(default_factory=dict)
    delete_names: set[str] = field(default_factory=set)
    children: dict[str, "_TreeNode"] = field(default_factory=dict)

    @staticmethod
    def build(
        path_blobs: dict[str, pygit2.Oid],
        delete_set: set[str],
    ) -> "_TreeNode":
        """전체 경로 목록을 한 번 순회하여 트리 구조로 분류. O(N)."""
        root = _TreeNode()
        for path, blob_oid in path_blobs.items():
            parts = path.split("/")
            node = root
            for part in parts[:-1]:
                if part not in node.children:
                    node.children[part] = _TreeNode()
                node = node.children[part]
            node.blobs[parts[-1]] = blob_oid

        for path in delete_set:
            parts = path.split("/")
            node = root
            for part in parts[:-1]:
                if part not in node.children:
                    node.children[part] = _TreeNode()
                node = node.children[part]
            node.delete_names.add(parts[-1])

        return root


class Pygit2GitOperator:
    """pygit2 기반 Git 조작 구현."""

    def __init__(self, repo_path: str, remote_url: str, bare: bool = False) -> None:
        self._repo_path = repo_path
        self._remote_url = remote_url
        self._bare = bare
        self._repo: pygit2.Repository | None = None
        self._commit_count = 0
        self._last_gc_time: float = 0.0
        self._last_repack_time: float = 0.0

    def init_repo(self) -> None:
        if os.path.exists(os.path.join(self._repo_path, ".git")):
            self._repo = pygit2.Repository(self._repo_path)
        elif os.path.exists(self._repo_path) and os.path.exists(
            os.path.join(self._repo_path, "HEAD")
        ):
            self._repo = pygit2.Repository(self._repo_path)
        else:
            os.makedirs(self._repo_path, exist_ok=True)
            self._repo = pygit2.init_repository(self._repo_path, bare=self._bare)

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

    def create_branch(self, branch: str, start_sha: str) -> None:
        """지정 commit에서 새 branch 생성."""
        ref_name = f"refs/heads/{branch}"
        existing = self._repo.references.get(ref_name)
        if existing is not None:
            logger.info("branch '%s' 이미 존재 — 건너뜀", branch)
            return
        target = pygit2.Oid(hex=start_sha)
        self._repo.references.create(ref_name, target)
        logger.info("branch 생성: %s (from %s)", branch, start_sha[:12])

    def create_orphan_branch(self, branch: str) -> None:
        """orphan branch는 첫 commit 시 자동 생성되므로 별도 작업 불필요."""
        logger.info("orphan branch '%s' 예약 (첫 commit 시 생성)", branch)

    def push(self, branch: str, lfs_enabled: bool = False) -> None:
        """push는 항상 git CLI 사용 (pygit2의 인증 복잡성 회피)."""
        if not self._remote_url:
            logger.debug("remote_url 미설정 — push 건너뜀: %s", branch)
            return
        if lfs_enabled:
            lfs_result = subprocess.run(
                ["git", "lfs", "push", "--all", "origin", branch],
                cwd=self._repo_path, capture_output=True, text=True,
            )
            if lfs_result.returncode != 0:
                raise RuntimeError(f"git lfs push 실패: {lfs_result.stderr}")
            logger.info("LFS push 완료: %s", branch)
        result = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=self._repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git push 실패: {result.stderr}")
        logger.info("push 완료: %s", branch)

    def fetch(self, remote: str = "origin") -> None:
        result = subprocess.run(
            ["git", "fetch", remote],
            cwd=self._repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git fetch 실패: {result.stderr}")
        # fetch 후 repo 다시 열기 (새 refs 반영)
        self._repo = pygit2.Repository(self._repo_path)

    def get_log_after(self, branch: str, after_sha: str | None, remote: str = "origin") -> list[dict]:
        if after_sha:
            range_spec = f"{after_sha}..{remote}/{branch}"
        else:
            range_spec = f"{remote}/{branch}"
        result = subprocess.run(
            ["git", "log", range_spec, "--reverse", "--format=%H%n%an%n%ae%n%at%n%P%n%B%x00"],
            cwd=self._repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        commits = []
        for entry in result.stdout.split("\x00"):
            entry = entry.strip()
            if not entry:
                continue
            lines = entry.split("\n", 5)
            if len(lines) < 6:
                continue
            commits.append({
                "sha": lines[0],
                "author_name": lines[1],
                "author_email": lines[2],
                "timestamp": int(lines[3]),
                "parents": lines[4].split() if lines[4] else [],
                "message": lines[5].strip(),
            })
        return commits

    def get_commit_files(self, commit_sha: str) -> tuple[list[tuple[str, bytes]], list[str]]:
        # diff-tree로 변경 파일 목록
        result = subprocess.run(
            ["git", "diff-tree", "-r", "--no-commit-id", "--name-status", commit_sha],
            cwd=self._repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git diff-tree 실패: {result.stderr}")

        file_changes = []
        deletes = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            status, path = parts[0], parts[1]
            if status == "D":
                deletes.append(path)
            else:
                # A, M, etc — 파일 내용 추출
                content_result = subprocess.run(
                    ["git", "show", f"{commit_sha}:{path}"],
                    cwd=self._repo_path,
                    capture_output=True,
                )
                if content_result.returncode == 0:
                    file_changes.append((path, content_result.stdout))
        return file_changes, deletes

    def delete_branch(self, branch: str) -> None:
        ref_name = f"refs/heads/{branch}"
        try:
            ref = self._repo.references.get(ref_name)
            if ref is not None:
                ref.delete()
                logger.info("branch 삭제: %s", branch)
        except Exception as e:
            logger.warning("branch 삭제 실패: %s — %s", branch, e)

    def list_remote_branches(self, remote: str = "origin", prefix: str = "") -> list[str]:
        result = subprocess.run(
            ["git", "branch", "-r", "--format=%(refname:short)"],
            cwd=self._repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        branches = []
        remote_prefix = f"{remote}/"
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith(remote_prefix):
                name = line[len(remote_prefix):]
                if not prefix or name.startswith(prefix):
                    branches.append(name)
        return branches

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
        """시간 기반 gc + loose object 임계값 기반 즉시 gc."""
        now = time.monotonic()

        # loose object 수 초과 시 즉시 gc
        if self._count_loose_objects() > _LOOSE_OBJECT_THRESHOLD:
            self._run_gc(now)
            return

        # 주 1회 시간 기반 gc
        if now - self._last_gc_time > _GC_WEEKLY_SECONDS:
            self._run_gc(now)
            return

        # 주 1회 repack
        if now - self._last_repack_time > _GC_WEEKLY_SECONDS:
            self._run_repack(now)

    def _run_gc(self, now: float) -> None:
        subprocess.run(
            ["git", "gc", "--auto"],
            cwd=self._repo_path,
            capture_output=True,
        )
        self._repo = pygit2.Repository(self._repo_path)
        self._last_gc_time = now
        logger.info("git gc 실행 완료 (commit count: %d)", self._commit_count)

    def _run_repack(self, now: float) -> None:
        subprocess.run(
            ["git", "repack", "-a", "-d"],
            cwd=self._repo_path,
            capture_output=True,
        )
        self._repo = pygit2.Repository(self._repo_path)
        self._last_repack_time = now
        logger.info("git repack 실행 완료")

    def _count_loose_objects(self) -> int:
        """objects 디렉토리의 loose object 수를 추정."""
        objects_dir = os.path.join(self._repo_path, ".git", "objects")
        if not os.path.isdir(objects_dir):
            objects_dir = os.path.join(self._repo_path, "objects")
        if not os.path.isdir(objects_dir):
            return 0
        count = 0
        for d in os.listdir(objects_dir):
            if len(d) == 2 and d != "pa" and d != "in":
                subdir = os.path.join(objects_dir, d)
                if os.path.isdir(subdir):
                    count += len(os.listdir(subdir))
        return count

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

        # O(N) 사전 분류: 전체 경로를 한 번만 순회
        tree_node = _TreeNode.build(path_blobs, delete_set)
        return self._rebuild_tree(prev_tree, tree_node)

    def _rebuild_tree(
        self,
        prev_tree: pygit2.Tree | None,
        node: _TreeNode,
    ) -> pygit2.Oid:
        """prev_tree를 기반으로 사전 분류된 변경/삭제를 적용하여 새 Tree 구성.

        _TreeNode에 이미 레벨별로 분류되어 있으므로 각 레벨에서 O(entries) 처리.
        """
        tb = self._repo.TreeBuilder()
        processed_names: set[str] = set()

        if prev_tree is not None:
            for entry in prev_tree:
                name = entry.name

                if entry.type_str == "blob":
                    if name in node.delete_names:
                        continue
                    if name in node.blobs:
                        tb.insert(name, node.blobs[name], pygit2.GIT_FILEMODE_BLOB)
                        processed_names.add(name)
                    else:
                        tb.insert(name, entry.id, entry.filemode)
                elif entry.type_str == "tree":
                    if name in node.children:
                        child_tree = self._repo.get(entry.id)
                        new_subtree = self._rebuild_tree(
                            child_tree, node.children[name],
                        )
                        tree_obj = self._repo.get(new_subtree)
                        if len(tree_obj) > 0:
                            tb.insert(name, new_subtree, pygit2.GIT_FILEMODE_TREE)
                        processed_names.add(name)
                    else:
                        tb.insert(name, entry.id, entry.filemode)

        # 새 blob 추가
        for name, blob_oid in node.blobs.items():
            if name not in processed_names:
                tb.insert(name, blob_oid, pygit2.GIT_FILEMODE_BLOB)

        # 새 디렉토리 추가
        for dir_name, child_node in node.children.items():
            if dir_name not in processed_names:
                new_subtree = self._rebuild_tree(None, child_node)
                tree_obj = self._repo.get(new_subtree)
                if len(tree_obj) > 0:
                    tb.insert(dir_name, new_subtree, pygit2.GIT_FILEMODE_TREE)

        return tb.write()
