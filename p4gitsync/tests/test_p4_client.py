"""P4Client 단위 테스트.

P4 서버 연결이 필요한 테스트는 mock을 사용한다.
실제 P4 서버 테스트는 integration/ 디렉토리에서 수행한다.
"""

from unittest.mock import patch

import pytest

from p4gitsync.p4.p4_client import P4Client
from p4gitsync.p4.p4_change_info import P4ChangeInfo
from p4gitsync.p4.p4_file_action import P4FileAction


class TestP4ClientWithMock:
    @pytest.fixture
    def mock_p4(self):
        with patch("p4gitsync.p4.p4_client.P4") as MockP4:
            mock_instance = MockP4.return_value
            mock_instance.connected.return_value = True
            client = P4Client(
                port="ssl:test:1666",
                user="testuser",
                workspace="test-ws",
            )
            yield client, mock_instance

    def test_connect(self, mock_p4):
        client, mock = mock_p4
        client.connect()
        mock.connect.assert_called_once()

    def test_disconnect(self, mock_p4):
        client, mock = mock_p4
        client.disconnect()
        mock.disconnect.assert_called_once()

    def test_get_changes_after(self, mock_p4):
        client, mock = mock_p4
        mock.run_changes.return_value = [
            {"change": "103"},
            {"change": "102"},
            {"change": "101"},
        ]
        result = client.get_changes_after("//test/main", 100)
        assert result == [101, 102, 103]
        mock.run_changes.assert_called_once_with(
            "-s", "submitted", "-e", "101", "//test/main/...",
        )

    def test_get_changes_after_empty(self, mock_p4):
        client, mock = mock_p4
        mock.run_changes.return_value = []
        result = client.get_changes_after("//test/main", 100)
        assert result == []

    def test_describe(self, mock_p4):
        client, mock = mock_p4
        mock.run_describe.return_value = [{
            "change": "100",
            "user": "john",
            "desc": "Add feature\n",
            "time": "1700000000",
            "depotFile": [
                "//test/main/src/foo.py",
                "//test/main/src/bar.py",
            ],
            "action": ["add", "edit"],
            "type": ["text", "text"],
            "rev": ["1", "3"],
        }]
        info = client.describe(100)
        assert isinstance(info, P4ChangeInfo)
        assert info.changelist == 100
        assert info.user == "john"
        assert info.description == "Add feature\n"
        assert len(info.files) == 2
        assert info.files[0].depot_path == "//test/main/src/foo.py"
        assert info.files[0].action == "add"
        assert info.files[0].revision == 1
        assert info.files[1].action == "edit"

    def test_describe_no_files(self, mock_p4):
        client, mock = mock_p4
        mock.run_describe.return_value = [{
            "change": "100",
            "user": "john",
            "desc": "Empty CL",
            "time": "1700000000",
        }]
        info = client.describe(100)
        assert len(info.files) == 0

    def test_print_file(self, mock_p4):
        client, mock = mock_p4
        client.print_file("//test/main/foo.py", 1, "/tmp/foo.py")
        mock.run_print.assert_called_once_with("-o", "/tmp/foo.py", "//test/main/foo.py#1")

    def test_print_file_safe_success(self, mock_p4):
        client, mock = mock_p4
        assert client.print_file_safe("//test/main/foo.py", 1, "/tmp/foo.py") is True

    def test_print_file_safe_failure(self, mock_p4):
        client, mock = mock_p4
        from P4 import P4Exception
        mock.run_print.side_effect = P4Exception("file not found")
        assert client.print_file_safe("//test/main/foo.py", 1, "/tmp/foo.py") is False

    def test_print_file_to_bytes(self, mock_p4):
        client, mock = mock_p4
        mock.run_print.return_value = [
            {"depotFile": "//test/main/foo.py"},
            b"file content here",
        ]
        content = client.print_file_to_bytes("//test/main/foo.py", 1)
        assert content == b"file content here"

    def test_print_file_to_bytes_string(self, mock_p4):
        client, mock = mock_p4
        mock.run_print.return_value = [
            {"depotFile": "//test/main/foo.py"},
            "text content",
        ]
        content = client.print_file_to_bytes("//test/main/foo.py", 1)
        assert content == b"text content"

    def test_sync(self, mock_p4):
        client, mock = mock_p4
        client.sync(100)
        mock.run_sync.assert_called_once_with("//...@100")

    def test_get_users(self, mock_p4):
        client, mock = mock_p4
        mock.run_users.return_value = [
            {"User": "john", "FullName": "John Doe", "Email": "john@example.com"},
        ]
        users = client.get_users()
        assert len(users) == 1
        assert users[0]["User"] == "john"

    def test_build_initial_user_mapping(self, mock_p4):
        client, mock = mock_p4
        mock.run_users.return_value = [
            {"User": "john", "FullName": "John Doe", "Email": "john@example.com"},
            {"User": "jane"},
        ]
        mappings = client.build_initial_user_mapping("company.com")
        assert len(mappings) == 2
        assert mappings[0] == ("john", "John Doe", "john@example.com")
        assert mappings[1] == ("jane", "jane", "jane@company.com")

    def test_run_filelog_batching(self, mock_p4):
        client, mock = mock_p4
        paths = [f"//test/main/file{i}.py" for i in range(5)]
        mock.run_filelog.return_value = []
        client.run_filelog(paths, batch_size=2)
        assert mock.run_filelog.call_count == 3

    def test_check_server_load_normal(self, mock_p4):
        client, mock = mock_p4
        mock.run_monitor.return_value = [
            {"status": "R"} for _ in range(10)
        ]
        assert client.check_server_load(50) is False

    def test_check_server_load_high(self, mock_p4):
        client, mock = mock_p4
        mock.run_monitor.return_value = [
            {"status": "R"} for _ in range(60)
        ]
        assert client.check_server_load(50) is True

    def test_fill_sizes(self, mock_p4):
        client, mock = mock_p4
        mock.run_sizes.return_value = [
            {"depotFile": "//d/a", "fileSize": "100"},
            {"depotFile": "//d/b", "fileSize": "5000"},
        ]
        a = P4FileAction("//d/a", "add", "binary", 1)
        b = P4FileAction("//d/b", "add", "binary", 2)
        client.fill_sizes([a, b])
        assert a.size == 100
        assert b.size == 5000

    def test_fill_sizes_skips_already_filled(self, mock_p4):
        client, mock = mock_p4
        a = P4FileAction("//d/a", "add", "binary", 1, size=42)
        client.fill_sizes([a])
        mock.run_sizes.assert_not_called()
        assert a.size == 42

    def test_fill_sizes_batches(self, mock_p4):
        client, mock = mock_p4
        mock.run_sizes.return_value = []
        actions = [P4FileAction(f"//d/f{i}", "add", "binary", 1) for i in range(5)]
        client.fill_sizes(actions, batch_size=2)
        assert mock.run_sizes.call_count == 3

    def test_head_digests_returns_md5_size_type(self, mock_p4):
        client, mock = mock_p4
        mock.run_fstat.return_value = [
            {
                "depotFile": "//test/main/a.bin",
                "digest": "ABCDEF0123456789ABCDEF0123456789",
                "fileSize": "1234",
                "headType": "binary+l",
                "headAction": "edit",
            },
            {
                "depotFile": "//test/main/b.txt",
                "digest": "0011223344556677",
                "fileSize": "10",
                "headType": "text",
                "headAction": "add",
            },
        ]
        out = client.head_digests(["//test/main/a.bin", "//test/main/b.txt"])
        assert out["//test/main/a.bin"] == (
            "abcdef0123456789abcdef0123456789", 1234, "binary+l",
        )
        assert out["//test/main/b.txt"] == ("0011223344556677", 10, "text")
        # headType 이 -T 필드에 포함됐는지 확인
        args = mock.run_fstat.call_args[0]
        assert any("headType" in a for a in args if isinstance(a, str))

    def test_head_digests_skips_delete_and_missing(self, mock_p4):
        client, mock = mock_p4
        mock.run_fstat.return_value = [
            {"depotFile": "//d/del", "headType": "binary",
             "headAction": "delete", "digest": "x", "fileSize": "1"},
            {"depotFile": "//d/nodig", "headType": "binary",
             "headAction": "edit", "fileSize": "1"},
            {"depotFile": "//d/ok", "headType": "binary",
             "headAction": "edit", "digest": "AB", "fileSize": "5"},
        ]
        out = client.head_digests(["//d/del", "//d/nodig", "//d/ok"])
        assert out == {"//d/ok": ("ab", 5, "binary")}

    def test_read_head_prefix_none_when_no_data(self, mock_p4):
        client, mock = mock_p4
        # mock run_print 는 handler 콜백을 호출하지 않음 → received False → None
        assert client.read_head_prefix("//d/a", 1) is None


class TestHeadPrefixHandler:
    def test_accumulate_and_cancel(self):
        from P4 import OutputHandler

        from p4gitsync.p4.p4_client import _HeadPrefixHandler

        h = _HeadPrefixHandler(4)
        assert h.outputBinary(b"ab") == OutputHandler.HANDLED
        assert h.received is True
        assert h.outputBinary(b"cdef") == OutputHandler.CANCEL
        assert bytes(h.buf[:4]) == b"abcd"

    def test_text_encoded(self):
        from p4gitsync.p4.p4_client import _HeadPrefixHandler

        h = _HeadPrefixHandler(100)
        h.outputText("hello")
        assert bytes(h.buf) == b"hello"
