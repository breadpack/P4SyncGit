"""P4 파일 타입 → Git 파일 모드 매핑 및 file_change 항목 정규화.

P4GitSync는 파일 변경 항목을 ``(path, content)`` 또는 ``(path, content, mode)``
튜플로 다룬다. mode가 생략되면 일반 파일(100644)로 간주하여 기존 호출부(역방향
동기, conflict 재현 등)와 하위호환을 유지한다.
"""

from __future__ import annotations

# Git object 모드 (8진수). fast-import / mktree / pygit2 공통.
GIT_MODE_FILE = 0o100644      # 일반 파일
GIT_MODE_EXEC = 0o100755      # 실행 비트 (+x)
GIT_MODE_SYMLINK = 0o120000   # 심볼릭 링크

FileChange = tuple[str, bytes] | tuple[str, bytes, int]


def git_mode_from_p4_type(file_type: str) -> int:
    """P4 파일 타입 문자열을 Git 파일 모드로 변환.

    P4 타입 예: ``text``, ``binary``, ``symlink``, ``xtext``, ``xbinary``,
    ``unicode``, ``binary+x``, ``text+lx`` 등. base 타입과 modifier(``+...``)를
    모두 검사한다.

    - ``symlink`` 포함 → 120000 (심링크)
    - base가 ``x``로 시작(``xtext``/``xbinary``) 또는 modifier에 ``x`` → 100755 (실행)
    - 그 외 → 100644
    """
    if not file_type:
        return GIT_MODE_FILE
    ft = file_type.lower()
    if "symlink" in ft:
        return GIT_MODE_SYMLINK
    base, _, mods = ft.partition("+")
    if base.startswith("x") or "x" in mods:
        return GIT_MODE_EXEC
    return GIT_MODE_FILE


def unpack_change(item: FileChange) -> tuple[str, bytes, int]:
    """file_change 항목을 (path, content, mode)로 정규화.

    mode가 없는 2-튜플이면 일반 파일(100644)로 채운다.
    """
    if len(item) == 3:
        path, content, mode = item  # type: ignore[misc]
        return path, content, mode
    path, content = item  # type: ignore[misc]
    return path, content, GIT_MODE_FILE
