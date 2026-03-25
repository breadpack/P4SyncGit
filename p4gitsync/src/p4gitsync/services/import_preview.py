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

    def format_html(
        self,
        summaries: list[BranchSummary],
        events: list[PreviewEvent],
        depot: str = "",
        server: str = "",
    ) -> str:
        """시각적 HTML 리포트 생성 (Mermaid gitGraph 포함)."""
        merges = [e for e in events if e.event_type == "merge"]
        cherry_picks = [e for e in events if e.event_type == "cherry_pick"]
        branch_points = [e for e in events if e.event_type == "branch_point"]
        total_cls = sum(s.total_cls for s in summaries)

        git_graph = self._build_mermaid_git_graph(summaries, events)
        flow_chart = self._build_mermaid_flow_chart(summaries)
        merge_flow = self._build_mermaid_merge_flow(summaries, merges, cherry_picks)

        rows_html = ""
        colors = ["#1b5e20","#0d47a1","#b71c1c","#e65100","#4a148c","#006064","#880e4f","#33691e","#f57f17"]
        for i, s in enumerate(summaries):
            c = colors[i % len(colors)]
            cp = sum(1 for m in s.merges if m.event_type == "cherry_pick")
            mg = sum(1 for m in s.merges if m.event_type == "merge")
            cl_range = f"CL {s.first_cl:,}~{s.last_cl:,}" if s.first_cl else "-"
            bp = f"CL {s.branch_point_cl:,}" if s.branch_point_cl else "-"
            rows_html += (
                f'<tr><td style="color:{c};font-weight:bold">{s.branch}</td>'
                f"<td>{s.stream}</td><td>{s.total_cls:,}</td><td>{cl_range}</td>"
                f"<td>{mg}</td><td>{cp}</td><td>{bp}</td><td>{s.parent_branch or '-'}</td></tr>\n"
            )

        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>P4GitSync Import Preview</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
body {{ font-family:'Segoe UI',sans-serif; background:#1a1a2e; color:#e0e0e0; padding:20px; max-width:1400px; margin:auto; }}
h1 {{ color:#00d4ff; }} h2 {{ color:#7ec8e3; margin-top:2em; }}
.mermaid {{ background:#16213e; border-radius:12px; padding:20px; margin:20px 0; overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; margin:20px 0; }}
th {{ background:#16213e; color:#00d4ff; padding:10px; text-align:left; }}
td {{ padding:8px 10px; border-bottom:1px solid #2a2a4a; }}
tr:hover {{ background:#16213e; }}
.stat {{ display:inline-block; background:#16213e; border-radius:8px; padding:8px 16px; margin:4px; }}
.stat-num {{ font-size:1.5em; color:#00d4ff; font-weight:bold; }}
.stat-label {{ font-size:0.85em; color:#888; }}
</style>
</head>
<body>
<h1>P4GitSync Import Preview</h1>
<p>Server: {server} | Depot: {depot}</p>
<div style="margin:20px 0;">
  <span class="stat"><span class="stat-num">{len(summaries)}</span><br><span class="stat-label">Branches</span></span>
  <span class="stat"><span class="stat-num">{total_cls:,}</span><br><span class="stat-label">Changelists</span></span>
  <span class="stat"><span class="stat-num">{len(merges)}</span><br><span class="stat-label">Merges</span></span>
  <span class="stat"><span class="stat-num">{len(cherry_picks)}</span><br><span class="stat-label">Cherry-picks</span></span>
  <span class="stat"><span class="stat-num">{len(branch_points)}</span><br><span class="stat-label">Branch Points</span></span>
</div>
<h2>Git Commit Tree</h2>
<pre class="mermaid">
{git_graph}
</pre>
<h2>Branch Hierarchy</h2>
<pre class="mermaid">
{flow_chart}
</pre>
<h2>Merge / Cherry-pick Flow</h2>
<pre class="mermaid">
{merge_flow}
</pre>
<h2>Branch Summary</h2>
<table>
<tr><th>Branch</th><th>Stream</th><th>CL</th><th>Range</th><th>Merge</th><th>CP</th><th>Branch Point</th><th>Parent</th></tr>
{rows_html}
</table>
<script>
mermaid.initialize({{ startOnLoad:true, theme:'dark', gitGraph:{{ mainBranchName:'{summaries[0].branch if summaries else "dev"}', showCommitLabel:true, rotateCommitLabel:true }}, flowchart:{{ curve:'basis', htmlLabels:true }} }});
</script>
</body>
</html>"""

    def format_git_graph_html(
        self,
        summaries: list[BranchSummary],
        events: list[PreviewEvent],
        depot: str = "",
        server: str = "",
    ) -> str:
        """git GUI 스타일의 커밋 그래프 HTML을 생성한다."""
        import json

        merges = [e for e in events if e.event_type == "merge"]
        cherry_picks = [e for e in events if e.event_type == "cherry_pick"]
        branch_points = [e for e in events if e.event_type == "branch_point"]
        total_cls = sum(s.total_cls for s in summaries)

        # 이벤트를 JSON으로 변환
        graph_events = []
        for e in sorted(events, key=lambda x: x.cl):
            if e.event_type in ("first_commit", "last_commit"):
                continue
            src_branch = None
            if e.merge_source:
                src_name = e.merge_source.split("/")[-1]
                for s in summaries:
                    if s.branch == src_name:
                        src_branch = src_name
                        break
            graph_events.append({
                "cl": e.cl,
                "type": e.event_type,
                "branch": e.branch,
                "source_branch": src_branch,
                "source_cl": e.merge_source_cl,
                "files": e.file_count,
                "desc": e.description[:60].replace("\n", " ").replace('"', "'"),
            })

        branches_json = json.dumps([
            {"name": s.branch, "parent": s.parent_branch, "bp": s.branch_point_cl,
             "cls": s.total_cls, "merges": sum(1 for m in s.merges if m.event_type == "merge"),
             "cps": sum(1 for m in s.merges if m.event_type == "cherry_pick")}
            for s in summaries
        ], ensure_ascii=False)
        events_json = json.dumps(graph_events, ensure_ascii=False)

        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>P4GitSync Commit Graph</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Consolas','Courier New',monospace; background:#0d1117; color:#c9d1d9; }}
.header {{ padding:16px 20px; background:#161b22; border-bottom:1px solid #30363d; }}
.header h1 {{ font-size:16px; color:#58a6ff; font-weight:normal; }}
.header .stats {{ margin-top:6px; font-size:12px; color:#8b949e; }}
.header .stats span {{ margin-right:16px; }}
.header .stats b {{ color:#c9d1d9; }}
.legend {{ padding:8px 20px; background:#161b22; border-bottom:1px solid #30363d; font-size:12px; display:flex; gap:16px; flex-wrap:wrap; }}
.legend-item {{ display:flex; align-items:center; gap:4px; }}
.legend-dot {{ width:10px; height:10px; border-radius:50%; display:inline-block; }}
.legend-line {{ width:20px; height:2px; display:inline-block; }}
.container {{ display:flex; height:calc(100vh - 90px); }}
.graph-panel {{ width:320px; min-width:320px; overflow-y:auto; background:#0d1117; border-right:1px solid #30363d; }}
.detail-panel {{ flex:1; overflow-y:auto; padding:0; }}
.row {{ display:flex; height:28px; align-items:center; cursor:pointer; padding-right:8px; }}
.row:hover {{ background:#161b22; }}
.row.selected {{ background:#1c2333; }}
.graph-cell {{ width:280px; min-width:280px; height:28px; position:relative; }}
.cl-label {{ font-size:11px; color:#8b949e; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; flex:1; padding-left:4px; }}
.detail-panel .info {{ padding:16px 20px; }}
.detail-panel .info h3 {{ color:#58a6ff; font-size:14px; margin-bottom:8px; }}
.detail-panel .info p {{ font-size:12px; color:#8b949e; margin:4px 0; }}
.detail-panel .info .tag {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; margin:2px; }}
.tag-merge {{ background:#1f3d1f; color:#3fb950; }}
.tag-cp {{ background:#3d2f1f; color:#d29922; }}
.tag-branch {{ background:#1f2d3d; color:#58a6ff; }}
svg {{ display:block; }}
</style>
</head>
<body>
<div class="header">
  <h1>P4GitSync Commit Graph &mdash; {depot}</h1>
  <div class="stats">
    <span>Server: <b>{server}</b></span>
    <span>Branches: <b>{len(summaries)}</b></span>
    <span>CL: <b>{total_cls:,}</b></span>
    <span>Merge: <b>{len(merges)}</b></span>
    <span>Cherry-pick: <b>{len(cherry_picks)}</b></span>
  </div>
</div>
<div class="legend" id="legend"></div>
<div class="container">
  <div class="graph-panel" id="graphPanel"></div>
  <div class="detail-panel" id="detailPanel">
    <div class="info" style="padding:40px;color:#484f58;">
      커밋을 클릭하면 상세 정보가 표시됩니다.
    </div>
  </div>
</div>

<script>
const BRANCHES = {branches_json};
const EVENTS = {events_json};

const COLORS = [
  '#3fb950','#58a6ff','#f85149','#d29922','#bc8cff',
  '#39d2c0','#f778ba','#79c0ff','#ffa657',
];
const branchColor = {{}};
const branchIndex = {{}};
BRANCHES.forEach((b, i) => {{
  branchColor[b.name] = COLORS[i % COLORS.length];
  branchIndex[b.name] = i;
}});

const COL_W = 28;
const ROW_H = 28;
const DOT_R = 5;
const LINE_W = 2;

function buildLegend() {{
  const el = document.getElementById('legend');
  BRANCHES.forEach(b => {{
    const c = branchColor[b.name];
    el.innerHTML += `<div class="legend-item">
      <span class="legend-dot" style="background:${{c}}"></span>
      <span style="color:${{c}}">${{b.name}}</span>
      <span style="color:#484f58">(${{b.cls.toLocaleString()}} CL, ${{b.merges}}M+${{b.cps}}CP)</span>
    </div>`;
  }});
}}

function buildGraph() {{
  const panel = document.getElementById('graphPanel');
  const activeBranches = new Set();
  const rows = [];

  EVENTS.forEach((ev, idx) => {{
    if (ev.type === 'branch_point') activeBranches.add(ev.branch);
    const active = [...activeBranches];
    rows.push({{ ev, active: [...active], idx }});
    if (ev.type === 'branch_point') {{}} // already added
  }});

  // Ensure main branch is always active
  if (BRANCHES.length > 0) activeBranches.add(BRANCHES[0].name);

  const svgW = BRANCHES.length * COL_W + 20;
  const svgH = rows.length * ROW_H;

  rows.forEach((row, ri) => {{
    const ev = row.ev;
    const col = branchIndex[ev.branch] ?? 0;
    const color = branchColor[ev.branch] || '#484f58';

    // Create row element
    const rowEl = document.createElement('div');
    rowEl.className = 'row';
    rowEl.dataset.idx = ri;
    rowEl.onclick = () => showDetail(ev);

    // SVG for graph
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', svgW);
    svg.setAttribute('height', ROW_H);
    svg.style.width = svgW + 'px';
    svg.style.height = ROW_H + 'px';
    const graphCell = document.createElement('div');
    graphCell.className = 'graph-cell';
    graphCell.style.width = svgW + 'px';

    // Draw vertical lines for active branches
    row.active.forEach(bname => {{
      const bi = branchIndex[bname] ?? 0;
      const x = bi * COL_W + COL_W / 2;
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', x); line.setAttribute('y1', 0);
      line.setAttribute('x2', x); line.setAttribute('y2', ROW_H);
      line.setAttribute('stroke', branchColor[bname] || '#30363d');
      line.setAttribute('stroke-width', LINE_W);
      line.setAttribute('opacity', '0.4');
      svg.appendChild(line);
    }});

    const cx = col * COL_W + COL_W / 2;
    const cy = ROW_H / 2;

    // Merge/cherry-pick connection line
    if ((ev.type === 'merge' || ev.type === 'cherry_pick') && ev.source_branch) {{
      const srcCol = branchIndex[ev.source_branch] ?? col;
      const sx = srcCol * COL_W + COL_W / 2;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      const midX = (sx + cx) / 2;
      path.setAttribute('d', `M${{sx}},${{0}} C${{sx}},${{cy}} ${{cx}},${{cy * 0.3}} ${{cx}},${{cy}}`);
      path.setAttribute('stroke', ev.type === 'cherry_pick' ? '#d29922' : color);
      path.setAttribute('stroke-width', LINE_W);
      path.setAttribute('fill', 'none');
      if (ev.type === 'cherry_pick') path.setAttribute('stroke-dasharray', '4,3');
      svg.appendChild(path);
    }}

    // Commit dot
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', cx); circle.setAttribute('cy', cy);
    circle.setAttribute('r', ev.type === 'branch_point' ? DOT_R + 2 : DOT_R);
    if (ev.type === 'cherry_pick') {{
      circle.setAttribute('fill', '#0d1117');
      circle.setAttribute('stroke', '#d29922');
      circle.setAttribute('stroke-width', '2');
    }} else if (ev.type === 'branch_point') {{
      circle.setAttribute('fill', '#0d1117');
      circle.setAttribute('stroke', color);
      circle.setAttribute('stroke-width', '2');
    }} else {{
      circle.setAttribute('fill', color);
    }}
    svg.appendChild(circle);

    graphCell.appendChild(svg);
    rowEl.appendChild(graphCell);

    // CL label
    const label = document.createElement('div');
    label.className = 'cl-label';
    const typeIcon = ev.type === 'merge' ? '\u2190' : ev.type === 'cherry_pick' ? '\u2662' : '\u25cb';
    label.textContent = `${{typeIcon}} CL${{ev.cl}} ${{ev.desc}}`;
    label.style.color = ev.type === 'cherry_pick' ? '#d29922' : color;
    rowEl.appendChild(label);

    panel.appendChild(rowEl);
  }});
}}

function showDetail(ev) {{
  const dp = document.getElementById('detailPanel');
  const color = branchColor[ev.branch] || '#c9d1d9';
  let typeTag = '';
  if (ev.type === 'merge') typeTag = '<span class="tag tag-merge">MERGE</span>';
  else if (ev.type === 'cherry_pick') typeTag = '<span class="tag tag-cp">CHERRY-PICK</span>';
  else if (ev.type === 'branch_point') typeTag = '<span class="tag tag-branch">BRANCH</span>';

  let srcInfo = '';
  if (ev.source_branch) srcInfo = `<p>Source: <b style="color:${{branchColor[ev.source_branch] || '#c9d1d9'}}">${{ev.source_branch}}</b></p>`;
  if (ev.source_cl) srcInfo += `<p>Source CL: <b>${{ev.source_cl}}</b></p>`;

  dp.innerHTML = `<div class="info">
    <h3 style="color:${{color}}">CL ${{ev.cl}} ${{typeTag}}</h3>
    <p>Branch: <b style="color:${{color}}">${{ev.branch}}</b></p>
    ${{srcInfo}}
    <p>Files: <b>${{ev.files}}</b></p>
    <p style="margin-top:12px;color:#c9d1d9;white-space:pre-wrap;font-size:13px;">${{ev.desc}}</p>
  </div>`;

  document.querySelectorAll('.row').forEach(r => r.classList.remove('selected'));
  const sel = document.querySelector(`.row[data-idx]`);
}}

buildLegend();
buildGraph();
</script>
</body>
</html>"""

    def _build_mermaid_git_graph(
        self, summaries: list[BranchSummary], events: list[PreviewEvent],
    ) -> str:
        """Mermaid gitGraph 문법 생성."""
        lines = ["gitGraph"]
        branch_order = [s.branch for s in summaries]
        main_branch = branch_order[0] if branch_order else "dev"

        sorted_events = sorted(events, key=lambda e: e.cl)
        created_branches = {main_branch}
        last_branch = main_branch

        for e in sorted_events:
            if e.event_type in ("first_commit", "last_commit"):
                continue

            if e.event_type == "branch_point":
                if e.branch in created_branches:
                    continue
                parent = None
                for s in summaries:
                    if s.branch == e.branch:
                        parent = s.parent_branch
                        break
                if parent and parent != last_branch:
                    lines.append(f"  checkout {parent}")
                    last_branch = parent
                lines.append(f"  branch {e.branch}")
                created_branches.add(e.branch)
                last_branch = e.branch

            elif e.event_type == "merge":
                if e.branch not in created_branches:
                    continue
                if e.branch != last_branch:
                    lines.append(f"  checkout {e.branch}")
                    last_branch = e.branch
                source_branch = None
                if e.merge_source:
                    src_name = e.merge_source.split("/")[-1]
                    if src_name in created_branches:
                        source_branch = src_name
                if source_branch:
                    lines.append(f'  merge {source_branch} id: "CL{e.cl}"')
                else:
                    lines.append(f'  commit id: "CL{e.cl}-mg"')

            elif e.event_type == "cherry_pick":
                if e.branch not in created_branches:
                    continue
                if e.branch != last_branch:
                    lines.append(f"  checkout {e.branch}")
                    last_branch = e.branch
                src_cl = e.merge_source_cl or "?"
                lines.append(f'  commit id: "CL{e.cl}cp{src_cl}" type: HIGHLIGHT')

        return "\n".join(lines)

    def _build_mermaid_flow_chart(self, summaries: list[BranchSummary]) -> str:
        """Branch hierarchy flow chart."""
        lines = ["graph TD"]
        for s in summaries:
            cp = sum(1 for m in s.merges if m.event_type == "cherry_pick")
            mg = sum(1 for m in s.merges if m.event_type == "merge")
            label = f"<b>{s.branch}</b><br/>{s.total_cls:,} CL"
            if mg or cp:
                label += f"<br/>{mg}M + {cp}CP"
            lines.append(f'  {s.branch}["{label}"]')
        for s in summaries:
            if s.parent_branch:
                bp = f"CL {s.branch_point_cl}" if s.branch_point_cl else ""
                lines.append(f'  {s.parent_branch} -->|"{bp}"| {s.branch}')
        return "\n".join(lines)

    def _build_mermaid_merge_flow(
        self, summaries: list[BranchSummary],
        merges: list[PreviewEvent], cherry_picks: list[PreviewEvent],
    ) -> str:
        """Merge/cherry-pick flow diagram."""
        lines = ["graph LR"]
        flows: dict[tuple[str, str, str], int] = {}
        for e in merges:
            src = e.merge_source.split("/")[-1] if e.merge_source else "unknown"
            flows[(src, e.branch, "merge")] = flows.get((src, e.branch, "merge"), 0) + 1
        for e in cherry_picks:
            src = e.merge_source.split("/")[-1] if e.merge_source else "unknown"
            flows[(src, e.branch, "cp")] = flows.get((src, e.branch, "cp"), 0) + 1

        all_nodes = set()
        for (src, tgt, _) in flows:
            all_nodes.add(src)
            all_nodes.add(tgt)
        for n in sorted(all_nodes):
            lines.append(f'  {n}["{n}"]')

        for (src, tgt, ftype), count in sorted(flows.items()):
            if ftype == "merge":
                lines.append(f'  {src} -->|"{count} merge"| {tgt}')
            else:
                lines.append(f'  {src} -.->|"{count} cp"| {tgt}')
        return "\n".join(lines)
