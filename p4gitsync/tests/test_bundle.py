"""bundle 명령 테스트 — 실제 git repo 에서 번들 생성."""

import os
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from p4gitsync.__main__ import _run_bundle
from p4gitsync.config.sync_config import (
    AppConfig,
    GitConfig,
    P4Config,
    StateConfig,
)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo_with_commit():
    with tempfile.TemporaryDirectory() as d:
        _git(["init", "-q", "-b", "main"], d)
        _git(["config", "user.email", "t@e.com"], d)
        _git(["config", "user.name", "tester"], d)
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("hello")
        _git(["add", "-A"], d)
        _git(["commit", "-qm", "init"], d)
        yield d


def _config(repo):
    return AppConfig(
        p4=P4Config(),
        git=GitConfig(repo_path=repo, default_branch="main"),
        state=StateConfig(db_path=":memory:"),
    )


class TestBundle:
    def test_creates_bundle_file(self, repo_with_commit):
        out = os.path.join(repo_with_commit, "out.bundle")
        args = SimpleNamespace(output=out, all=False)
        _run_bundle(_config(repo_with_commit), args)
        assert os.path.exists(out)
        assert os.path.getsize(out) > 0
        # git 이 유효한 번들로 인식하는지
        verify = subprocess.run(
            ["git", "bundle", "verify", out],
            cwd=repo_with_commit, capture_output=True, text=True,
        )
        assert verify.returncode == 0

    def test_all_refs(self, repo_with_commit):
        out = os.path.join(repo_with_commit, "all.bundle")
        args = SimpleNamespace(output=out, all=True)
        _run_bundle(_config(repo_with_commit), args)
        assert os.path.exists(out)

    def test_failure_exits(self, repo_with_commit):
        # 존재하지 않는 ref → git bundle 실패 → SystemExit
        out = os.path.join(repo_with_commit, "x.bundle")
        cfg = _config(repo_with_commit)
        cfg.git.default_branch = "nonexistent-branch"
        args = SimpleNamespace(output=out, all=False)
        with pytest.raises(SystemExit):
            _run_bundle(cfg, args)
