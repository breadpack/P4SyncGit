from dataclasses import dataclass, field

from p4gitsync.p4.p4_file_action import P4FileAction


@dataclass
class P4ChangeInfo:
    changelist: int
    user: str
    description: str
    timestamp: int
    files: list[P4FileAction] = field(default_factory=list)
