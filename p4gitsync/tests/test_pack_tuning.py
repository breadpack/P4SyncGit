"""pack_tuning 테스트 — 순수 빌더/렌더 + 실제 git repo 적용."""

import subprocess
import tempfile

import pytest

from p4gitsync.config.sync_config import PackTuningConfig
from p4gitsync.git import pack_tuning


class TestBuildPackConfig:
    def test_disabled_returns_empty(self):
        assert pack_tuning.build_pack_config(PackTuningConfig(enabled=False)) == []

    def test_core_keys_present(self):
        settings = dict(pack_tuning.build_pack_config(PackTuningConfig()))
        assert settings["core.bigFileThreshold"] == "512m"
        assert settings["pack.packSizeLimit"] == "2g"
        assert settings["pack.windowMemory"] == "1g"
        assert settings["pack.threads"] == "0"

    def test_bitmaps_toggle(self):
        on = dict(pack_tuning.build_pack_config(PackTuningConfig(write_bitmaps=True)))
        assert on["pack.writeBitmaps"] == "true"
        off = dict(pack_tuning.build_pack_config(PackTuningConfig(write_bitmaps=False)))
        assert "pack.writeBitmaps" not in off

    def test_commit_graph_toggle(self):
        off = dict(pack_tuning.build_pack_config(PackTuningConfig(commit_graph=False)))
        assert "core.commitGraph" not in off
        assert "fetch.writeCommitGraph" not in off

    def test_index_version_omitted_when_zero(self):
        s = dict(pack_tuning.build_pack_config(PackTuningConfig(index_version=0)))
        assert "index.version" not in s

    def test_custom_values(self):
        cfg = PackTuningConfig(big_file_threshold="256m", window=20, depth=100)
        s = dict(pack_tuning.build_pack_config(cfg))
        assert s["core.bigFileThreshold"] == "256m"
        assert s["pack.window"] == "20"
        assert s["pack.depth"] == "100"

    def test_serve_partial_clone_toggle(self):
        on = dict(pack_tuning.build_pack_config(PackTuningConfig(serve_partial_clone=True)))
        assert on["uploadpack.allowFilter"] == "true"
        assert on["uploadpack.allowAnySHA1InWant"] == "true"
        off = dict(pack_tuning.build_pack_config(PackTuningConfig(serve_partial_clone=False)))
        assert "uploadpack.allowFilter" not in off


class TestRenderGitconfig:
    def test_empty(self):
        assert pack_tuning.render_gitconfig([]) == ""

    def test_groups_by_section(self):
        out = pack_tuning.render_gitconfig([
            ("core.bigFileThreshold", "512m"),
            ("pack.window", "10"),
            ("pack.depth", "50"),
        ])
        assert "[core]" in out
        assert "\tbigFileThreshold = 512m" in out
        assert "[pack]" in out
        assert "\twindow = 10" in out
        # pack 섹션 헤더는 한 번만
        assert out.count("[pack]") == 1


@pytest.fixture
def git_repo():
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q"], cwd=d, check=True, capture_output=True)
        yield d


def _git_config_get(repo, key):
    r = subprocess.run(
        ["git", "config", "--get", key],
        cwd=repo, capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


class TestApplyToRepo:
    def test_applies_settings(self, git_repo):
        settings = pack_tuning.build_pack_config(PackTuningConfig())
        applied = pack_tuning.apply_to_repo(git_repo, settings)
        assert len(applied) == len(settings)
        assert _git_config_get(git_repo, "core.bigFileThreshold") == "512m"
        assert _git_config_get(git_repo, "pack.packSizeLimit") == "2g"
        assert _git_config_get(git_repo, "pack.writeBitmaps") == "true"


class TestEnsureRepoTuned:
    def test_disabled_noop(self, git_repo):
        applied = pack_tuning.ensure_repo_tuned(
            git_repo, PackTuningConfig(enabled=False),
        )
        assert applied == []
        assert _git_config_get(git_repo, "core.bigFileThreshold") is None

    def test_existing_repo_tuned(self, git_repo):
        applied = pack_tuning.ensure_repo_tuned(git_repo, PackTuningConfig())
        assert applied
        assert _git_config_get(git_repo, "index.version") == "4"

    def test_inits_missing_repo(self):
        with tempfile.TemporaryDirectory() as d:
            import os
            target = os.path.join(d, "new_repo")
            applied = pack_tuning.ensure_repo_tuned(target, PackTuningConfig())
            assert applied
            assert os.path.exists(os.path.join(target, ".git"))
            assert _git_config_get(target, "pack.windowMemory") == "1g"
