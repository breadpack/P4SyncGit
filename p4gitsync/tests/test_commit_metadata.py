from p4gitsync.git.commit_metadata import CommitMetadata


class TestCommitMetadata:
    def test_format_message(self):
        meta = CommitMetadata(
            author_name="Test",
            author_email="test@example.com",
            author_timestamp=1700000000,
            message="Add feature",
            p4_changelist=123,
        )
        assert meta.format_message() == "Add feature\n\n[P4CL: 123]"

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
        assert result.endswith("[P4CL: 456]")
