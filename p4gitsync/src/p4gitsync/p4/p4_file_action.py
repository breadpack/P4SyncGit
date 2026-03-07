from dataclasses import dataclass


@dataclass
class P4FileAction:
    depot_path: str
    action: str          # add, edit, delete, move/add, move/delete, integrate, branch
    file_type: str       # text, binary, ...
    revision: int
