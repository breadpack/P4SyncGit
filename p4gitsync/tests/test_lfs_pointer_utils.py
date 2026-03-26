import pytest
from p4gitsync.lfs.lfs_pointer_utils import (
    format_lfs_pointer,
    is_lfs_pointer,
    parse_lfs_pointer,
)


class TestFormatLfsPointer:
    def test_format_produces_valid_pointer(self):
        oid = "abc123" * 10 + "abcd"
        result = format_lfs_pointer(oid, 12345)
        assert result.startswith(b"version https://git-lfs.github.com/spec/v1\n")
        assert f"oid sha256:{oid}".encode() in result
        assert b"size 12345\n" in result

    def test_format_ends_with_newline(self):
        oid = "a" * 64
        result = format_lfs_pointer(oid, 1)
        assert result.endswith(b"\n")

    def test_format_zero_size(self):
        oid = "0" * 64
        result = format_lfs_pointer(oid, 0)
        assert b"size 0\n" in result


class TestIsLfsPointer:
    def test_valid_pointer(self):
        pointer = format_lfs_pointer("a" * 64, 100)
        assert is_lfs_pointer(pointer) is True

    def test_regular_content(self):
        assert is_lfs_pointer(b"hello world") is False

    def test_empty_content(self):
        assert is_lfs_pointer(b"") is False

    def test_partial_prefix(self):
        assert is_lfs_pointer(b"version https://git-lfs") is False


class TestParseLfsPointer:
    def test_roundtrip(self):
        oid = "abcdef01" * 8
        size = 999999
        pointer_bytes = format_lfs_pointer(oid, size)
        parsed = parse_lfs_pointer(pointer_bytes)
        assert parsed.oid == oid
        assert parsed.size == size
        assert parsed.pointer_bytes == pointer_bytes

    def test_malformed_missing_oid(self):
        bad = b"version https://git-lfs.github.com/spec/v1\nsize 100\n"
        with pytest.raises(ValueError, match="oid"):
            parse_lfs_pointer(bad)

    def test_malformed_missing_size(self):
        bad = b"version https://git-lfs.github.com/spec/v1\noid sha256:aaa\n"
        with pytest.raises(ValueError, match="size"):
            parse_lfs_pointer(bad)

    def test_not_a_pointer(self):
        with pytest.raises(ValueError):
            parse_lfs_pointer(b"not a pointer")
