"""lfs_routing 순수 판정 함수 + partition_for_lfs 단위 테스트."""

from __future__ import annotations

import pytest

from p4gitsync.config.lfs_config import LfsConfig
from p4gitsync.lfs.lfs_routing import (
    P4TypeClass,
    classify_p4_file_type,
    decide_lfs_route,
    partition_for_lfs,
    sniff_is_binary,
)
from p4gitsync.p4.p4_file_action import P4FileAction


# ── classify_p4_file_type ────────────────────────────────────


class TestClassifyP4FileType:
    @pytest.mark.parametrize(
        "ft",
        ["text", "unicode", "utf8", "utf16", "symlink", "text+x", "xtext", "text+lx"],
    )
    def test_text_family(self, ft: str):
        assert classify_p4_file_type(ft) is P4TypeClass.TEXT

    @pytest.mark.parametrize(
        "ft",
        ["binary", "apple", "resource", "ubinary", "binary+l", "binary+S", "xbinary", "binary+F"],
    )
    def test_binary_family(self, ft: str):
        assert classify_p4_file_type(ft) is P4TypeClass.BINARY

    @pytest.mark.parametrize("ft", ["", "weirdtype", "blob", "foo+l"])
    def test_unknown(self, ft: str):
        assert classify_p4_file_type(ft) is P4TypeClass.UNKNOWN

    def test_case_insensitive(self):
        assert classify_p4_file_type("BINARY+L") is P4TypeClass.BINARY


# ── sniff_is_binary ──────────────────────────────────────────


class TestSniffIsBinary:
    def test_nul_present(self):
        assert sniff_is_binary(b"abc\x00def") is True

    def test_pure_ascii(self):
        assert sniff_is_binary(b"hello world\n") is False

    def test_empty(self):
        assert sniff_is_binary(b"") is False


# ── decide_lfs_route ─────────────────────────────────────────


def _cfg(**kw) -> LfsConfig:
    base = {
        "enabled": True,
        "extensions": [".png", ".uasset"],
        "size_threshold_bytes": 1000,
        "auto_detect_binary": True,
    }
    base.update(kw)
    return LfsConfig(**base)


class TestDecideLfsRoute:
    def test_ext_match_always_lfs(self):
        # 확장자 매칭이면 size/타입 무관 True
        assert decide_lfs_route(
            git_path="a/b.png", file_type="text", size=1, cfg=_cfg(),
        ) is True

    def test_binary_over_threshold(self):
        assert decide_lfs_route(
            git_path="a/blob", file_type="binary", size=2000, cfg=_cfg(),
        ) is True

    def test_binary_under_threshold(self):
        assert decide_lfs_route(
            git_path="a/blob", file_type="binary", size=500, cfg=_cfg(),
        ) is False

    def test_text_large_not_lfs(self):
        assert decide_lfs_route(
            git_path="a/big.log", file_type="text", size=10**9, cfg=_cfg(),
        ) is False

    def test_unknown_with_nul_sniff_over_threshold(self):
        assert decide_lfs_route(
            git_path="a/blob", file_type="", size=2000, cfg=_cfg(),
            sniff_head=b"\x00\x01",
        ) is True

    def test_unknown_text_sniff_not_lfs(self):
        assert decide_lfs_route(
            git_path="a/blob", file_type="", size=2000, cfg=_cfg(),
            sniff_head=b"plain text",
        ) is False

    def test_auto_detect_disabled_ext_only(self):
        # auto_detect_binary=False → 확장자 전용(회귀 가드)
        cfg = _cfg(auto_detect_binary=False)
        assert decide_lfs_route(
            git_path="a/blob", file_type="binary", size=10**9, cfg=cfg,
        ) is False
        assert decide_lfs_route(
            git_path="a/b.png", file_type="binary", size=1, cfg=cfg,
        ) is True

    def test_unknown_no_sniff_large_conservative(self):
        # sniff 불가 + 임계 이상 → binary 가정 → LFS
        assert decide_lfs_route(
            git_path="a/blob", file_type="", size=2000, cfg=_cfg(),
        ) is True

    def test_unknown_no_sniff_small_text(self):
        # sniff 불가 + 임계 미만 → text 가정 → normal
        assert decide_lfs_route(
            git_path="a/blob", file_type="", size=500, cfg=_cfg(),
        ) is False

    def test_binary_size_none_safe_default(self):
        # size 미상 + binary → 안전하게 LFS
        assert decide_lfs_route(
            git_path="a/blob", file_type="binary", size=None, cfg=_cfg(),
        ) is True

    def test_binary_threshold_override(self):
        cfg = _cfg(binary_size_threshold_bytes=5000)
        assert decide_lfs_route(
            git_path="a/blob", file_type="binary", size=2000, cfg=cfg,
        ) is False
        assert decide_lfs_route(
            git_path="a/blob", file_type="binary", size=6000, cfg=cfg,
        ) is True


# ── partition_for_lfs ────────────────────────────────────────


class _FakeP4:
    """fill_sizes / read_head_prefix 만 흉내내는 가짜 P4Client."""

    def __init__(self, sizes: dict[str, int] | None = None, head: bytes | None = b""):
        self._sizes = sizes or {}
        self._head = head
        self.fill_sizes_calls: list[list[str]] = []
        self.head_reads: list[str] = []

    def fill_sizes(self, actions: list[P4FileAction]) -> None:
        self.fill_sizes_calls.append([a.depot_path for a in actions])
        for a in actions:
            if a.depot_path in self._sizes:
                a.size = self._sizes[a.depot_path]

    def read_head_prefix(self, depot_path: str, revision: int, n: int) -> bytes | None:
        self.head_reads.append(depot_path)
        return self._head


def _fa(depot_path: str, file_type: str, size: int | None = None) -> P4FileAction:
    return P4FileAction(
        depot_path=depot_path, action="add", file_type=file_type, revision=1, size=size,
    )


class TestPartitionForLfs:
    def test_ext_match_skips_size_query(self):
        cfg = _cfg()
        p4 = _FakeP4()
        actions = [(_fa("//d/a.png", "binary"), "a.png")]
        lfs, normal = partition_for_lfs(actions, cfg, p4)
        assert [g for _, g in lfs] == ["a.png"]
        assert normal == []
        # 확장자 매칭은 size 조회를 트리거하지 않음
        assert p4.fill_sizes_calls == []

    def test_text_goes_normal_no_size(self):
        cfg = _cfg()
        p4 = _FakeP4()
        actions = [(_fa("//d/a.cs", "text"), "a.cs")]
        lfs, normal = partition_for_lfs(actions, cfg, p4)
        assert [g for _, g in normal] == ["a.cs"]
        assert lfs == []
        assert p4.fill_sizes_calls == []

    def test_ambiguous_binary_routed_by_size(self):
        cfg = _cfg()
        p4 = _FakeP4(sizes={"//d/big": 5000, "//d/small": 100})
        actions = [
            (_fa("//d/big", "binary"), "big"),
            (_fa("//d/small", "binary"), "small"),
        ]
        lfs, normal = partition_for_lfs(actions, cfg, p4)
        assert [g for _, g in lfs] == ["big"]
        assert [g for _, g in normal] == ["small"]
        # ambiguous 두 건만 size 조회
        assert p4.fill_sizes_calls == [["//d/big", "//d/small"]]

    def test_unknown_type_binary_sniff_to_lfs(self):
        cfg = _cfg()
        p4 = _FakeP4(sizes={"//d/blob": 5000}, head=b"\x00bin")
        actions = [(_fa("//d/blob", ""), "blob")]
        lfs, normal = partition_for_lfs(actions, cfg, p4)
        assert [g for _, g in lfs] == ["blob"]
        assert p4.head_reads == ["//d/blob"]  # UNKNOWN 은 sniff 수행

    def test_unknown_large_text_sniff_to_normal(self):
        # 대형 UNKNOWN 이지만 sniff 결과 text → normal (대형 text 오분류 방지)
        cfg = _cfg()
        p4 = _FakeP4(sizes={"//d/big.log": 10**6}, head=b"plain text only")
        actions = [(_fa("//d/big.log", ""), "big.log")]
        lfs, normal = partition_for_lfs(actions, cfg, p4)
        assert [g for _, g in normal] == ["big.log"]
        assert lfs == []

    def test_unknown_sniff_fail_conservative_lfs(self):
        # read 실패(None) + 대형 → 보수적으로 LFS
        cfg = _cfg()
        p4 = _FakeP4(sizes={"//d/blob": 5000}, head=None)
        actions = [(_fa("//d/blob", ""), "blob")]
        lfs, normal = partition_for_lfs(actions, cfg, p4)
        assert [g for _, g in lfs] == ["blob"]

    def test_auto_detect_disabled_ext_only(self):
        cfg = _cfg(auto_detect_binary=False)
        p4 = _FakeP4(sizes={"//d/blob": 10**9})
        actions = [(_fa("//d/blob", "binary"), "blob")]
        lfs, normal = partition_for_lfs(actions, cfg, p4)
        assert lfs == []
        assert [g for _, g in normal] == ["blob"]
        assert p4.fill_sizes_calls == []
