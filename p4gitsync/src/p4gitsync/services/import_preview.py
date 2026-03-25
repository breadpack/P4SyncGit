from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import re

from p4gitsync.p4.merge_analyzer import MergeAnalyzer, INTEGRATION_ACTIONS

_MERGE_DESC_PATTERN = re.compile(
    r"(?:Merge|Copy|Copying|Premerge)\s+(//[^\s.]+)", re.IGNORECASE,
)
_CHERRYPICK_DESC_PATTERN = re.compile(
    r"\[(?:핫픽스|hotfix|cherry-?pick)[^]]*\]"
    r".*?\[(?:(\w+)@(\d+))",
    re.IGNORECASE,
)
_SOURCE_REF_PATTERN = re.compile(
    r"\[(\w+)@(\d+(?:[.,]\d+)*)\]",
)
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.services.stream_tree_viewer import StreamTreeViewer, StreamInfo

logger = logging.getLogger("p4gitsync.import_preview")


@dataclass
class PreviewEvent:
    """타임라인에 표시할 이벤트."""

    cl: int
    timestamp: int
    event_type: str  # "branch_point" | "merge" | "first_commit" | "last_commit"
    branch: str
    stream: str
    description: str = ""
    user: str = ""
    workspace: str = ""
    merge_source: str | None = None
    merge_source_cl: int | None = None
    file_count: int = 0


@dataclass
class BranchSummary:
    """branch별 요약."""

    stream: str
    branch: str
    total_cls: int
    merge_count: int
    first_cl: int | None
    last_cl: int | None
    branch_point_cl: int | None
    parent_branch: str | None
    merges: list[PreviewEvent] = field(default_factory=list)


class ImportPreview:
    """import 전 미리보기 — branch 분기점, merge 이벤트 추출."""

    def __init__(self, p4_client: P4Client) -> None:
        self._p4 = p4_client
        self._merge_analyzer = MergeAnalyzer(p4_client)

    def build_preview(
        self,
        depot: str,
        default_branch: str = "main",
        scan_merges: bool = True,
        merge_scan_limit: int = 0,
    ) -> tuple[list[BranchSummary], list[PreviewEvent]]:
        """import 미리보기 데이터를 구성한다.

        Args:
            depot: P4 depot 경로
            default_branch: mainline의 Git branch 이름
            scan_merges: integration CL을 분석할지 여부
            merge_scan_limit: merge 스캔 CL 수 제한 (0=전체)

        Returns:
            (branch_summaries, timeline_events)
        """
        # stream 트리 구성 (virtual 제외)
        viewer = StreamTreeViewer(self._p4)
        roots = viewer.build_tree(depot, default_branch, include_virtual=False)
        all_streams = viewer._flatten(roots)

        summaries: list[BranchSummary] = []
        events: list[PreviewEvent] = []

        for si in all_streams:
            logger.info("스캔 중: %s (%s)", si.stream, si.branch)
            summary, stream_events = self._scan_stream(
                si, all_streams, scan_merges, merge_scan_limit,
            )
            summaries.append(summary)
            events.extend(stream_events)

        events.sort(key=lambda e: e.cl)
        return summaries, events

    def _scan_stream(
        self,
        si: StreamInfo,
        all_streams: list[StreamInfo],
        scan_merges: bool,
        merge_scan_limit: int,
    ) -> tuple[BranchSummary, list[PreviewEvent]]:
        """단일 stream을 스캔하여 요약 및 이벤트 추출."""
        events: list[PreviewEvent] = []
        merge_count = 0

        # 부모 branch 찾기 (tree 구조에서 실제 parent 탐색)
        parent_branch = None
        for s in all_streams:
            if si in s.children:
                parent_branch = s.branch
                break

        # 분기점 이벤트
        if si.branch_point_cl:
            events.append(PreviewEvent(
                cl=si.branch_point_cl,
                timestamp=0,
                event_type="branch_point",
                branch=si.branch,
                stream=si.stream,
                description=f"'{si.branch}' branch 생성 (from {parent_branch or 'root'})",
            ))

        # CL 목록
        try:
            all_cls = self._p4.get_all_changes(si.stream)
        except Exception:
            all_cls = []

        if not all_cls:
            return BranchSummary(
                stream=si.stream, branch=si.branch, total_cls=0,
                merge_count=0, first_cl=None, last_cl=None,
                branch_point_cl=si.branch_point_cl, parent_branch=parent_branch,
            ), events

        # 첫/마지막 commit 이벤트
        first_info = self._describe_safe(all_cls[0])
        if first_info:
            events.append(PreviewEvent(
                cl=all_cls[0],
                timestamp=first_info.get("timestamp", 0),
                event_type="first_commit",
                branch=si.branch,
                stream=si.stream,
                description=first_info.get("description", "")[:100],
                user=first_info.get("user", ""),
                workspace=first_info.get("workspace", ""),
            ))

        if len(all_cls) > 1:
            last_info = self._describe_safe(all_cls[-1])
            if last_info:
                events.append(PreviewEvent(
                    cl=all_cls[-1],
                    timestamp=last_info.get("timestamp", 0),
                    event_type="last_commit",
                    branch=si.branch,
                    stream=si.stream,
                    description=last_info.get("description", "")[:100],
                    user=last_info.get("user", ""),
                    workspace=last_info.get("workspace", ""),
                ))

        # merge 스캔
        if scan_merges:
            merge_events, merge_count = self._scan_merges(
                si, all_cls, merge_scan_limit,
            )
            events.extend(merge_events)

        return BranchSummary(
            stream=si.stream, branch=si.branch, total_cls=len(all_cls),
            merge_count=merge_count, first_cl=all_cls[0], last_cl=all_cls[-1],
            branch_point_cl=si.branch_point_cl, parent_branch=parent_branch,
            merges=merge_events if scan_merges else [],
        ), events

    def _scan_merges(
        self,
        si: StreamInfo,
        all_cls: list[int],
        limit: int,
    ) -> tuple[list[PreviewEvent], int]:
        """stream의 CL에서 integration(merge)을 감지한다.

        경량 스캔: describe의 action 필드만으로 integration CL을 식별하고,
        description에서 source stream을 추출한다 (filelog 호출 없음).
        """
        events: list[PreviewEvent] = []
        merge_count = 0
        scan_cls = all_cls if limit == 0 else all_cls[-limit:]

        batch_size = 50
        for batch_start in range(0, len(scan_cls), batch_size):
            batch = scan_cls[batch_start:batch_start + batch_size]

            for cl in batch:
                try:
                    info = self._p4.describe(cl)
                except Exception:
                    continue

                # action 필드만으로 integration 감지 (filelog 불필요)
                integration_files = [
                    fa for fa in info.files if fa.action in INTEGRATION_ACTIONS
                ]
                if not integration_files:
                    continue

                # cherry-pick vs full merge 분류
                event_type, source_stream, source_cl = self._classify_integration(
                    info.description, integration_files, cl,
                )

                merge_count += 1
                events.append(PreviewEvent(
                    cl=cl,
                    timestamp=info.timestamp,
                    event_type=event_type,
                    branch=si.branch,
                    stream=si.stream,
                    description=info.description[:100],
                    user=info.user,
                    workspace=info.workspace,
                    merge_source=source_stream,
                    merge_source_cl=source_cl,
                    file_count=len(integration_files),
                ))

            processed = min(batch_start + batch_size, len(scan_cls))
            if processed % 500 == 0 or processed == len(scan_cls):
                logger.info(
                    "  %s: %d/%d CL 스캔, %d merges",
                    si.branch, processed, len(scan_cls), merge_count,
                )

        return events, merge_count

    def _classify_integration(
        self,
        description: str,
        integration_files: list,
        cl: int,
    ) -> tuple[str, str | None, int | None]:
        """integration CL을 cherry-pick vs full merge로 분류.

        Returns:
            (event_type, source_stream, source_cl)
        """
        # 1. 핫픽스/cherry-pick 패턴 확인
        cp_match = _CHERRYPICK_DESC_PATTERN.search(description)
        if cp_match:
            source_name = cp_match.group(1)  # "alpha", "dev" 등
            source_cl_str = cp_match.group(2)
            source_cl = int(source_cl_str.split(",")[0].split(".")[0]) if source_cl_str else None
            # source stream 이름 → 전체 경로 추정
            source_stream = f"//stream/{source_name}" if source_name else None
            return "cherry_pick", source_stream, source_cl

        # 2. [stream@CL] 패턴 (핫픽스 키워드 없어도)
        ref_match = _SOURCE_REF_PATTERN.search(description)
        if ref_match and len(integration_files) <= 10:
            source_name = ref_match.group(1)
            source_cl_str = ref_match.group(2)
            source_cl = int(source_cl_str.split(",")[0].split(".")[0]) if source_cl_str else None
            source_stream = f"//stream/{source_name}" if source_name else None
            return "cherry_pick", source_stream, source_cl

        # 3. Full merge — description 패턴 또는 filelog fallback
        source_stream = self._extract_source_from_description(description)
        if source_stream is None:
            source_stream = self._detect_source_from_filelog(
                integration_files[0].depot_path, cl,
            )
        return "merge", source_stream, None

    def _detect_source_from_filelog(self, depot_path: str, cl: int) -> str | None:
        """integration 파일 1개의 filelog에서 source stream을 추출한다."""
        try:
            results = self._p4._p4.run_filelog("-m", "1", f"{depot_path}@{cl}")
            if not results:
                return None
            entry = results[0]
            for rev in entry.revisions:
                if rev.change != cl:
                    continue
                try:
                    for integ in rev.integrations:
                        if "from" in integ.how:
                            source = integ.file
                            m = re.match(r"(//[^/]+/[^/]+)/", source)
                            if m:
                                return m.group(1)
                except AttributeError:
                    pass
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_source_from_description(description: str) -> str | None:
        """description에서 'Merge/Copy/Copying //stream/X to ...' 패턴으로 source stream 추출."""
        m = _MERGE_DESC_PATTERN.search(description)
        if m:
            return m.group(1)
        return None

    def _describe_safe(self, cl: int) -> dict | None:
        try:
            info = self._p4.describe(cl)
            return {
                "description": info.description,
                "user": info.user,
                "workspace": info.workspace,
                "timestamp": info.timestamp,
            }
        except Exception:
            return None

    def format_report(
        self,
        summaries: list[BranchSummary],
        events: list[PreviewEvent],
    ) -> str:
        """미리보기 결과를 마크다운 문서로 포맷팅한다."""
        lines = [
            "# P4GitSync Import Preview",
            "",
            "## Branch 요약",
            "",
            "| Branch | Stream | CL 수 | Merge | Cherry-pick | 분기점 | Parent |",
            "|--------|--------|-------|-------|-------------|--------|--------|",
        ]

        for s in summaries:
            bp = f"CL {s.branch_point_cl}" if s.branch_point_cl else "-"
            cp_count = sum(1 for m in s.merges if m.event_type == "cherry_pick")
            mg_count = sum(1 for m in s.merges if m.event_type == "merge")
            lines.append(
                f"| {s.branch} | {s.stream} | {s.total_cls:,} | "
                f"{mg_count} | {cp_count} | {bp} | {s.parent_branch or '-'} |"
            )

        total_cls = sum(s.total_cls for s in summaries)
        total_merges = sum(1 for e in events if e.event_type == "merge")
        total_cp = sum(1 for e in events if e.event_type == "cherry_pick")
        lines.extend([
            "",
            f"**총 {len(summaries)}개 branch, {total_cls:,} CL, "
            f"{total_merges} merge, {total_cp} cherry-pick**",
        ])

        # Git branch 트리
        lines.extend(["", "## Git Branch Tree", "", "```"])
        tree_lines = self._format_branch_tree(summaries)
        lines.extend(tree_lines)
        lines.append("```")

        # 타임라인
        lines.extend(["", "## 타임라인 (분기/머지 이벤트)", ""])

        branch_points = [e for e in events if e.event_type == "branch_point"]
        merges = [e for e in events if e.event_type == "merge"]
        cherry_picks = [e for e in events if e.event_type == "cherry_pick"]

        if branch_points:
            lines.extend(["### Branch 생성", ""])
            lines.append("| CL | Branch | 설명 |")
            lines.append("|---:|--------|------|")
            for e in sorted(branch_points, key=lambda x: x.cl):
                lines.append(f"| {e.cl} | `{e.branch}` | {e.description} |")

        if merges:
            lines.extend(["", "### Merge (Full Integration)", ""])
            lines.append("| CL | Target Branch | Source Stream | 파일 수 | 설명 |")
            lines.append("|---:|--------------|--------------|--------:|------|")
            for e in sorted(merges, key=lambda x: x.cl):
                desc = e.description.replace("|", "/").replace("\n", " ")[:60]
                lines.append(
                    f"| {e.cl} | `{e.branch}` | {e.merge_source or '?'} | "
                    f"{e.file_count} | {desc} |"
                )

        if cherry_picks:
            lines.extend(["", "### Cherry-pick (Hotfix)", ""])
            lines.append("| CL | Target Branch | Source | Source CL | 파일 수 | 설명 |")
            lines.append("|---:|--------------|--------|----------:|--------:|------|")
            for e in sorted(cherry_picks, key=lambda x: x.cl):
                desc = e.description.replace("|", "/").replace("\n", " ")[:60]
                source_short = e.merge_source.split("/")[-1] if e.merge_source else "?"
                lines.append(
                    f"| {e.cl} | `{e.branch}` | {source_short} | "
                    f"{e.merge_source_cl or '-'} | {e.file_count} | {desc} |"
                )

        # branch별 merge 다이어그램
        lines.extend(["", "## Branch별 Merge 다이어그램", ""])
        for s in summaries:
            if not s.merges:
                continue
            lines.append(f"### {s.branch} ({s.stream})")
            lines.append("")
            lines.append("```")
            lines.extend(self._format_merge_diagram(s))
            lines.append("```")
            lines.append("")

        return "\n".join(lines)

    def _format_branch_tree(self, summaries: list[BranchSummary]) -> list[str]:
        """branch 트리를 텍스트로 포맷팅."""
        # root 찾기
        branch_map = {s.branch: s for s in summaries}
        children: dict[str | None, list[BranchSummary]] = {}
        for s in summaries:
            children.setdefault(s.parent_branch, []).append(s)

        lines = []
        roots = children.get(None, [])
        for i, root in enumerate(roots):
            self._format_branch_node(
                root, children, lines, "", i == len(roots) - 1,
            )
        return lines

    def _format_branch_node(
        self,
        node: BranchSummary,
        children: dict[str | None, list[BranchSummary]],
        lines: list[str],
        prefix: str,
        is_last: bool,
    ) -> None:
        connector = "└── " if is_last else "├── "
        bp = f" (분기: CL {node.branch_point_cl})" if node.branch_point_cl else ""
        merge_info = f", {node.merge_count} merges" if node.merge_count else ""
        lines.append(
            f"{prefix}{connector}{node.branch} ({node.total_cls:,} CL{merge_info}){bp}"
        )
        kids = children.get(node.branch, [])
        child_prefix = prefix + ("    " if is_last else "│   ")
        for i, child in enumerate(kids):
            self._format_branch_node(
                child, children, lines, child_prefix, i == len(kids) - 1,
            )

    def _format_merge_diagram(self, summary: BranchSummary) -> list[str]:
        """branch의 merge 이벤트를 시각적 다이어그램으로."""
        lines = []
        source_streams = set()
        for m in summary.merges:
            if m.merge_source:
                source_streams.add(m.merge_source)

        if not source_streams:
            return [f"  {summary.branch}: (merge 없음)"]

        # source별 그룹핑
        by_source: dict[str, list[PreviewEvent]] = {}
        for m in summary.merges:
            by_source.setdefault(m.merge_source or "?", []).append(m)

        for source, merge_events in sorted(by_source.items()):
            source_short = source.split("/")[-1]
            lines.append(f"  {source_short} → {summary.branch}:")
            for m in merge_events[:20]:  # 최대 20개
                ts = datetime.fromtimestamp(m.timestamp).strftime("%Y-%m-%d") if m.timestamp else "?"
                desc = m.description[:50].replace("\n", " ")
                lines.append(
                    f"    CL {m.cl} ({ts}) [{m.file_count} files] {desc}"
                )
            if len(merge_events) > 20:
                lines.append(f"    ... 외 {len(merge_events) - 20}건")
            lines.append("")

        return lines
