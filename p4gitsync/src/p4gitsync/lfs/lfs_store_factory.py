"""LfsObjectStore 생성 공용 팩토리.

`__main__` import 핸들러와 `cutover._initialize` 가 동일한 규약으로
LfsObjectStore 를 생성하도록 중복을 제거한다. AppConfig 대신 primitive 를
받아 config 결합도를 낮춘다.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from p4gitsync.lfs.lfs_object_store import LfsObjectStore


def build_lfs_store(
    repo_path: str, bare: bool, enabled: bool
) -> "LfsObjectStore | None":
    """LFS 활성 시 LfsObjectStore 를 생성한다(비활성 시 None).

    git_dir 산정: bare repo 면 repo_path 자체, 아니면 repo_path/".git".
    """
    if not enabled:
        return None
    from p4gitsync.lfs.lfs_object_store import LfsObjectStore

    path = Path(repo_path)
    git_dir = path if bare else path / ".git"
    return LfsObjectStore(git_dir=git_dir)
