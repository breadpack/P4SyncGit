"""검사/미리보기 계열 CLI 핸들러.

tree / preview 명령을 처리한다.
"""

from __future__ import annotations

from p4gitsync.config.sync_config import AppConfig


def _run_tree(config: AppConfig, depot: str | None, include_deleted: bool, include_virtual: bool = False) -> None:
    from p4gitsync.services.stream_tree_viewer import StreamTreeViewer

    # depot 추출
    if depot:
        p4_depot = depot
    else:
        stream = config.p4.stream
        parts = stream.rstrip("/").split("/")
        p4_depot = "/".join(parts[:3])  # //depot

    p4_client = config.p4.create_client()
    p4_client.connect()

    try:
        viewer = StreamTreeViewer(p4_client)
        roots = viewer.build_tree(
            p4_depot,
            default_branch=config.git.default_branch,
            include_deleted=include_deleted,
            include_virtual=include_virtual,
        )

        if not roots:
            print(f"Stream을 찾을 수 없습니다: {p4_depot}")
            return

        print(f"\nP4 Stream Tree: {p4_depot}")
        print("=" * 60)
        print(viewer.format_tree(roots))
        print(viewer.format_summary(roots))
    finally:
        p4_client.disconnect()


def _run_preview(
    config: AppConfig,
    depot: str | None,
    output: str,
    no_merge_scan: bool,
    merge_scan_limit: int,
) -> None:
    from p4gitsync.services.import_preview import ImportPreview

    if depot:
        p4_depot = depot
    else:
        stream = config.p4.stream
        parts = stream.rstrip("/").split("/")
        p4_depot = "/".join(parts[:3])

    p4_client = config.p4.create_client()
    p4_client.connect()

    try:
        preview = ImportPreview(p4_client)

        scan_merges = not no_merge_scan
        if scan_merges:
            print("merge 스캔 중... (시간이 걸릴 수 있습니다)")
            if merge_scan_limit:
                print(f"  stream당 최근 {merge_scan_limit} CL만 스캔")

        summaries, events = preview.build_preview(
            p4_depot,
            default_branch=config.git.default_branch,
            scan_merges=scan_merges,
            merge_scan_limit=merge_scan_limit,
        )

        report = preview.format_report(summaries, events)
        with open(output, "w", encoding="utf-8") as f:
            f.write(report)

        # HTML 시각화 파일 생성
        base_name = output.rsplit(".", 1)[0]
        html_output = base_name + ".html"
        graph_output = base_name + "-graph.html"

        html_report = preview.format_html(
            summaries, events,
            depot=p4_depot,
            server=f"{config.p4.user}@{config.p4.port}",
        )
        with open(html_output, "w", encoding="utf-8") as f:
            f.write(html_report)

        graph_report = preview.format_git_graph_html(
            summaries, events,
            depot=p4_depot,
            server=f"{config.p4.user}@{config.p4.port}",
        )
        with open(graph_output, "w", encoding="utf-8") as f:
            f.write(graph_report)

        print("\n미리보기 문서 생성 완료:")
        print(f"  마크다운:   {output}")
        print(f"  다이어그램: {html_output}")
        print(f"  커밋 그래프: {graph_output}")

        # 간략 요약 출력
        total_cls = sum(s.total_cls for s in summaries)
        merges = sum(1 for e in events if e.event_type == "merge")
        cps = sum(1 for e in events if e.event_type == "cherry_pick")
        branch_points = sum(1 for e in events if e.event_type == "branch_point")
        print(f"  Branch: {len(summaries)}개")
        print(f"  총 CL: {total_cls:,}개")
        print(f"  분기점: {branch_points}개")
        print(f"  Merge: {merges}개")
        print(f"  Cherry-pick: {cps}개")
    finally:
        p4_client.disconnect()
