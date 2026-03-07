from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SyncEvent:
    """동기화 이벤트 기본 클래스. (cl, priority)로 전역 정렬."""

    cl: int
    stream: str
    priority: int  # BranchCreate=0, Changelist=1

    def sort_key(self) -> tuple[int, int]:
        return (self.cl, self.priority)


@dataclass
class BranchCreateEvent(SyncEvent):
    """새 stream의 분기점에서 Git branch를 생성하는 이벤트."""

    parent_stream: str = ""
    branch: str = ""

    def __init__(
        self, cl: int, stream: str, parent_stream: str, branch: str,
    ) -> None:
        super().__init__(cl=cl, stream=stream, priority=0)
        self.parent_stream = parent_stream
        self.branch = branch


@dataclass
class ChangelistEvent(SyncEvent):
    """일반 changelist 동기화 이벤트."""

    branch: str = ""

    def __init__(self, cl: int, stream: str, branch: str) -> None:
        super().__init__(cl=cl, stream=stream, priority=1)
        self.branch = branch
