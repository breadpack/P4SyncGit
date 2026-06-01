"""대용량 depot → git 마이그레이션 사전 점검(preflight).

마이그레이션을 시작하기 전에 다음을 진단/조언한다:

1. **사이징 어드바이저** — head / 전체 history 용량을 측정하고 LFS 총량을 추정해
   history 수용 전략(전체 / 절단 / 혼합)을 추천한다.
2. **case-collision 스캔** — P4(대소문자 무시) → git(대소문자 구분) 전환 시
   대소문자만 다른 경로가 충돌하는지 탐지한다(마이그레이션 blocker).
3. **비-LFS 대용량 탐지** — LFS 추적 대상이 아닌데 임계치를 넘는 파일을 찾는다.
   그대로 두면 git 본문 history에 영구히 박혀 되돌리기 어렵다.

순수 판정 함수(detect_*, recommend_*)는 P4 의존 없이 단위 테스트된다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from p4gitsync.config.lfs_config import LfsConfig

logger = logging.getLogger("p4gitsync.preflight")

# 사이징 권고 임계치 (binary 단위)
_GIB = 1024 ** 3
_TIB = 1024 ** 4
# 이 미만이면 전체 history도 부담 없음
_HISTORY_FULL_OK = 50 * _GIB
# 이 이상이면 전체 history는 무거움 → 절단/혼합 권고
_HISTORY_HEAVY = 2 * _TIB
# 비-LFS 파일이 이 크기를 넘으면 LFS 누락 의심
_DEFAULT_LARGE_THRESHOLD = 5 * 1024 * 1024  # 5 MiB


# ── 순수 판정 함수 (단위 테스트 대상) ──────────────────────────


def detect_case_collisions(paths: list[str]) -> list[list[str]]:
    """대소문자만 다른 경로 그룹을 반환.

    P4는 case-insensitive라 `Hero.png`/`hero.png`가 공존할 수 있으나 git은
    이를 별개 파일로 보고, case-insensitive 파일시스템(Windows/macOS)에서
    checkout 시 충돌한다.

    Returns:
        충돌 그룹 목록. 각 그룹은 소문자 키가 같은 서로 다른 경로 2개 이상.
        충돌 없으면 빈 목록.
    """
    by_lower: dict[str, list[str]] = {}
    for p in paths:
        by_lower.setdefault(p.lower(), [])
        if p not in by_lower[p.lower()]:
            by_lower[p.lower()].append(p)
    return sorted(
        (group for group in by_lower.values() if len(group) > 1),
        key=lambda g: g[0].lower(),
    )


def detect_non_lfs_large(
    files: list[tuple[str, int]],
    lfs_config: LfsConfig | None,
    threshold_bytes: int = _DEFAULT_LARGE_THRESHOLD,
) -> list[tuple[str, int]]:
    """LFS 추적 대상이 아닌데 threshold를 넘는 (path, size) 목록을 반환.

    그대로 커밋하면 git 본문에 영구히 박혀(F1) 제거에 history rewrite가 필요하다.
    크기 내림차순 정렬.
    """
    out: list[tuple[str, int]] = []
    for path, size in files:
        if size <= threshold_bytes:
            continue
        if lfs_config is not None and lfs_config.is_lfs_target(path):
            continue
        out.append((path, size))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def recommend_history_strategy(
    head_bytes: int, history_bytes: int,
) -> tuple[str, str]:
    """head/전체 history 용량으로부터 (전략, 근거)를 추천.

    전략: "full" | "truncate" | "hybrid"
    """
    ratio = (history_bytes / head_bytes) if head_bytes else 0.0
    if history_bytes <= _HISTORY_FULL_OK:
        return (
            "full",
            f"전체 history {_fmt(history_bytes)}로 충분히 작음 — 전체 변환 권장.",
        )
    if history_bytes >= _HISTORY_HEAVY:
        return (
            "hybrid",
            f"전체 history {_fmt(history_bytes)}가 큼(head 대비 {ratio:.1f}배). "
            f"코드 history는 전체, 대용량 에셋은 최근/head만 가져오는 혼합(또는 head 절단) 권장.",
        )
    return (
        "truncate",
        f"전체 history {_fmt(history_bytes)} (head 대비 {ratio:.1f}배). "
        f"과거 에셋 리비전 조회 필요성이 낮으면 head/최근 절단 권장.",
    )


def _fmt(num_bytes: int) -> str:
    """바이트를 사람이 읽는 단위로."""
    n = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


# ── 리포트 구조 ────────────────────────────────────────────


@dataclass
class DirSize:
    path: str
    head_files: int
    head_bytes: int


@dataclass
class PreflightReport:
    stream: str
    head_files: int = 0
    head_bytes: int = 0
    history_revs: int = 0
    history_bytes: int = 0
    dir_sizes: list[DirSize] = field(default_factory=list)
    strategy: str = ""
    strategy_rationale: str = ""
    case_collisions: list[list[str]] = field(default_factory=list)
    non_lfs_large: list[tuple[str, int]] = field(default_factory=list)
    large_threshold: int = _DEFAULT_LARGE_THRESHOLD

    @property
    def has_blockers(self) -> bool:
        """마이그레이션 전 반드시 해결해야 하는 항목이 있는가."""
        return bool(self.case_collisions) or bool(self.non_lfs_large)

    def format_report(self) -> str:
        lines: list[str] = []
        lines.append("=" * 60)
        lines.append(f"Preflight 점검: {self.stream}")
        lines.append("=" * 60)
        lines.append("")
        lines.append("[1] 용량")
        lines.append(f"  Head    : {self.head_files:,} files / {_fmt(self.head_bytes)}")
        lines.append(f"  History : {self.history_revs:,} revs / {_fmt(self.history_bytes)}")
        if self.dir_sizes:
            lines.append("  디렉터리별 head:")
            for d in self.dir_sizes:
                lines.append(f"    {d.path}  —  {d.head_files:,} files / {_fmt(d.head_bytes)}")
        lines.append("")
        lines.append("[2] History 전략 권고")
        lines.append(f"  → {self.strategy.upper()}: {self.strategy_rationale}")
        lines.append(f"  LFS 총량 추정(전체 history 상한): {_fmt(self.history_bytes)}")
        lines.append("")
        lines.append("[3] Blocker — case-collision (P4 insensitive → git sensitive)")
        if self.case_collisions:
            lines.append(f"  🔴 {len(self.case_collisions)}개 충돌 그룹 발견 (마이그레이션 전 정리 필요):")
            for group in self.case_collisions[:20]:
                lines.append(f"    - {'  |  '.join(group)}")
            if len(self.case_collisions) > 20:
                lines.append(f"    ... 외 {len(self.case_collisions) - 20}개")
        else:
            lines.append("  ✅ 충돌 없음")
        lines.append("")
        lines.append(f"[4] Blocker — 비-LFS 대용량 (> {_fmt(self.large_threshold)})")
        if self.non_lfs_large:
            lines.append(f"  🔴 {len(self.non_lfs_large)}개 — LFS 추적에 추가하거나 .gitattributes 보완 필요:")
            for path, size in self.non_lfs_large[:20]:
                lines.append(f"    - {_fmt(size):>10}  {path}")
            if len(self.non_lfs_large) > 20:
                lines.append(f"    ... 외 {len(self.non_lfs_large) - 20}개")
        else:
            lines.append("  ✅ 없음")
        lines.append("")
        lines.append("=" * 60)
        lines.append("판정: " + ("🔴 BLOCKER 있음 — 위 항목 해결 후 진행" if self.has_blockers
                                else "✅ 통과 — 마이그레이션 진행 가능"))
        lines.append("=" * 60)
        return "\n".join(lines)


class PreflightChecker:
    """P4 depot에 대해 preflight 점검을 수행."""

    def __init__(self, p4_client, lfs_config: LfsConfig | None = None) -> None:
        self._p4 = p4_client
        self._lfs = lfs_config

    def run(
        self,
        stream: str,
        top_dirs: list[str] | None = None,
        large_threshold: int = _DEFAULT_LARGE_THRESHOLD,
    ) -> PreflightReport:
        report = PreflightReport(stream=stream, large_threshold=large_threshold)

        # [1] 용량
        report.head_files, report.head_bytes = self._p4.get_size_summary(
            f"{stream}/...#head",
        )
        report.history_revs, report.history_bytes = self._p4.get_size_summary(
            f"{stream}/...", all_revisions=True,
        )
        for d in top_dirs or []:
            files, b = self._p4.get_size_summary(f"{d}/...#head")
            report.dir_sizes.append(DirSize(path=d, head_files=files, head_bytes=b))

        # [2] 전략 권고
        report.strategy, report.strategy_rationale = recommend_history_strategy(
            report.head_bytes, report.history_bytes,
        )

        # [3]/[4] 파일별 스캔 (head)
        logger.info("파일 목록 수집 중 (head)... 대형 depot은 시간이 걸릴 수 있습니다")
        file_sizes = self._p4.iter_file_sizes(f"{stream}/...#head")
        paths = [p for p, _ in file_sizes]
        report.case_collisions = detect_case_collisions(paths)
        report.non_lfs_large = detect_non_lfs_large(file_sizes, self._lfs, large_threshold)

        return report
