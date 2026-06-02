from dataclasses import dataclass

DELETE_ACTIONS = frozenset({"delete", "move/delete", "purge"})
ADD_EDIT_ACTIONS = frozenset({"add", "edit", "branch", "integrate", "move/add"})


@dataclass
class P4FileAction:
    depot_path: str
    action: str          # add, edit, delete, move/add, move/delete, integrate, branch
    file_type: str       # text, binary, ...
    revision: int
    size: int | None = None  # 바이트. LFS 라우팅용으로 lazy 채움(p4 sizes). None=미상
