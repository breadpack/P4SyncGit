import os
import subprocess
import tempfile

import pygit2
import pytest

from p4gitsync.git.commit_metadata import CommitMetadata
from p4gitsync.git.pygit2_git_operator import Pygit2GitOperator
from p4gitsync.git.git_cli_operator import GitCliOperator


def _make_metadata(cl: int = 1, msg: str = "test commit") -> CommitMetadata:
    return CommitMetadata(
        author_name="Test User",
        author_email="test@example.com",
        author_timestamp=1700000000 + cl,
        message=msg,
        p4_changelist=cl,
    )


class TestPygit2GitOperator:
    @pytest.fixture
    def repo_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def operator(self, repo_dir):
        op = Pygit2GitOperator(repo_dir, "")
        op.init_repo()
        return op

    def test_init_repo(self, repo_dir):
        op = Pygit2GitOperator(repo_dir, "")
        op.init_repo()
        assert os.path.exists(os.path.join(repo_dir, ".git"))

    def test_create_first_commit(self, operator, repo_dir):
        files = [("hello.txt", b"hello world")]
        sha = operator.create_commit("main", None, _make_metadata(1), files)
        assert len(sha) == 40

        repo = pygit2.Repository(repo_dir)
        commit = repo.get(repo.references.get("refs/heads/main").target)
        assert "[P4CL: 1]" in commit.message

    def test_create_sequential_commits(self, operator):
        sha1 = operator.create_commit(
            "main", None, _make_metadata(1), [("a.txt", b"aaa")]
        )
        sha2 = operator.create_commit(
            "main", sha1, _make_metadata(2), [("b.txt", b"bbb")]
        )
        assert sha1 != sha2

    def test_edit_file(self, operator, repo_dir):
        sha1 = operator.create_commit(
            "main", None, _make_metadata(1), [("a.txt", b"v1")]
        )
        sha2 = operator.create_commit(
            "main", sha1, _make_metadata(2), [("a.txt", b"v2")]
        )
        repo = pygit2.Repository(repo_dir)
        commit = repo.get(pygit2.Oid(hex=sha2))
        blob = commit.tree["a.txt"]
        assert repo.get(blob.id).data == b"v2"

    def test_delete_file(self, operator, repo_dir):
        sha1 = operator.create_commit(
            "main", None, _make_metadata(1),
            [("a.txt", b"aaa"), ("b.txt", b"bbb")],
        )
        sha2 = operator.create_commit(
            "main", sha1, _make_metadata(2), [], deletes=["a.txt"],
        )
        repo = pygit2.Repository(repo_dir)
        commit = repo.get(pygit2.Oid(hex=sha2))
        entries = [e.name for e in commit.tree]
        assert "a.txt" not in entries
        assert "b.txt" in entries

    def test_nested_directory(self, operator, repo_dir):
        files = [
            ("src/main.py", b"print('hello')"),
            ("src/utils/helper.py", b"def help(): pass"),
        ]
        sha = operator.create_commit("main", None, _make_metadata(1), files)
        repo = pygit2.Repository(repo_dir)
        commit = repo.get(pygit2.Oid(hex=sha))
        assert "src" in [e.name for e in commit.tree]

    def test_merge_commit(self, operator, repo_dir):
        sha1 = operator.create_commit(
            "main", None, _make_metadata(1), [("a.txt", b"main")]
        )
        sha2 = operator.create_commit(
            "dev", sha1, _make_metadata(2), [("a.txt", b"dev")]
        )
        sha3 = operator.create_merge_commit(
            "main", [sha1, sha2], _make_metadata(3, "merge dev"),
            [("a.txt", b"merged")],
        )
        repo = pygit2.Repository(repo_dir)
        commit = repo.get(pygit2.Oid(hex=sha3))
        assert len(commit.parents) == 2

    def test_get_head_sha(self, operator):
        assert operator.get_head_sha("main") is None
        sha = operator.create_commit(
            "main", None, _make_metadata(1), [("a.txt", b"test")]
        )
        assert operator.get_head_sha("main") == sha

    def test_commit_count(self, operator):
        assert operator.commit_count == 0
        operator.create_commit(
            "main", None, _make_metadata(1), [("a.txt", b"test")]
        )
        assert operator.commit_count == 1


class TestGitCliOperator:
    @pytest.fixture
    def repo_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def operator(self, repo_dir):
        op = GitCliOperator(repo_dir, "")
        op.init_repo()
        return op

    def test_init_repo(self, repo_dir):
        op = GitCliOperator(repo_dir, "")
        op.init_repo()
        assert os.path.exists(os.path.join(repo_dir, ".git"))

    def test_create_first_commit(self, operator, repo_dir):
        files = [("hello.txt", b"hello world")]
        sha = operator.create_commit("main", None, _make_metadata(1), files)
        assert len(sha) == 40

        result = subprocess.run(
            ["git", "log", "--oneline", "refs/heads/main"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        assert "P4CL: 1" in result.stdout or sha[:7] in result.stdout

    def test_create_sequential_commits(self, operator):
        sha1 = operator.create_commit(
            "main", None, _make_metadata(1), [("a.txt", b"aaa")]
        )
        sha2 = operator.create_commit(
            "main", sha1, _make_metadata(2), [("b.txt", b"bbb")]
        )
        assert sha1 != sha2

    def test_delete_file(self, operator, repo_dir):
        sha1 = operator.create_commit(
            "main", None, _make_metadata(1),
            [("a.txt", b"aaa"), ("b.txt", b"bbb")],
        )
        sha2 = operator.create_commit(
            "main", sha1, _make_metadata(2), [], deletes=["a.txt"],
        )
        result = subprocess.run(
            ["git", "ls-tree", "--name-only", sha2],
            cwd=repo_dir, capture_output=True, text=True,
        )
        assert "a.txt" not in result.stdout
        assert "b.txt" in result.stdout

    def test_get_head_sha(self, operator):
        assert operator.get_head_sha("main") is None
        sha = operator.create_commit(
            "main", None, _make_metadata(1), [("a.txt", b"test")]
        )
        assert operator.get_head_sha("main") == sha
