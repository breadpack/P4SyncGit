from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from p4gitsync.p4.p4_change_info import P4ChangeInfo

logger = logging.getLogger("p4gitsync.merge_analyzer")

INTEGRATION_ACTIONS = frozenset({"integrate", "branch", "copy", "merge"})

_STREAM_PATH_RE = re.compile(r"^(//[^/]+/[^/]+)/")


@dataclass
class IntegrationRecord:
    source_depot_path: str
    target_depot_path: str
    source_revision: int
    source_stream: str


@dataclass
class MergeInfo:
    has_integration: bool
    primary_source_stream: str | None = None
    source_changelist: int | None = None
    records: list[IntegrationRecord] = field(default_factory=list)


class MergeAnalyzerProtocol(Protocol):
    def analyze(self, change_info: P4ChangeInfo) -> MergeInfo: ...


def _extract_stream_from_depot_path(depot_path: str) -> str | None:
    """depot path에서 stream 경로를 추출.

    예: //depot/main/src/foo.py -> //depot/main
    """
    m = _STREAM_PATH_RE.match(depot_path)
    return m.group(1) if m else None


class MergeAnalyzer:
    """P4 changelist의 integration 정보를 분석하여 merge 여부를 판단."""

    def __init__(self, p4_client) -> None:
        self._p4 = p4_client

    def analyze(self, change_info: P4ChangeInfo) -> MergeInfo:
        """changelist의 파일 action을 분석하여 MergeInfo 반환."""
        integration_files = [
            fa for fa in change_info.files
            if fa.action in INTEGRATION_ACTIONS
        ]

        if not integration_files:
            return MergeInfo(has_integration=False)

        depot_paths = [fa.depot_path for fa in integration_files]
        try:
            filelog_results = self._p4.run_filelog(depot_paths)
        except Exception:
            logger.warning(
                "CL %d filelog 조회 실패, 일반 commit으로 처리",
                change_info.changelist,
            )
            return MergeInfo(has_integration=False)

        records = self._parse_filelog_results(filelog_results, change_info.changelist)

        if not records:
            return MergeInfo(has_integration=False)

        stream_counts: dict[str, int] = {}
        max_source_cl = 0

        for rec in records:
            stream_counts[rec.source_stream] = stream_counts.get(rec.source_stream, 0) + 1
            if rec.source_revision > max_source_cl:
                max_source_cl = rec.source_revision

        primary_source_stream = max(stream_counts, key=stream_counts.get)

        return MergeInfo(
            has_integration=True,
            primary_source_stream=primary_source_stream,
            source_changelist=max_source_cl if max_source_cl > 0 else None,
            records=records,
        )

    def _parse_filelog_results(
        self, filelog_results: list, target_changelist: int,
    ) -> list[IntegrationRecord]:
        """filelog 결과에서 integration record를 추출."""
        records: list[IntegrationRecord] = []

        for entry in filelog_results:
            try:
                target_depot_path = entry.depotFile
                revisions = entry.revisions
            except AttributeError:
                continue

            target_rev = self._find_revision_for_cl(revisions, target_changelist)
            if target_rev is None:
                continue

            try:
                integrations = target_rev.integrations
            except AttributeError:
                continue

            for integ in integrations:
                try:
                    how = integ.how
                    source_file = integ.file
                    source_rev_end = integ.erev
                except AttributeError:
                    continue

                if "from" not in how:
                    continue

                source_stream = _extract_stream_from_depot_path(source_file)
                if source_stream is None:
                    logger.debug(
                        "source stream 추출 실패: %s", source_file,
                    )
                    continue

                source_rev_num = self._parse_revision_number(source_rev_end)

                source_cl = self._get_source_changelist(source_file, source_rev_num)
                if source_cl is None:
                    logger.warning(
                        "source CL 조회 실패 (obliterate?): %s#%s",
                        source_file, source_rev_end,
                    )
                    continue

                records.append(IntegrationRecord(
                    source_depot_path=source_file,
                    target_depot_path=target_depot_path,
                    source_revision=source_cl,
                    source_stream=source_stream,
                ))

        return records

    def _find_revision_for_cl(self, revisions, target_changelist: int):
        """filelog revision 목록에서 target CL에 해당하는 revision을 찾는다."""
        for rev in revisions:
            try:
                if rev.change == target_changelist:
                    return rev
            except AttributeError:
                continue
        return None

    def _get_source_changelist(self, depot_path: str, revision: int) -> int | None:
        """source 파일의 특정 revision에 해당하는 changelist 번호를 조회."""
        if revision <= 0:
            return None
        try:
            results = self._p4.run_filelog([f"{depot_path}#{revision}"])
            if results:
                entry = results[0]
                for rev in entry.revisions:
                    if rev.rev == revision:
                        return rev.change
        except Exception:
            logger.debug(
                "source filelog 조회 실패: %s#%d", depot_path, revision,
            )
        return None

    @staticmethod
    def _parse_revision_number(rev_str: str) -> int:
        """'#3' 같은 revision 문자열에서 숫자만 추출."""
        if isinstance(rev_str, int):
            return rev_str
        cleaned = str(rev_str).lstrip("#")
        try:
            return int(cleaned)
        except (ValueError, TypeError):
            return 0
