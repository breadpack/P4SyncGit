from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

from p4gitsync.config.lfs_config import LfsConfig
from p4gitsync.config.sync_config import InitialImportConfig
from p4gitsync.git.commit_metadata import CommitMetadata
from p4gitsync.git.fast_importer import FastImporter
from p4gitsync.p4.p4_change_info import P4ChangeInfo
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.p4.p4_file_action import ADD_EDIT_ACTIONS, DELETE_ACTIONS
from p4gitsync.p4.path_utils import depot_to_git_path
from p4gitsync.state.state_store import StateStore, StreamMapping

logger = logging.getLogger("p4gitsync.multi_stream_import")


@dataclass
class StreamNode:
    """Stream 계층 트리의 노드."""

    stream: str
    branch: str
    parent_stream: str | None = None
    branch_point_cl: int | None = None
    children: list[StreamNode] = field(default_factory=list)


class MultiStreamImporter:
    """다중 P4 Stream의 초기 히스토리를 branch 관계를 보존하여 Git에 import한다.

    1. P4 Stream 계층 조회
    2. 의존 순서 정렬 (parent → child)
    3. parent import → 분기점 commit SHA 확보
    4. child import → parent의 분기점에서 branch 생성 후 이어서 commit
    """

    def __init__(
        self,
        p4_client: P4Client,
        state_store: StateStore,
        repo_path: str,
        config: InitialImportConfig | None = None,
        lfs_config: LfsConfig | None = None,
        user_mapper=None,
    ) -> None:
        self._p4 = p4_client
        self._state = state_store
        self._repo_path = repo_path
        self._lfs = lfs_config
        self._user_mapper = user_mapper

        cfg = config or InitialImportConfig()
        self._checkpoint_interval = cfg.checkpoint_interval
        self._server_load_threshold = 50
        self._throttle_wait_seconds = 60

    def run(self, streams: list[str], default_branch: str = "main") -> None:
        """다중 stream import 실행.

        Args:
            streams: import할 P4 stream 경로 목록 (예: ["//depot/main", "//depot/develop"])
            default_branch: mainline stream의 Git branch 이름
        """
        # 1. Stream 계층 구성
        tree = self._build_stream_tree(streams, default_branch)
        import_order = self._flatten_tree(tree)

        logger.info(
            "다중 stream import 시작: %d streams, 순서=%s",
            len(import_order),
            [n.branch for n in import_order],
        )

        # 2. 순서대로 import
        for node in import_order:
            self._import_stream(node)

        logger.info("다중 stream import 완료: %d streams", len(import_order))

    def _build_stream_tree(
        self, streams: list[str], default_branch: str,
    ) -> list[StreamNode]:
        """P4 Stream 정보를 조회하여 계층 트리를 구성한다."""
        nodes: dict[str, StreamNode] = {}

        for stream in streams:
            info = self._p4.get_stream_info(stream)
            parent = info.get("Parent", "none")
            if parent == "none":
                parent = None

            # stream 이름에서 branch 이름 생성
            branch = self._stream_to_branch(stream, streams, default_branch)

            # 분기점 CL 결정: child stream의 첫 CL 직전
            branch_point_cl = None
            if parent and parent in streams:
                first_cls = self._p4.get_changes_after(stream, 0)
                if first_cls:
                    branch_point_cl = first_cls[0] - 1
                    # 더 정확한 방법: parent에서 branch_point_cl 이하의 마지막 CL 찾기
                    parent_cls = self._p4.get_all_changes(parent)
                    valid_cls = [cl for cl in parent_cls if cl <= branch_point_cl]
                    if valid_cls:
                        branch_point_cl = valid_cls[-1]

            node = StreamNode(
                stream=stream,
                branch=branch,
                parent_stream=parent if parent in streams else None,
                branch_point_cl=branch_point_cl,
            )
            nodes[stream] = node

        # parent-child 관계 연결
        roots = []
        for node in nodes.values():
            if node.parent_stream and node.parent_stream in nodes:
                nodes[node.parent_stream].children.append(node)
            else:
                roots.append(node)

        return roots

    def _flatten_tree(self, roots: list[StreamNode]) -> list[StreamNode]:
        """트리를 BFS로 평탄화하여 import 순서 결정 (parent 먼저)."""
        result = []
        queue = list(roots)
        while queue:
            node = queue.pop(0)
            result.append(node)
            queue.extend(node.children)
        return result

    def _stream_to_branch(
        self, stream: str, all_streams: list[str], default_branch: str,
    ) -> str:
        """P4 stream 경로를 Git branch 이름으로 변환.

        mainline(parent 없는 root)이면 default_branch, 나머지는 stream 이름의 마지막 부분.
        """
        info = self._p4.get_stream_info(stream)
        parent = info.get("Parent", "none")
        stream_type = info.get("Type", "").lower()

        # mainline stream
        if parent == "none" or stream_type == "mainline":
            return default_branch

        # 나머지: //depot/develop → develop, //depot/feature/FOO → feature/FOO
        parts = stream.rstrip("/").split("/")
        # //depot/name → parts = ['', '', 'depot', 'name']
        if len(parts) > 3:
            return "/".join(parts[3:])
        return parts[-1]

    def _import_stream(self, node: StreamNode) -> None:
        """단일 stream을 import한다. parent가 있으면 분기점에서 branch 생성."""
        last_cl = self._state.get_last_synced_cl(node.stream)
        all_changes = self._p4.get_changes_after(node.stream, last_cl)

        if not all_changes:
            logger.info("import 대상 CL 없음: %s (%s)", node.stream, node.branch)
            return

        logger.info(
            "stream import 시작: %s → %s (%d CL, 분기점=%s)",
            node.stream, node.branch, len(all_changes),
            f"CL {node.branch_point_cl}" if node.branch_point_cl else "root",
        )

        # 분기점에서 branch 생성
        if node.parent_stream and node.branch_point_cl:
            parent_sha = self._state.get_commit_sha(node.branch_point_cl, node.parent_stream)
            if parent_sha is None:
                # 정확한 CL이 없으면 직전 commit 사용
                parent_sha = self._state.get_last_commit_before(
                    node.parent_stream, node.branch_point_cl + 1,
                )
            if parent_sha and not parent_sha.startswith("fast-import:"):
                self._create_branch_at(node.branch, parent_sha)
                logger.info(
                    "branch '%s' 생성: parent=%s, 분기점 CL=%d, SHA=%s",
                    node.branch, node.parent_stream, node.branch_point_cl, parent_sha[:12],
                )

        # stream registry 등록
        self._state.register_stream(StreamMapping(
            stream=node.stream,
            branch=node.branch,
            parent_stream=node.parent_stream,
            branch_point_cl=node.branch_point_cl,
        ))

        # fast-import 실행
        stream_prefix_len = len(node.stream) + 1
        fast_importer = FastImporter(self._repo_path)
        fast_importer.start()

        try:
            for i, cl in enumerate(all_changes):
                self._throttle_if_needed()

                info = self._p4.describe(cl)
                files, deletes = self._extract_files(info, node.stream, stream_prefix_len)

                # 첫 CL에서 LFS 설정 파일 삽입
                if i == 0 and self._lfs and self._lfs.enabled:
                    gitattributes = self._lfs.generate_gitattributes().encode("utf-8")
                    files.insert(0, (".gitattributes", gitattributes))
                    lfsconfig = self._lfs.generate_lfsconfig()
                    if lfsconfig is not None:
                        files.insert(1, (".lfsconfig", lfsconfig.encode("utf-8")))

                # author 매핑
                if self._user_mapper:
                    author = self._user_mapper.p4_to_git({
                        "user": info.user,
                        "workspace": info.workspace,
                        "description": info.description,
                        "changelist": cl,
                    })
                    name, email = author["name"], author["email"]
                else:
                    name, email = self._state.get_git_author(info.user)

                metadata = CommitMetadata(
                    author_name=name,
                    author_email=email,
                    author_timestamp=info.timestamp,
                    message=info.description,
                    p4_changelist=cl,
                )
                mark = fast_importer.add_commit(node.branch, metadata, files, deletes)

                if (i + 1) % self._checkpoint_interval == 0:
                    fast_importer.checkpoint()
                    self._state.set_last_synced_cl(
                        node.stream, cl, f"fast-import:mark:{mark}",
                    )
                    self._state.record_commit(
                        cl, f"fast-import:mark:{mark}", node.stream, node.branch,
                    )
                    logger.info(
                        "체크포인트: %s CL %d (%d/%d)",
                        node.branch, cl, i + 1, len(all_changes),
                    )

                if (i + 1) % 100 == 0:
                    logger.info(
                        "진행 [%s]: %d/%d CL", node.branch, i + 1, len(all_changes),
                    )

        finally:
            fast_importer.finish()

        self._post_import(node, all_changes)
        logger.info(
            "stream import 완료: %s → %s (%d CL)",
            node.stream, node.branch, len(all_changes),
        )

    def _extract_files(
        self,
        info: P4ChangeInfo,
        stream: str,
        prefix_len: int,
    ) -> tuple[list[tuple[str, bytes]], list[str]]:
        """changelist에서 파일 변경사항을 추출한다."""
        files: list[tuple[str, bytes]] = []
        deletes: list[str] = []

        for fa in info.files:
            git_path = depot_to_git_path(fa.depot_path, stream, prefix_len)
            if git_path is None:
                continue

            if fa.action in DELETE_ACTIONS:
                deletes.append(git_path)
            elif fa.action in ADD_EDIT_ACTIONS:
                content = self._p4.print_file_to_bytes(fa.depot_path, fa.revision)
                if content is not None:
                    if self._lfs and self._lfs.enabled and self._lfs.is_lfs_target(git_path):
                        content = LfsConfig.create_lfs_pointer(content)
                    files.append((git_path, content))

        return files, deletes

    def _create_branch_at(self, branch: str, commit_sha: str) -> None:
        """특정 commit에서 Git branch를 생성한다."""
        result = subprocess.run(
            ["git", "branch", branch, commit_sha],
            cwd=self._repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("branch 생성 실패: %s — %s", branch, result.stderr)

    def _post_import(self, node: StreamNode, all_changes: list[int]) -> None:
        """import 완료 후 Git SHA 매핑 및 gc."""
        result = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{node.branch}"],
            cwd=self._repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            head_sha = result.stdout.strip()
            last_cl = all_changes[-1]
            self._state.set_last_synced_cl(node.stream, last_cl, head_sha)
            self._state.record_commit(last_cl, head_sha, node.stream, node.branch)
            logger.info(
                "import 후 HEAD [%s]: %s (CL %d)",
                node.branch, head_sha[:12], last_cl,
            )

        subprocess.run(
            ["git", "gc", "--auto"],
            cwd=self._repo_path,
            capture_output=True,
        )

    def _throttle_if_needed(self) -> None:
        """P4 서버 과부하 시 대기."""
        import time
        try:
            if self._p4.check_server_load(self._server_load_threshold):
                logger.warning(
                    "P4 서버 과부하. %d초 대기.", self._throttle_wait_seconds,
                )
                time.sleep(self._throttle_wait_seconds)
        except Exception:
            logger.exception("서버 부하 확인 중 오류")
