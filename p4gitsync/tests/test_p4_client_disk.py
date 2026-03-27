from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from P4 import P4Exception
from p4gitsync.p4.p4_client import P4Client


class TestPrintFileToDisk:
    @pytest.fixture
    def client(self):
        with patch("p4gitsync.p4.p4_client.P4") as mock_p4_class:
            mock_p4 = MagicMock()
            mock_p4.connected.return_value = True
            mock_p4_class.return_value = mock_p4
            c = P4Client(port="ssl:p4:1666", user="test", workspace="test-ws")
            c._p4 = mock_p4
            yield c

    def test_creates_file_on_disk(self, client: P4Client, tmp_path: Path):
        depot = "//depot/main/art/texture.png"

        def fake_run_print(*args):
            # run_print -o <dest> <depot>#<rev> → 파일 생성 시뮬레이션
            for i, arg in enumerate(args):
                if arg == "-o" and i + 1 < len(args):
                    dest = Path(args[i + 1])
                    dest.write_bytes(b"fake png data")
            return [{"depotFile": depot}]

        client._p4.run_print.side_effect = fake_run_print

        result = client.print_file_to_disk(depot, 5, tmp_path)
        assert isinstance(result, Path)
        assert result.name == "texture.png"
        assert result.read_bytes() == b"fake png data"

    def test_raises_on_p4_exception(self, client: P4Client, tmp_path: Path):
        client._p4.run_print.side_effect = P4Exception("file not found")
        with pytest.raises(RuntimeError, match="p4 print -o 실패"):
            client.print_file_to_disk("//depot/missing", 1, tmp_path)

    def test_raises_when_file_not_created(self, client: P4Client, tmp_path: Path):
        # run_print 성공하지만 파일이 생성되지 않은 경우
        client._p4.run_print.return_value = [{"depotFile": "//depot/x"}]
        with pytest.raises(RuntimeError, match="파일 미생성"):
            client.print_file_to_disk("//depot/x", 1, tmp_path)
