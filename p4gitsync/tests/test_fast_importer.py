import os
import subprocess
import tempfile

import pytest

from p4gitsync.git.commit_metadata import CommitMetadata
from p4gitsync.git.fast_importer import FastImporter


def _make_metadata(cl: int, msg: str = "test") -> CommitMetadata:
    return CommitMetadata(
        author_name="Test User",
        author_email="test@example.com",
        author_timestamp=1700000000 + cl,
        message=msg,
        p4_changelist=cl,
    )


class TestFastImporter:
    @pytest.fixture
    def repo_dir(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", d], capture_output=True, check=True)
            yield d

    def test_single_commit(self, repo_dir):
        fi = FastImporter(repo_dir)
        fi.start()
        fi.add_commit(
            "main",
            _make_metadata(1, "first commit"),
            [("hello.txt", b"hello world")],
        )
        fi.finish()

        result = subprocess.run(
            ["git", "log", "--oneline", "refs/heads/main"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "P4CL: 1" in result.stdout or "first commit" in result.stdout

    def test_multiple_commits(self, repo_dir):
        fi = FastImporter(repo_dir)
        fi.start()
        for i in range(10):
            fi.add_commit(
                "main",
                _make_metadata(i + 1, f"commit {i + 1}"),
                [(f"file{i}.txt", f"content {i}".encode())],
            )
        fi.finish()

        result = subprocess.run(
            ["git", "rev-list", "--count", "refs/heads/main"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "10"

    def test_delete_file(self, repo_dir):
        fi = FastImporter(repo_dir)
        fi.start()
        fi.add_commit(
            "main",
            _make_metadata(1),
            [("a.txt", b"aaa"), ("b.txt", b"bbb")],
        )
        fi.add_commit(
            "main",
            _make_metadata(2),
            [],
            deletes=["a.txt"],
        )
        fi.finish()

        result = subprocess.run(
            ["git", "ls-tree", "--name-only", "refs/heads/main"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        assert "a.txt" not in result.stdout
        assert "b.txt" in result.stdout

    def test_checkpoint(self, repo_dir):
        fi = FastImporter(repo_dir)
        fi.start()
        fi.add_commit(
            "main",
            _make_metadata(1),
            [("a.txt", b"content")],
        )
        fi.checkpoint()
        fi.add_commit(
            "main",
            _make_metadata(2),
            [("b.txt", b"content2")],
        )
        fi.finish()

        result = subprocess.run(
            ["git", "rev-list", "--count", "refs/heads/main"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "2"

    def test_mark_counter(self, repo_dir):
        fi = FastImporter(repo_dir)
        fi.start()
        mark1 = fi.add_commit("main", _make_metadata(1), [("a.txt", b"a")])
        mark2 = fi.add_commit("main", _make_metadata(2), [("b.txt", b"b")])
        fi.finish()
        assert mark1 == 1
        assert mark2 == 2
        assert fi.current_mark == 2

    def test_file_modes_preserved(self, repo_dir):
        """일반/실행/심링크 모드가 실제 git tree에 보존되는지 검증."""
        from p4gitsync.git.file_mode import (
            GIT_MODE_EXEC,
            GIT_MODE_FILE,
            GIT_MODE_SYMLINK,
        )

        fi = FastImporter(repo_dir)
        fi.start()
        fi.add_commit(
            "main",
            _make_metadata(1),
            [
                ("normal.txt", b"plain", GIT_MODE_FILE),
                ("run.sh", b"#!/bin/sh\necho hi\n", GIT_MODE_EXEC),
                ("link", b"normal.txt", GIT_MODE_SYMLINK),
            ],
        )
        fi.finish()

        result = subprocess.run(
            ["git", "ls-tree", "refs/heads/main"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        modes = {}
        for line in result.stdout.strip().splitlines():
            meta, path = line.split("\t", 1)
            mode = meta.split()[0]
            modes[path] = mode
        assert modes["normal.txt"] == "100644"
        assert modes["run.sh"] == "100755"
        assert modes["link"] == "120000"

    def test_unicode_content(self, repo_dir):
        fi = FastImporter(repo_dir)
        fi.start()
        fi.add_commit(
            "main",
            _make_metadata(1, "한글 커밋 메시지"),
            [("readme.txt", "한글 내용입니다".encode("utf-8"))],
        )
        fi.finish()

        result = subprocess.run(
            ["git", "show", "refs/heads/main:readme.txt"],
            cwd=repo_dir, capture_output=True,
        )
        assert "한글 내용입니다".encode("utf-8") in result.stdout
