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
            for i, arg in enumerate(args):
                if arg == "-o" and i + 1 < len(args):
                    dest = Path(args[i + 1])
                    dest.write_bytes(b"fake png data")
            return [{"depotFile": depot}]

        client._p4.run_print.side_effect = fake_run_print

        result = client.print_file_to_disk(depot, 5, tmp_path)
        assert isinstance(result, Path)
        assert result.suffix == ".png"
        assert result.read_bytes() == b"fake png data"

    def test_raises_on_p4_exception(self, client: P4Client, tmp_path: Path):
        client._p4.run_print.side_effect = P4Exception("file not found")
        with pytest.raises(RuntimeError, match="p4 print -o 실패"):
            client.print_file_to_disk("//depot/missing.bin", 1, tmp_path)

    def test_raises_when_file_not_created(self, client: P4Client, tmp_path: Path):
        def fake_run_print(*args):
            # run_print 후 파일 삭제하여 미생성 상태 시뮬레이션
            for i, arg in enumerate(args):
                if arg == "-o" and i + 1 < len(args):
                    Path(args[i + 1]).unlink(missing_ok=True)
            return [{"depotFile": "//depot/x.bin"}]

        client._p4.run_print.side_effect = fake_run_print
        with pytest.raises(RuntimeError, match="파일 미생성"):
            client.print_file_to_disk("//depot/x.bin", 1, tmp_path)
