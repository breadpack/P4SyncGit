from __future__ import annotations

import logging
from dataclasses import dataclass, field

from p4gitsync.config.sync_config import StreamPolicy
from p4gitsync.git.git_operator import GitOperator
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.state.state_store import StateStore, StreamMapping

logger = logging.getLogger("p4gitsync.stream_watcher")


@dataclass
class P4StreamInfo:
    """P4 stream 정보."""

    stream: str  # //ProjectSTAR/dev
    type: str  # development, release, mainline
    parent: str | None
    first_changelist: int


@dataclass
class StreamChanges:
    """stream 변경 감지 결과."""

    created: list[P4StreamInfo] = field(default_factory=list)
    deleted: list[P4StreamInfo] = field(default_factory=list)


class StreamWatcher:
    """P4 stream 생성/삭제를 감지하고 Git branch를 관리."""

    def __init__(
        self,
        p4_client: P4Client,
        state_store: StateStore,
        git_operator: GitOperator,
        depot: str,
        policy: StreamPolicy | None = None,
    ) -> None:
        self._p4 = p4_client
        self._state = state_store
        self._git = git_operator
        self._depot = depot
        self._policy = policy or StreamPolicy()

    def detect_changes(self) -> StreamChanges:
        """P4 서버의 stream 목록과 등록된 stream을 비교하여 변경 감지."""
        p4_streams = self._fetch_p4_streams()
        registered = {m.stream for m in self._state.get_all_registered_streams()}

        changes = StreamChanges()

        for info in p4_streams:
            if info.stream not in registered:
                if not self._policy.should_include(info.stream, info.type):
                    logger.debug("stream '%s' (type=%s) 필터링 제외", info.stream, info.type)
                    continue
                changes.created.append(info)

        p4_stream_names = {s.stream for s in p4_streams}
        for stream_name in registered:
            if stream_name not in p4_stream_names:
                info = P4StreamInfo(
                    stream=stream_name, type="unknown", parent=None, first_changelist=0,
                )
                changes.deleted.append(info)

        if changes.created:
            logger.info("새 stream %d개 감지: %s",
                        len(changes.created),
                        [s.stream for s in changes.created])
        if changes.deleted:
            logger.info("삭제된 stream %d개 감지: %s",
                        len(changes.deleted),
                        [s.stream for s in changes.deleted])

        return changes

    def handle_created_stream(self, info: P4StreamInfo) -> None:
        """새 stream에 대한 Git branch 생성 및 등록."""
        branch = self._stream_to_branch(info.stream)

        if info.type == "mainline" or info.parent is None:
            self._git.create_orphan_branch(branch)
            self._state.register_stream(StreamMapping(
                stream=info.stream,
                branch=branch,
                parent_stream=None,
                branch_point_cl=None,
            ))
            logger.info("mainline stream 등록: %s -> %s", info.stream, branch)
            return

        parent_sha = self._state.get_last_commit_before(
            info.parent, info.first_changelist,
        )

        if parent_sha:
            self._git.create_branch(branch, parent_sha)
        else:
            parent_head = self._git.get_head_sha(
                self._get_parent_branch(info.parent),
            )
            if parent_head:
                self._git.create_branch(branch, parent_head)
            else:
                self._git.create_orphan_branch(branch)
                logger.warning(
                    "parent stream '%s'에 commit 없음 — orphan branch 생성: %s",
                    info.parent, branch,
                )

        self._state.register_stream(StreamMapping(
            stream=info.stream,
            branch=branch,
            parent_stream=info.parent,
            branch_point_cl=info.first_changelist if info.first_changelist > 0 else None,
        ))
        logger.info(
            "stream 등록: %s -> %s (parent=%s, branch_point_cl=%d)",
            info.stream, branch, info.parent, info.first_changelist,
        )

    def handle_deleted_stream(self, stream: str) -> None:
        """삭제된 stream 비활성화. Git branch는 보존, 폴링에서 제외."""
        mapping = self._state.get_stream_mapping(stream)
        if mapping is None:
            logger.warning("삭제 처리 대상 stream '%s'이 등록되어 있지 않음", stream)
            return

        logger.info(
            "stream 비활성화: %s (branch '%s' 보존, 폴링 제외)",
            stream, mapping.branch,
        )

    def _fetch_p4_streams(self) -> list[P4StreamInfo]:
        """P4 서버에서 depot의 모든 stream 정보 조회."""
        raw_streams = self._p4.get_streams(self._depot)
        result = []

        for s in raw_streams:
            stream_path = s.get("Stream", "")
            stream_type = s.get("Type", "unknown")
            parent = s.get("Parent", None)
            if parent == "none":
                parent = None

            first_cl = self._get_first_changelist(stream_path)

            result.append(P4StreamInfo(
                stream=stream_path,
                type=stream_type,
                parent=parent,
                first_changelist=first_cl,
            ))

        return result

    def _get_first_changelist(self, stream: str) -> int:
        """stream의 최초 submitted changelist 조회."""
        changes = self._p4.get_all_changes(stream)
        return changes[0] if changes else 0

    def _stream_to_branch(self, stream: str) -> str:
        """P4 stream 경로를 Git branch 이름으로 변환.

        //depot/main -> main
        //depot/dev  -> dev
        """
        parts = stream.strip("/").split("/")
        if len(parts) >= 2:
            return parts[-1]
        return stream.replace("/", "_").strip("_")

    def _get_parent_branch(self, parent_stream: str) -> str:
        """parent stream의 Git branch 이름 조회."""
        mapping = self._state.get_stream_mapping(parent_stream)
        if mapping:
            return mapping.branch
        return self._stream_to_branch(parent_stream)
