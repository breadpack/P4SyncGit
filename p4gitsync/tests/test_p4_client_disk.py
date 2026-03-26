from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from p4gitsync.p4.p4_client import P4Client


class TestPrintFileToDisk:
    @pytest.fixture
    def client(self):
        with patch("p4gitsync.p4.p4_client.P4") as mock_p4_class:
            mock_p4 = MagicMock()
            mock_p4_class.return_value = mock_p4
            c = P4Client(port="ssl:p4:1666", user="test", workspace="test-ws")
            c._p4 = mock_p4
            c._connected = True
            yield c

    def test_creates_file_on_disk(self, client: P4Client, tmp_path: Path):
        depot = "//depot/main/art/texture.png"

        def fake_subprocess_run(*args, **kwargs):
            cmd = args[0]
            o_idx = cmd.index("-o")
            dest = Path(cmd[o_idx + 1])
            dest.write_bytes(b"fake png data")
            return MagicMock(returncode=0, stderr=b"")

        with patch("subprocess.run", side_effect=fake_subprocess_run):
            result = client.print_file_to_disk(depot, 5, tmp_path)
            assert isinstance(result, Path)
            assert result.name == "texture.png"
            assert result.read_bytes() == b"fake png data"

    def test_raises_on_failure(self, client: P4Client, tmp_path: Path):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=1, stderr=b"file not found")
            with pytest.raises(RuntimeError):
                client.print_file_to_disk("//depot/missing", 1, tmp_path)
