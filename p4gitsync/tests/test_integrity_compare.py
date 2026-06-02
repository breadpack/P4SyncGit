"""integrity_compare 순수 판정 로직 테스트."""

from p4gitsync.services import integrity_compare as ic


class TestDecideNormal:
    def test_match(self):
        status, _ = ic.decide_normal("abc", "abc")
        assert status == ic.MATCH

    def test_mismatch(self):
        status, reason = ic.decide_normal("a" * 64, "b" * 64)
        assert status == ic.MISMATCH
        assert "불일치" in reason


class TestDecideLfs:
    def test_object_missing_is_mismatch(self):
        status, reason = ic.decide_lfs(
            ptr_oid="o" * 64, ptr_size=10, object_exists=False,
            p4_size=10, p4_md5="m", local_md5="m",
        )
        assert status == ic.MISMATCH
        assert "누락" in reason

    def test_size_mismatch(self):
        status, reason = ic.decide_lfs(
            ptr_oid="o" * 64, ptr_size=10, object_exists=True,
            p4_size=20, p4_md5="m", local_md5="m",
        )
        assert status == ic.MISMATCH
        assert "크기" in reason

    def test_md5_match(self):
        status, _ = ic.decide_lfs(
            ptr_oid="o" * 64, ptr_size=10, object_exists=True,
            p4_size=10, p4_md5="ABCDEF", local_md5="abcdef",   # 대소문자 무시
        )
        assert status == ic.MATCH

    def test_md5_mismatch(self):
        status, reason = ic.decide_lfs(
            ptr_oid="o" * 64, ptr_size=10, object_exists=True,
            p4_size=10, p4_md5="aaa", local_md5="bbb",
        )
        assert status == ic.MISMATCH
        assert "MD5" in reason

    def test_need_content_when_meta_absent(self):
        # 로컬 store 없음(object_exists=None) + digest 없음 → 콘텐츠 fallback 신호
        status, _ = ic.decide_lfs(
            ptr_oid="o" * 64, ptr_size=10, object_exists=None,
            p4_size=None, p4_md5=None, local_md5=None,
        )
        assert status == ic.NEED_CONTENT


class TestDecideLfsContent:
    def test_match(self):
        status, _ = ic.decide_lfs_content("deadbeef", "deadbeef")
        assert status == ic.MATCH

    def test_mismatch(self):
        status, reason = ic.decide_lfs_content("a" * 64, "b" * 64)
        assert status == ic.MISMATCH
        assert "oid" in reason
