"""무결성 비교의 순수 판정 로직 (P4/git 의존 없이 단위 테스트).

IntegrityChecker 가 파일별로 수집한 해시/메타로 일치 여부를 판정한다.
LFS 파일은 콘텐츠를 다시 받지 않고 P4 fstat 의 MD5 digest 와 로컬 LFS object
의 MD5 + 크기로 교차검증한다(TB급에서 네트워크 전송 0). 메타가 없을 때만
콘텐츠 해시 fallback(NEED_CONTENT)을 caller 에 신호한다.
"""

from __future__ import annotations

# 판정 결과
MATCH = "match"
MISMATCH = "mismatch"
NEED_CONTENT = "need_content"   # 메타로 판정 불가 → caller 가 콘텐츠 해시로 재판정


def decide_normal(git_sha256: str, p4_sha256: str) -> tuple[str, str]:
    """일반(비-LFS) 파일: git blob 과 P4 콘텐츠의 SHA256 비교."""
    if git_sha256 == p4_sha256:
        return (MATCH, "")
    return (
        MISMATCH,
        f"내용 해시 불일치 (git={git_sha256[:12]}, p4={p4_sha256[:12]})",
    )


def decide_lfs(
    *,
    ptr_oid: str,
    ptr_size: int,
    object_exists: bool | None,
    p4_size: int | None,
    p4_md5: str | None,
    local_md5: str | None,
) -> tuple[str, str]:
    """LFS 파일: 로컬 object 존재 + 크기 + MD5(로컬 object ↔ P4 digest) 교차검증.

    - object_exists 가 False 면 마이그레이션 산출물 자체가 깨진 것(즉시 불일치).
    - 크기/ MD5 메타가 둘 다 갖춰지면 그것으로 판정.
    - 메타가 부족하면 NEED_CONTENT 를 반환해 caller 가 P4 콘텐츠 SHA256 ↔
      pointer.oid 로 재판정하게 한다.
    """
    if object_exists is False:
        return (MISMATCH, f"LFS object 누락: {ptr_oid[:12]}")
    if p4_size is not None and ptr_size != p4_size:
        return (
            MISMATCH,
            f"크기 불일치 (pointer={ptr_size}, p4={p4_size})",
        )
    if p4_md5 and local_md5:
        if local_md5.lower() == p4_md5.lower():
            return (MATCH, "")
        return (
            MISMATCH,
            f"MD5 불일치 (lfs={local_md5[:12]}, p4={p4_md5[:12]})",
        )
    return (NEED_CONTENT, "")


def decide_lfs_content(ptr_oid: str, p4_content_sha256: str) -> tuple[str, str]:
    """LFS fallback: P4 콘텐츠 SHA256 이 pointer.oid(=콘텐츠 SHA256) 와 같은지."""
    if ptr_oid == p4_content_sha256:
        return (MATCH, "")
    return (
        MISMATCH,
        f"LFS oid 불일치 (pointer={ptr_oid[:12]}, p4={p4_content_sha256[:12]})",
    )
