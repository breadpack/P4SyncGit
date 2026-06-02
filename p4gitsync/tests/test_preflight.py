"""preflight 순수 판정 로직 + PreflightChecker(P4 mock) 테스트."""

from p4gitsync.config.lfs_config import LfsConfig
from p4gitsync.services.preflight import (
    PreflightChecker,
    detect_case_collisions,
    detect_non_lfs_large,
    recommend_history_strategy,
)

_GIB = 1024 ** 3
_TIB = 1024 ** 4


class TestDetectCaseCollisions:
    def test_no_collision(self):
        paths = ["//d/a/Hero.png", "//d/a/Villain.png", "//d/b/readme.txt"]
        assert detect_case_collisions(paths) == []

    def test_collision_detected(self):
        paths = ["//d/a/Hero.png", "//d/a/hero.png", "//d/a/keep.txt"]
        groups = detect_case_collisions(paths)
        assert len(groups) == 1
        assert set(groups[0]) == {"//d/a/Hero.png", "//d/a/hero.png"}

    def test_duplicate_exact_path_not_collision(self):
        # 동일 경로 중복은 충돌 아님
        paths = ["//d/a/x.txt", "//d/a/x.txt"]
        assert detect_case_collisions(paths) == []

    def test_multiple_groups(self):
        paths = ["A.cs", "a.cs", "B.PNG", "b.png", "ok.txt"]
        groups = detect_case_collisions(paths)
        assert len(groups) == 2


class TestDetectNonLfsLarge:
    def _lfs(self):
        return LfsConfig(enabled=True, extensions=[".png", ".fbx"])

    def test_large_non_lfs_flagged(self):
        files = [("//d/big.dat", 10 * 1024 * 1024), ("//d/small.txt", 100)]
        out = detect_non_lfs_large(files, self._lfs(), threshold_bytes=5 * 1024 * 1024)
        assert out == [("//d/big.dat", 10 * 1024 * 1024)]

    def test_large_lfs_tracked_not_flagged(self):
        files = [("//d/tex.png", 50 * 1024 * 1024)]
        out = detect_non_lfs_large(files, self._lfs(), threshold_bytes=5 * 1024 * 1024)
        assert out == []

    def test_small_non_lfs_not_flagged(self):
        files = [("//d/code.cs", 1024)]
        out = detect_non_lfs_large(files, self._lfs(), threshold_bytes=5 * 1024 * 1024)
        assert out == []

    def test_sorted_descending(self):
        files = [("//d/a.bin", 6_000_000), ("//d/b.bin", 9_000_000)]
        out = detect_non_lfs_large(files, None, threshold_bytes=5 * 1024 * 1024)
        assert [p for p, _ in out] == ["//d/b.bin", "//d/a.bin"]

    def test_file_types_excludes_binary_routed(self):
        # auto_detect_binary 켜짐 + file_types 제공 → binary 대형은 route_to_lfs 로
        # 이미 LFS 가므로 리포트에서 제외, text 대형만 남는다.
        cfg = LfsConfig(
            enabled=True, extensions=[".png"],
            auto_detect_binary=True, size_threshold_bytes=1024,
        )
        files = [("//d/blob", 10_000_000), ("//d/big.log", 8_000_000)]
        types = {"//d/blob": "binary", "//d/big.log": "text"}
        out = detect_non_lfs_large(
            files, cfg, threshold_bytes=5 * 1024 * 1024, file_types=types,
        )
        assert out == [("//d/big.log", 8_000_000)]

    def test_file_types_none_falls_back_to_ext(self):
        # file_types 미지정 → 기존 확장자 기준(회귀)
        cfg = LfsConfig(enabled=True, extensions=[".png"], auto_detect_binary=True)
        files = [("//d/blob", 10_000_000)]
        out = detect_non_lfs_large(files, cfg, threshold_bytes=5 * 1024 * 1024)
        assert out == [("//d/blob", 10_000_000)]


class TestRecommendDiskHeadroom:
    def test_sufficient(self):
        from p4gitsync.services.preflight import recommend_disk_headroom

        ok, msg = recommend_disk_headroom(1000, 2000, safety_factor=1.5)
        assert ok is True  # 필요 1500 <= 여유 2000
        assert "≥" in msg

    def test_insufficient(self):
        from p4gitsync.services.preflight import recommend_disk_headroom

        ok, msg = recommend_disk_headroom(1000, 1200, safety_factor=1.5)
        assert ok is False  # 필요 1500 > 여유 1200
        assert "확보" in msg

    def test_safety_factor_applied(self):
        from p4gitsync.services.preflight import recommend_disk_headroom

        # 여유가 소요와 같아도 safety_factor 때문에 부족
        ok, _ = recommend_disk_headroom(1000, 1000, safety_factor=1.5)
        assert ok is False
        ok2, _ = recommend_disk_headroom(1000, 1000, safety_factor=1.0)
        assert ok2 is True


class TestRecommendHistoryStrategy:
    def test_small_history_full(self):
        strat, _ = recommend_history_strategy(10 * _GIB, 20 * _GIB)
        assert strat == "full"

    def test_heavy_history_hybrid(self):
        strat, _ = recommend_history_strategy(1 * _TIB, 3 * _TIB)
        assert strat == "hybrid"

    def test_medium_history_truncate(self):
        strat, _ = recommend_history_strategy(100 * _GIB, 300 * _GIB)
        assert strat == "truncate"


class _FakeP4:
    """PreflightChecker용 최소 P4 mock."""

    def __init__(self, head, history, files):
        self._head = head
        self._history = history
        self._files = files

    def get_size_summary(self, path, all_revisions=False):
        if all_revisions:
            return self._history
        if path.endswith("#head") and "/CODE/" not in path:
            return self._head
        # top-dir 호출은 임의값
        return (1, 1)

    def iter_file_sizes(self, path):
        return self._files


class TestPreflightChecker:
    def test_run_assembles_report_with_blockers(self):
        files = [
            ("//d/main/Hero.png", 100),
            ("//d/main/hero.png", 100),          # case 충돌
            ("//d/main/huge.dat", 20 * 1024 * 1024),  # 비-LFS 대용량
            ("//d/main/tex.png", 50 * 1024 * 1024),   # LFS 추적 → 제외
        ]
        p4 = _FakeP4(
            head=(692_453, 1297969888016),
            history=(2_627_846, 3869753641569),
            files=files,
        )
        lfs = LfsConfig(enabled=True, extensions=[".png"])
        report = PreflightChecker(p4, lfs_config=lfs).run("//d/main")

        assert report.head_files == 692_453
        assert report.history_bytes == 3869753641569
        assert report.strategy == "hybrid"   # 3.5 TiB → heavy
        assert len(report.case_collisions) == 1
        assert report.non_lfs_large == [("//d/main/huge.dat", 20 * 1024 * 1024)]
        assert report.has_blockers is True
        # 리포트 문자열이 예외 없이 생성되는지
        text = report.format_report()
        assert "BLOCKER" in text

    def test_clean_passes(self):
        p4 = _FakeP4(
            head=(10, 1024),
            history=(10, 2048),
            files=[("//d/main/a.cs", 100), ("//d/main/b.cs", 200)],
        )
        report = PreflightChecker(p4, lfs_config=None).run("//d/main")
        assert report.has_blockers is False
        assert report.strategy == "full"
        assert "통과" in report.format_report()


class TestGitattributesHardening:
    def test_normalization_off_by_default(self):
        cfg = LfsConfig(enabled=True, extensions=[".png"])
        out = cfg.generate_gitattributes()
        assert "text=auto" not in out
        assert "*.png filter=lfs" in out

    def test_normalization_on(self):
        cfg = LfsConfig(enabled=True, extensions=[".png"], text_normalization=True)
        out = cfg.generate_gitattributes()
        assert "* text=auto eol=lf" in out
        assert "*.sh text eol=lf" in out
        assert "*.png filter=lfs" in out
