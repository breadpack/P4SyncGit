from pathlib import Path

import pytest
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.lfs.lfs_pointer_utils import is_lfs_pointer
from p4gitsync.config.lfs_config import LfsConfig


class TestCommitBuilderLfsRouting:
    @pytest.fixture
    def lfs_store(self, tmp_path: Path) -> LfsObjectStore:
        return LfsObjectStore(tmp_path / "repo.git")

    @pytest.fixture
    def lfs_config(self) -> LfsConfig:
        return LfsConfig(enabled=True, extensions=[".png", ".uasset"])

    def test_lfs_target_uses_store(self, lfs_store: LfsObjectStore, lfs_config: LfsConfig):
        src = lfs_store.tmp_dir / "test.png"
        src.write_bytes(b"fake png binary data")
        pointer = lfs_store.store_from_file(src)
        assert is_lfs_pointer(pointer.pointer_bytes)
        assert lfs_store.exists(pointer.oid)

    def test_non_lfs_target_unchanged(self, lfs_config: LfsConfig):
        assert lfs_config.is_lfs_target("src/main.py") is False
        assert lfs_config.is_lfs_target("readme.txt") is False

    def test_lfs_target_extensions(self, lfs_config: LfsConfig):
        assert lfs_config.is_lfs_target("art/texture.png") is True
        assert lfs_config.is_lfs_target("content/map.uasset") is True
