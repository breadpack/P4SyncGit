from __future__ import annotations

import re
from dataclasses import dataclass

LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"

_OID_RE = re.compile(r"oid sha256:([0-9a-f]{64})")
_SIZE_RE = re.compile(r"size (\d+)")


@dataclass(frozen=True)
class LfsPointer:
    """Git LFS pointer metadata."""

    oid: str
    size: int
    pointer_bytes: bytes


def format_lfs_pointer(oid: str, size: int) -> bytes:
    """oid + size -> 표준 LFS 포인터 텍스트. 유일한 포맷 생성 지점."""
    return (
        f"version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        f"size {size}\n"
    ).encode("utf-8")


def is_lfs_pointer(content: bytes) -> bool:
    """콘텐츠가 LFS 포인터인지 판별."""
    return content.startswith(LFS_POINTER_PREFIX + b"\n")


def parse_lfs_pointer(content: bytes) -> LfsPointer:
    """포인터 텍스트에서 oid, size 추출. 포맷 불일치 시 ValueError."""
    text = content.decode("utf-8", errors="replace")

    size_match = _SIZE_RE.search(text)
    if not size_match:
        raise ValueError(f"LFS pointer에 size가 없습니다: {text[:80]}")

    oid_match = _OID_RE.search(text)
    if not oid_match:
        raise ValueError(f"LFS pointer에 oid가 없습니다: {text[:80]}")

    return LfsPointer(
        oid=oid_match.group(1),
        size=int(size_match.group(1)),
        pointer_bytes=content,
    )
