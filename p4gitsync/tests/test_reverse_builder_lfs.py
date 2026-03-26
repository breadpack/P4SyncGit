from pathlib import Path

import pytest
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.lfs.lfs_pointer_utils import is_lfs_pointer


class TestResolveLfsContent:
    @pytest.fixture
    def store(self, tmp_path: Path) -> LfsObjectStore:
        return LfsObjectStore(tmp_path / "repo.git")

    def test_non_lfs_content_unchanged(self, store: LfsObjectStore):
        content = b"normal file content"
        assert not is_lfs_pointer(content)

    def test_lfs_pointer_resolves_to_path(self, store: LfsObjectStore):
        original = b"large binary asset data"
        pointer = store.store_from_stream(iter([original]))
        path = store.retrieve(pointer.oid)
        assert path.read_bytes() == original

    def test_missing_oid_raises(self, store: LfsObjectStore):
        with pytest.raises(FileNotFoundError):
            store.retrieve("0" * 64)
