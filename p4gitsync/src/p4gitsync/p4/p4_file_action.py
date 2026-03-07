from dataclasses import dataclass

DELETE_ACTIONS = frozenset({"delete", "move/delete", "purge"})
ADD_EDIT_ACTIONS = frozenset({"add", "edit", "branch", "integrate", "move/add"})


@dataclass
class P4FileAction:
    depot_path: str
    action: str          # add, edit, delete, move/add, move/delete, integrate, branch
    file_type: str       # text, binary, ...
    revision: int
