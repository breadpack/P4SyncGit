from __future__ import annotations

import logging

logger = logging.getLogger("p4gitsync.virtual_stream_filter")


class VirtualStreamFilter:
    """Virtual stream의 exclude 패턴으로 depot path를 필터링."""

    def __init__(self, parent_stream: str, exclude_patterns: list[str]) -> None:
        self._parent_prefix = parent_stream + "/"
        self._parent_prefix_len = len(self._parent_prefix)
        self._excludes = exclude_patterns

    def is_included(self, depot_path: str) -> bool:
        """depot_path가 virtual stream view에 포함되는지 확인."""
        if not depot_path.startswith(self._parent_prefix):
            return False

        rel_path = depot_path[self._parent_prefix_len:]
        for exclude in self._excludes:
            if rel_path.startswith(exclude):
                return False
        return True

    @property
    def parent_stream(self) -> str:
        return self._parent_prefix[:-1]  # strip trailing /

    @property
    def parent_prefix_len(self) -> int:
        return self._parent_prefix_len
