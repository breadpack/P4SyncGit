from __future__ import annotations

import logging
import os
from pathlib import Path

from p4gitsync.config.lfs_config import LfsConfig
from p4gitsync.git.commit_metadata import CommitMetadata, IntegrationCommitInfo
from p4gitsync.git.git_operator import GitOperator
from p4gitsync.p4.merge_analyzer import MergeAnalyzer, MergeInfo
from p4gitsync.p4.p4_change_info import P4ChangeInfo
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.p4.p4_file_action import ADD_EDIT_ACTIONS, DELETE_ACTIONS, P4FileAction
from p4gitsync.p4.path_utils import depot_to_git_path
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.commit_builder")


class CommitBuilder:
    """P4 changelist를 Git commit으로 변환."""

    def __init__(
        self,
        p4_client: P4Client,
        git_operator: GitOperator,
        state_store: StateStore,
        stream: str,
        stream_prefix_len: int | None = None,
        lfs_config: LfsConfig | None = None,
        merge_analyzer: MergeAnalyzer | None = None,
        batch_print_threshold: int = 50,
    ) -> None:
        self._p4 = p4_client
        self._git = git_operator
        self._state = state_store
        self._stream = stream
        if stream_prefix_len is not None:
            self._stream_prefix_len = stream_prefix_len
        else:
            self._stream_prefix_len = len(stream) + 1
        self._lfs = lfs_config
        self._merge_analyzer = merge_analyzer
        self._last_has_integration = False
        self._lfs_initialized = False

    @property
    def last_has_integration(self) -> bool:
        return self._last_has_integration
        self._batch_print_threshold = batch_print_threshold

    def build_commit(
        self,
        info: P4ChangeInfo,
        branch: str,
        parent_sha: str | None,
    ) -> str:
        """P4 changelist 정보를 기반으로 Git commit을 생성하고 SHA 반환."""
        file_changes, deletes = self._extract_file_changes(info)

        merge_info = self._analyze_merge(info)
        integration_info = self._build_integration_info(merge_info)
        self._last_has_integration = merge_info is not None and merge_info.has_integration

        name, email = self._state.get_git_author(info.user)
        metadata = CommitMetadata(
            author_name=name,
            author_email=email,
            author_timestamp=info.timestamp,
            message=info.description,
            p4_changelist=info.changelist,
            integration_info=integration_info,
        )

        if merge_info and merge_info.has_integration:
            sha = self._try_merge_commit(
                merge_info, branch, parent_sha, metadata, file_changes, deletes,
            )
        else:
            sha = self._git.create_commit(
                branch, parent_sha, metadata, file_changes, deletes,
            )

        logger.info("CL %d -> commit %s", info.changelist, sha[:8])
        return sha

    def _analyze_merge(self, info: P4ChangeInfo) -> MergeInfo | None:
        """MergeAnalyzer가 설정되어 있으면 integration 분석을 수행."""
        if self._merge_analyzer is None:
            return None
        try:
            return self._merge_analyzer.analyze(info)
        except Exception:
            logger.warning("CL %d merge 분석 실패, 일반 commit으로 처리", info.changelist)
            return None

    def _build_integration_info(
        self, merge_info: MergeInfo | None,
    ) -> IntegrationCommitInfo | None:
        """MergeInfo로부터 commit message용 IntegrationCommitInfo 생성."""
        if merge_info is None or not merge_info.has_integration:
            return None
        return IntegrationCommitInfo(
            source_stream=merge_info.primary_source_stream,
            target_stream=self._stream,
            source_changelist=merge_info.source_changelist,
            integrated_files=len(merge_info.records),
        )

    def _try_merge_commit(
        self,
        merge_info: MergeInfo,
        branch: str,
        parent_sha: str | None,
        metadata: CommitMetadata,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str],
    ) -> str:
        """merge commit 생성 시도. source SHA가 없으면 일반 commit으로 fallback."""
        source_sha = None
        if merge_info.source_changelist is not None:
            source_sha = self._state.get_commit_sha(merge_info.source_changelist)

        if source_sha and parent_sha:
            parent_shas = [parent_sha, source_sha]
            sha = self._git.create_merge_commit(
                branch, parent_shas, metadata, file_changes, deletes,
            )
            logger.info(
                "CL %d -> merge commit (source: %s CL %d)",
                metadata.p4_changelist,
                merge_info.primary_source_stream,
                merge_info.source_changelist,
            )
            return sha

        if source_sha is None and merge_info.source_changelist is not None:
            logger.warning(
                "CL %d: source CL %d의 commit SHA 미발견, 일반 commit으로 fallback",
                metadata.p4_changelist,
                merge_info.source_changelist,
            )
        return self._git.create_commit(
            branch, parent_sha, metadata, file_changes, deletes,
        )

    def _extract_file_changes(
        self, info: P4ChangeInfo,
    ) -> tuple[list[tuple[str, bytes]], list[str]]:
        """changelist의 파일 변경 사항을 추출. 파일 수에 따라 batch/개별 모드 전환."""
        file_changes: list[tuple[str, bytes]] = []
        deletes: list[str] = []

        # 추가/편집 파일과 삭제 파일 분류
        add_edit_files: list[tuple[P4FileAction, str]] = []
        for fa in info.files:
            git_path = depot_to_git_path(fa.depot_path, self._stream, self._stream_prefix_len)
            if git_path is None:
                continue
            if fa.action in DELETE_ACTIONS:
                deletes.append(git_path)
            elif fa.action in ADD_EDIT_ACTIONS:
                add_edit_files.append((fa, git_path))

        # batch print 모드: 파일 수가 2개 이상이면 일괄 추출
        if len(add_edit_files) >= 2:
            file_specs = [
                f"{fa.depot_path}#{fa.revision}" for fa, _ in add_edit_files
            ]
            batch_results = self._p4.print_files_batch(file_specs)

            for fa, git_path in add_edit_files:
                content = batch_results.get(fa.depot_path)
                if content is not None:
                    if self._lfs and self._lfs.enabled and self._lfs.is_lfs_target(git_path):
                        content = LfsConfig.create_lfs_pointer(content)
                    file_changes.append((git_path, content))
                else:
                    logger.warning(
                        "파일 내용 추출 실패, 건너뜀: %s#%d", fa.depot_path, fa.revision
                    )
        else:
            for fa, git_path in add_edit_files:
                content = self._p4.print_file_to_bytes(fa.depot_path, fa.revision)
                if content is not None:
                    if self._lfs and self._lfs.enabled and self._lfs.is_lfs_target(git_path):
                        content = LfsConfig.create_lfs_pointer(content)
                    file_changes.append((git_path, content))
                else:
                    logger.warning(
                        "파일 내용 추출 실패, 건너뜀: %s#%d", fa.depot_path, fa.revision
                    )

        if self._lfs and self._lfs.enabled and not self._lfs_initialized:
            self._inject_lfs_config_files(file_changes)
            self._lfs_initialized = True

        return file_changes, deletes

    def _inject_lfs_config_files(
        self, file_changes: list[tuple[str, bytes]],
    ) -> None:
        """첫 commit에 .gitattributes와 .lfsconfig를 자동 삽입."""
        gitattributes = self._lfs.generate_gitattributes().encode("utf-8")
        file_changes.insert(0, (".gitattributes", gitattributes))
        logger.info("LFS .gitattributes 자동 생성 (확장자 %d개)", len(self._lfs.extensions))

        lfsconfig = self._lfs.generate_lfsconfig()
        if lfsconfig is not None:
            file_changes.insert(1, (".lfsconfig", lfsconfig.encode("utf-8")))
            logger.info("LFS .lfsconfig 자동 생성 (서버: %s)", self._lfs.server_url)
