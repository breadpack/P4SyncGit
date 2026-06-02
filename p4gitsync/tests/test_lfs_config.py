"""LfsConfig 신규 필드/메서드 + 하위호환 테스트."""

from __future__ import annotations

from p4gitsync.config.lfs_config import LfsConfig


class TestLfsConfigDefaults:
    def test_new_fields_default(self):
        cfg = LfsConfig()
        # 기본은 비활성(확장자 전용, 기존 동작 유지)
        assert cfg.auto_detect_binary is False
        assert cfg.binary_size_threshold_bytes is None

    def test_effective_threshold_falls_back(self):
        cfg = LfsConfig(size_threshold_bytes=1234)
        assert cfg.effective_binary_threshold == 1234

    def test_effective_threshold_override(self):
        cfg = LfsConfig(size_threshold_bytes=1234, binary_size_threshold_bytes=9999)
        assert cfg.effective_binary_threshold == 9999


class TestIsLfsTargetUnchanged:
    def test_ext_match(self):
        cfg = LfsConfig(extensions=[".png", ".uasset"])
        assert cfg.is_lfs_target("a/b/hero.png") is True
        assert cfg.is_lfs_target("a/b/code.cs") is False

    def test_ext_alias_matches(self):
        cfg = LfsConfig(extensions=[".png"])
        # is_lfs_target_ext 와 is_lfs_target 은 동일 동작
        assert cfg.is_lfs_target_ext("x.png") == cfg.is_lfs_target("x.png")
        assert cfg.is_lfs_target_ext("x.txt") == cfg.is_lfs_target("x.txt")


class TestRouteToLfsDelegation:
    def test_ext_match(self):
        cfg = LfsConfig(extensions=[".png"], auto_detect_binary=True)
        assert cfg.route_to_lfs(git_path="a.png", file_type="text", size=1) is True

    def test_binary_threshold(self):
        cfg = LfsConfig(
            extensions=[], auto_detect_binary=True, size_threshold_bytes=1000,
        )
        assert cfg.route_to_lfs(git_path="blob", file_type="binary", size=2000) is True
        assert cfg.route_to_lfs(git_path="blob", file_type="binary", size=500) is False

    def test_disabled_ext_only(self):
        cfg = LfsConfig(extensions=[".png"], auto_detect_binary=False)
        assert cfg.route_to_lfs(git_path="blob", file_type="binary", size=10**9) is False
