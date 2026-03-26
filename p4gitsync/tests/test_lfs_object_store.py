from pathlib import Path

import pytest
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.lfs.lfs_pointer_utils import parse_lfs_pointer


@pytest.fixture
def git_dir(tmp_path: Path) -> Path:
    return tmp_path / "repo.git"


@pytest.fixture
def store(git_dir: Path) -> LfsObjectStore:
    return LfsObjectStore(git_dir)


class TestStoreFromStream:
    def test_stores_and_returns_pointer(self, store: LfsObjectStore):
        content = b"hello world binary content"
        pointer = store.store_from_stream(iter([content]))
        assert pointer.oid
        assert pointer.size == len(content)
        assert store.exists(pointer.oid)

    def test_multi_chunk(self, store: LfsObjectStore):
        chunks = [b"chunk1", b"chunk2", b"chunk3"]
        pointer = store.store_from_stream(iter(chunks))
        assert pointer.size == 18

    def test_idempotent(self, store: LfsObjectStore):
        content = b"same content"
        p1 = store.store_from_stream(iter([content]))
        p2 = store.store_from_stream(iter([content]))
        assert p1.oid == p2.oid

    def test_pointer_bytes_roundtrip(self, store: LfsObjectStore):
        content = b"test"
        pointer = store.store_from_stream(iter([content]))
        parsed = parse_lfs_pointer(pointer.pointer_bytes)
        assert parsed.oid == pointer.oid
        assert parsed.size == pointer.size

    def test_empty_content(self, store: LfsObjectStore):
        pointer = store.store_from_stream(iter([b""]))
        assert pointer.size == 0
        assert store.exists(pointer.oid)


class TestStoreFromFile:
    def test_stores_file_and_removes_source(self, store: LfsObjectStore, tmp_path: Path):
        src = tmp_path / "binary.dat"
        src.write_bytes(b"file content here")
        pointer = store.store_from_file(src)
        assert pointer.size == 17
        assert store.exists(pointer.oid)
        assert not src.exists()

    def test_idempotent(self, store: LfsObjectStore, tmp_path: Path):
        src1 = tmp_path / "a.dat"
        src1.write_bytes(b"same")
        p1 = store.store_from_file(src1)
        src2 = tmp_path / "b.dat"
        src2.write_bytes(b"same")
        p2 = store.store_from_file(src2)
        assert p1.oid == p2.oid


class TestRetrieve:
    def test_existing(self, store: LfsObjectStore):
        content = b"retrieve me"
        pointer = store.store_from_stream(iter([content]))
        path = store.retrieve(pointer.oid)
        assert path.read_bytes() == content

    def test_missing_raises(self, store: LfsObjectStore):
        with pytest.raises(FileNotFoundError):
            store.retrieve("0" * 64)


class TestObjectPath:
    def test_standard_layout(self, store: LfsObjectStore, git_dir: Path):
        oid = "ab" + "cd" + "ef" * 30
        expected = git_dir / "lfs" / "objects" / "ab" / "cd" / oid
        assert store.object_path(oid) == expected


class TestTmpDir:
    def test_tmp_dir_property(self, store: LfsObjectStore, git_dir: Path):
        assert store.tmp_dir == git_dir / "lfs" / "tmp"
        assert store.tmp_dir.exists()
