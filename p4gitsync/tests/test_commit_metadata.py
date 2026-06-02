from p4gitsync.git.commit_metadata import (
    CommitMetadata,
    parse_p4cl_from_message,
)


class TestCommitMetadata:
    def test_format_message(self):
        meta = CommitMetadata(
            author_name="Test",
            author_email="test@example.com",
            author_timestamp=1700000000,
            message="Add feature",
            p4_changelist=123,
        )
        assert meta.format_message() == "Add feature\n\nP4CL: 123"

    def test_format_message_multiline(self):
        meta = CommitMetadata(
            author_name="Test",
            author_email="test@example.com",
            author_timestamp=1700000000,
            message="Fix bug\n\nDetailed description here",
            p4_changelist=456,
        )
        result = meta.format_message()
        assert result.startswith("Fix bug\n\nDetailed description here")
        assert result.endswith("P4CL: 456")


class TestParseP4cl:
    def test_bracketless_current_format(self):
        # 현재 생성 포맷(git trailer 표준)
        assert parse_p4cl_from_message("msg\n\nP4CL: 123") == 123

    def test_bracketed_legacy_format(self):
        # 구 포맷 하위호환
        assert parse_p4cl_from_message("msg\n\n[P4CL: 456]") == 456

    def test_no_trailer(self):
        assert parse_p4cl_from_message("just a message") is None

    def test_roundtrip(self):
        # format_message 가 만든 trailer 를 parse 가 그대로 복원(생성↔파싱 일관성).
        meta = CommitMetadata(
            author_name="A", author_email="a@b.c",
            author_timestamp=1700000000, message="hello", p4_changelist=789,
        )
        assert parse_p4cl_from_message(meta.format_message()) == 789
