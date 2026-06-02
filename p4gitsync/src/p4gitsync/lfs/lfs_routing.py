"""LFS 라우팅 판정 — P4 file_type(text/binary) + 크기 임계 기반.

확장자 화이트리스트만으로는 잡히지 않는 대형 binary(확장자 없는 빌드 산출물,
`.pak`/`.uexp` 등)가 git 본문(fast-import inline)에 영구히 박히는 것을 막기 위해,
P4가 파일 add 시점에 매긴 ``file_type``과 파일 크기로 LFS 라우팅을 결정한다.

순수 판정 함수(``classify_p4_file_type``/``sniff_is_binary``/``decide_lfs_route``)는
P4 의존 없이 단위 테스트된다. ``partition_for_lfs``만 P4Client(덕타이핑)에 의존한다.

라우팅 규칙: **확장자 화이트리스트 매칭 OR (binary 타입 AND size >= 임계)**.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from p4gitsync.config.lfs_config import LfsConfig
    from p4gitsync.p4.p4_client import P4Client
    from p4gitsync.p4.p4_file_action import P4FileAction


# P4 base type 분류표. modifier(+l/+x/+S/+F 등)를 떼어낸 base 로 판정한다.
_TEXT_BASE_TYPES = frozenset({"text", "unicode", "utf8", "utf16", "symlink"})
_BINARY_BASE_TYPES = frozenset({"binary", "apple", "resource", "ubinary"})

# UNKNOWN 타입 보정용 null-byte 샘플링 기본 prefix 크기.
SNIFF_PREFIX_SIZE = 8192


class P4TypeClass(enum.Enum):
    """P4 file_type 의 거시적 분류."""

    TEXT = "text"
    BINARY = "binary"
    UNKNOWN = "unknown"  # 빈 문자열/모르는 타입 → null-byte sniff 로 보정


def classify_p4_file_type(file_type: str) -> P4TypeClass:
    """P4 file_type 문자열의 base 타입을 분류한다.

    modifier(``+l``/``+x``/``+S``/``+F`` 등)를 떼어낸 base 로 판정한다.
    레거시 실행 접두사(``xtext``/``xbinary``)도 처리한다.

    예: ``binary+l`` → BINARY, ``xtext`` → TEXT, ``text+x`` → TEXT,
    ``""``/``"weirdtype"`` → UNKNOWN.
    """
    if not file_type:
        return P4TypeClass.UNKNOWN
    ft = file_type.lower()
    if "symlink" in ft:
        return P4TypeClass.TEXT
    base, _, _mods = ft.partition("+")
    # 레거시 실행 접두사 제거: xtext/xbinary → text/binary
    if (
        base.startswith("x")
        and base not in _BINARY_BASE_TYPES
        and base not in _TEXT_BASE_TYPES
    ):
        base = base[1:]
    if base in _TEXT_BASE_TYPES:
        return P4TypeClass.TEXT
    if base in _BINARY_BASE_TYPES:
        return P4TypeClass.BINARY
    return P4TypeClass.UNKNOWN


def sniff_is_binary(head: bytes) -> bool:
    """파일 앞부분 prefix 에 NUL 바이트가 있으면 binary 로 간주한다.

    호출자가 이미 읽은 prefix(예: 앞 ``SNIFF_PREFIX_SIZE`` 바이트)를 넘긴다.
    이 함수 자체는 I/O 를 하지 않는 순수 함수다.
    """
    return b"\x00" in head


def decide_lfs_route(
    *,
    git_path: str,
    file_type: str,
    size: int | None,
    cfg: "LfsConfig",
    sniff_head: bytes | None = None,
) -> bool:
    """파일을 LFS 로 보낼지 결정한다.

    규칙: **확장자 화이트리스트 매칭 OR (binary 타입 AND size >= 임계)**.

    - 확장자 매칭이면 size/타입 무관 즉시 True(저비용).
    - ``auto_detect_binary`` 가 꺼져 있으면 확장자 전용(기존 동작과 동일).
    - 타입이 UNKNOWN 이면 ``sniff_head`` 의 NUL 검사로 binary 여부를 보정한다.
      sniff_head 가 없으면(대형/미상이라 다운로드를 생략한 경우) size 로 보수적
      판단한다: 임계 이상/미상이면 binary 로 간주(안전), 미만이면 text 로 본다.
    - binary 확정인데 ``size`` 가 ``None``(미상)이면 안전하게 True(대형 가정).
      5TB depot 에서 대형 blob 이 git 본문에 박히는 것이 보더라인 small binary 가
      LFS 포인터화되는 것보다 훨씬 위험하기 때문.
    """
    if cfg.is_lfs_target_ext(git_path):
        return True
    if not cfg.auto_detect_binary:
        return False

    threshold = cfg.effective_binary_threshold
    type_class = classify_p4_file_type(file_type)
    if type_class is P4TypeClass.UNKNOWN:
        if sniff_head is not None:
            type_class = (
                P4TypeClass.BINARY if sniff_is_binary(sniff_head) else P4TypeClass.TEXT
            )
        else:
            # sniff 불가: 대형/미상은 binary 로 보수적 처리, 소형은 text 로.
            type_class = (
                P4TypeClass.TEXT
                if (size is not None and size < threshold)
                else P4TypeClass.BINARY
            )

    if type_class is not P4TypeClass.BINARY:
        return False

    if size is None:
        return True  # safe default: 미상 → 대형 가정
    return size >= threshold


def partition_for_lfs(
    actions: list[tuple["P4FileAction", str]],
    cfg: "LfsConfig",
    p4: "P4Client",
) -> tuple[list[tuple["P4FileAction", str]], list[tuple["P4FileAction", str]]]:
    """``(P4FileAction, git_path)`` 목록을 ``(lfs_files, normal_files)`` 로 분할한다.

    1. 확장자 매칭 → LFS (size 조회 불필요)
    2. text 타입 → normal (size 조회 불필요)
    3. ambiguous(binary/unknown, 확장자 미스) → batch ``p4 sizes`` 로 크기 확보 후
       ``decide_lfs_route`` 적용. UNKNOWN 타입은 head prefix 를 읽어 sniff 보정.

    ``auto_detect_binary`` 가 꺼져 있으면 확장자 매칭만으로 분할한다(기존 동작).
    파일 간 순서는 보존되지 않으나(ambiguous 가 뒤로 모임) fast-import 의 파일 항목은
    순서 무관하므로 문제되지 않는다.
    """
    lfs_files: list[tuple[P4FileAction, str]] = []
    normal_files: list[tuple[P4FileAction, str]] = []
    ambiguous: list[tuple[P4FileAction, str]] = []

    for fa, git_path in actions:
        if cfg.is_lfs_target_ext(git_path):
            lfs_files.append((fa, git_path))
        elif not cfg.auto_detect_binary:
            normal_files.append((fa, git_path))
        elif classify_p4_file_type(fa.file_type) is P4TypeClass.TEXT:
            normal_files.append((fa, git_path))
        else:
            ambiguous.append((fa, git_path))

    if ambiguous:
        p4.fill_sizes([fa for fa, _ in ambiguous])
        for fa, git_path in ambiguous:
            sniff_head: bytes | None = None
            # UNKNOWN 타입만 내용 샘플링으로 보정한다. read_head_prefix 는 앞부분만
            # 부분 전송하므로 대형 파일이어도 저렴하다. 읽기 실패 시 None →
            # decide_lfs_route 가 size 기반으로 보수적으로(LFS) 판단한다.
            if classify_p4_file_type(fa.file_type) is P4TypeClass.UNKNOWN:
                sniff_head = p4.read_head_prefix(
                    fa.depot_path, fa.revision, SNIFF_PREFIX_SIZE,
                )
            if decide_lfs_route(
                git_path=git_path,
                file_type=fa.file_type,
                size=fa.size,
                cfg=cfg,
                sniff_head=sniff_head,
            ):
                lfs_files.append((fa, git_path))
            else:
                normal_files.append((fa, git_path))

    return lfs_files, normal_files
