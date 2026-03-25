from __future__ import annotations

import logging
from dataclasses import dataclass, field

from p4gitsync.p4.p4_client import P4Client

logger = logging.getLogger("p4gitsync.stream_tree_viewer")


@dataclass
class StreamInfo:
    """P4 Stream의 정보."""

    stream: str
    name: str
    stream_type: str
    parent: str | None
    branch: str
    change_count: int = 0
    first_cl: int | None = None
    last_cl: int | None = None
    branch_point_cl: int | None = None
    deleted: bool = False
    children: list[StreamInfo] = field(default_factory=list)


class StreamTreeViewer:
    """P4 depot의 stream 계층을 조회하여 트리 형태로 표시한다."""

    def __init__(self, p4_client: P4Client) -> None:
        self._p4 = p4_client

    def build_tree(
        self,
        depot: str,
        default_branch: str = "main",
        include_deleted: bool = False,
    ) -> list[StreamInfo]:
        """depot의 전체 stream 트리를 구성한다.

        Args:
            depot: P4 depot 경로 (예: "//depot")
            default_branch: mainline의 Git branch 이름
            include_deleted: 삭제된 stream 포함 여부

        Returns:
            root StreamInfo 목록 (트리 구조)
        """
        raw_streams = self._p4.get_streams(depot)
        nodes: dict[str, StreamInfo] = {}

        for raw in raw_streams:
            stream_path = raw.get("Stream", "")
            stream_type = raw.get("Type", "unknown")
            parent = raw.get("Parent", "none")
            name = raw.get("Name", stream_path.split("/")[-1])

            if parent == "none":
                parent = None

            # 삭제 여부 확인
            is_deleted = stream_type == "deleted"
            if is_deleted and not include_deleted:
                continue

            branch = self._stream_to_branch(
                stream_path, stream_type, default_branch,
            )

            info = StreamInfo(
                stream=stream_path,
                name=name,
                stream_type=stream_type,
                parent=parent,
                branch=branch,
                deleted=is_deleted,
            )

            # CL 통계 조회
            try:
                changes = self._p4.get_all_changes(stream_path)
                info.change_count = len(changes)
                if changes:
                    info.first_cl = changes[0]
                    info.last_cl = changes[-1]
            except Exception:
                pass

            # 분기점 결정
            if parent and not is_deleted:
                try:
                    first_cls = self._p4.get_changes_after(stream_path, 0)
                    if first_cls:
                        info.branch_point_cl = first_cls[0] - 1
                except Exception:
                    pass

            nodes[stream_path] = info

        # parent-child 관계 연결
        roots = []
        for node in nodes.values():
            if node.parent and node.parent in nodes:
                nodes[node.parent].children.append(node)
            else:
                roots.append(node)

        # children을 이름순 정렬
        for node in nodes.values():
            node.children.sort(key=lambda n: n.stream)

        return roots

    def format_tree(self, roots: list[StreamInfo]) -> str:
        """트리를 텍스트로 포맷팅한다."""
        lines = []
        for i, root in enumerate(roots):
            is_last = i == len(roots) - 1
            self._format_node(root, lines, "", is_last)
        return "\n".join(lines)

    def format_summary(self, roots: list[StreamInfo]) -> str:
        """트리 요약 정보를 포맷팅한다."""
        all_nodes = self._flatten(roots)
        total_cls = sum(n.change_count for n in all_nodes)
        deleted = sum(1 for n in all_nodes if n.deleted)

        lines = [
            "",
            f"Stream: {len(all_nodes)}개"
            + (f" (삭제됨: {deleted}개)" if deleted else ""),
            f"총 Changelist: {total_cls:,}개",
            "",
            "Git Branch 매핑:",
        ]
        for node in all_nodes:
            status = " [삭제됨]" if node.deleted else ""
            cl_range = ""
            if node.first_cl and node.last_cl:
                cl_range = f" (CL {node.first_cl}~{node.last_cl}, {node.change_count:,}건)"
            lines.append(f"  {node.stream} → {node.branch}{cl_range}{status}")

        return "\n".join(lines)

    def _format_node(
        self,
        node: StreamInfo,
        lines: list[str],
        prefix: str,
        is_last: bool,
    ) -> None:
        """단일 노드를 트리 형태로 포맷팅."""
        connector = "└── " if is_last else "├── "

        # 노드 정보
        type_badge = f"[{node.stream_type}]"
        deleted_badge = " [삭제됨]" if node.deleted else ""
        cl_info = ""
        if node.change_count > 0:
            cl_info = f" ({node.change_count:,} CL)"
        branch_info = f" → {node.branch}"
        branch_point = ""
        if node.branch_point_cl:
            branch_point = f" (분기점: CL {node.branch_point_cl})"

        line = (
            f"{prefix}{connector}{node.name} "
            f"{type_badge}{branch_info}{cl_info}{branch_point}{deleted_badge}"
        )
        lines.append(line)

        # children
        child_prefix = prefix + ("    " if is_last else "│   ")
        for i, child in enumerate(node.children):
            child_is_last = i == len(node.children) - 1
            self._format_node(child, lines, child_prefix, child_is_last)

    def _flatten(self, roots: list[StreamInfo]) -> list[StreamInfo]:
        """트리를 BFS로 평탄화."""
        result = []
        queue = list(roots)
        while queue:
            node = queue.pop(0)
            result.append(node)
            queue.extend(node.children)
        return result

    @staticmethod
    def _stream_to_branch(
        stream: str, stream_type: str, default_branch: str,
    ) -> str:
        if stream_type == "mainline":
            return default_branch
        parts = stream.rstrip("/").split("/")
        if len(parts) > 3:
            return "/".join(parts[3:])
        return parts[-1]
