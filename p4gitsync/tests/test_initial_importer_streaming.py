"""대형 CL 메모리 스트리밍(C1) 단위 테스트."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import subprocess

from p4gitsync.config.sync_config import InitialImportConfig
from p4gitsync.errors import ContentExtractionError
from p4gitsync.git.fast_importer import FastImporter
from p4gitsync.git.file_mode import GIT_MODE_FILE
from p4gitsync.p4.p4_change_info import P4ChangeInfo
from p4gitsync.p4.p4_file_action import P4FileAction
from p4gitsync.services.initial_importer import InitialImporter, _CLData


def _importer(tmp_path, **cfg_kw) -> InitialImporter:
    cfg = InitialImportConfig(**cfg_kw)
    return InitialImporter(
        p4_client=MagicMock(),
        state_store=MagicMock(),
        repo_path=str(tmp_path),
        stream="//depot/main",
        config=cfg,
    )


def _fa(path: str, ft: str = "text", size: int | None = None) -> P4FileAction:
    return P4FileAction(
        depot_path=path, action="add", file_type=ft, revision=1, size=size,
    )


class TestIsStreamingCl:
    def test_under_threshold(self, tmp_path):
        imp = _importer(
            tmp_path, streaming_file_threshold=2000, streaming_bytes_threshold=10**9,
        )
        nf = [(_fa(f"//d/f{i}"), f"f{i}") for i in range(10)]
        assert imp._is_streaming_cl(nf) is False

    def test_file_count_over(self, tmp_path):
        imp = _importer(tmp_path, streaming_file_threshold=5)
        nf = [(_fa(f"//d/f{i}"), f"f{i}") for i in range(5)]
        assert imp._is_streaming_cl(nf) is True

    def test_known_bytes_over(self, tmp_path):
        imp = _importer(
            tmp_path, streaming_file_threshold=10**9, streaming_bytes_threshold=1000,
        )
        nf = [(_fa("//d/big", size=2000), "big")]
        assert imp._is_streaming_cl(nf) is True

    def test_unknown_size_not_counted(self, tmp_path):
        # size 미상은 누적에 안 잡힘(파일 수 임계가 안전망)
        imp = _importer(
            tmp_path, streaming_file_threshold=10**9, streaming_bytes_threshold=1000,
        )
        nf = [(_fa("//d/big", size=None), "big")]
        assert imp._is_streaming_cl(nf) is False


class TestStreamNormalToImporter:
    def test_writes_content_and_mode(self, tmp_path):
        imp = _importer(tmp_path)
        p4 = MagicMock()
        p4.print_files_batch.return_value = {"//d/a": b"AAA", "//d/b": b"BBB"}
        imp._main_extract_client = p4
        fi = MagicMock()
        data = _CLData(cl=5, info=MagicMock())
        data.streaming = True
        data.normal_files = [(_fa("//d/a"), "a"), (_fa("//d/b"), "b")]

        imp._stream_normal_to_importer(data, fi)

        calls = fi.write_file.call_args_list
        assert len(calls) == 2
        assert calls[0].args == ("a", b"AAA", GIT_MODE_FILE)
        assert calls[1].args == ("b", b"BBB", GIT_MODE_FILE)

    def test_chunks_by_batch_size(self, tmp_path, monkeypatch):
        import p4gitsync.services.initial_importer as mod

        monkeypatch.setattr(mod, "_BATCH_PRINT_SIZE", 2)
        imp = _importer(tmp_path)
        p4 = MagicMock()
        p4.print_files_batch.return_value = {}  # content 없음은 missing 처리
        imp._main_extract_client = p4
        fi = MagicMock()
        data = _CLData(cl=5, info=MagicMock())
        data.normal_files = [(_fa(f"//d/f{i}"), f"f{i}") for i in range(5)]

        with pytest.raises(ContentExtractionError):
            imp._stream_normal_to_importer(data, fi)
        # 5개 / batch 2 → 3회 print 호출(chunk 분할)
        assert p4.print_files_batch.call_count == 3

    def test_missing_raises(self, tmp_path):
        imp = _importer(tmp_path)
        p4 = MagicMock()
        p4.print_files_batch.return_value = {"//d/a": None}
        imp._main_extract_client = p4
        fi = MagicMock()
        data = _CLData(cl=7, info=MagicMock())
        data.normal_files = [(_fa("//d/a"), "a")]

        with pytest.raises(ContentExtractionError):
            imp._stream_normal_to_importer(data, fi)


class TestStreamLfsToImporter:
    def test_no_lfs_files_noop(self, tmp_path):
        imp = _importer(tmp_path)
        fi = MagicMock()
        data = _CLData(cl=1, info=MagicMock())
        imp._stream_lfs_to_importer(data, fi)
        fi.write_file.assert_not_called()

    def test_writes_pointer(self, tmp_path):
        imp = _importer(tmp_path)
        p4 = MagicMock()
        p4.print_file_to_disk.return_value = "/tmp/x"
        imp._main_extract_client = p4
        store = MagicMock()
        store.store_from_file.return_value = MagicMock(pointer_bytes=b"PTR")
        imp._lfs_store = store
        fi = MagicMock()
        data = _CLData(cl=2, info=MagicMock())
        data.lfs_files = [(_fa("//d/tex.png", "binary"), "tex.png")]

        imp._stream_lfs_to_importer(data, fi)

        fi.write_file.assert_called_once_with("tex.png", b"PTR")


class TestStreamingEqualsNonStreaming:
    """스트리밍/일반 경로가 동일한 git tree 를 만드는지(C1 정합성) 검증."""

    def test_same_tree(self, tmp_path):
        subprocess.run(
            ["git", "init", str(tmp_path)], capture_output=True, check=True,
        )
        imp = _importer(tmp_path)
        imp._state.get_git_author.return_value = ("Test User", "test@example.com")
        info = P4ChangeInfo(
            changelist=1, user="u", description="msg",
            timestamp=1700000000, files=[], workspace="",
        )

        # 일반 경로: normal_results 에 content 누적
        fi1 = FastImporter(str(tmp_path))
        fi1.start()
        d1 = _CLData(cl=1, info=info)
        d1.normal_results = [
            ("a.txt", b"AAA", GIT_MODE_FILE),
            ("dir/b.txt", b"BBB", GIT_MODE_FILE),
        ]
        imp._write_cl_to_importer(d1, 0, "nonstream", fi1)
        fi1.finish()

        # 스트리밍 경로: 목록만, 메인 연결로 추출하며 write
        p4 = MagicMock()
        p4.print_files_batch.return_value = {"//d/a.txt": b"AAA", "//d/b.txt": b"BBB"}
        imp._main_extract_client = p4
        fi2 = FastImporter(str(tmp_path))
        fi2.start()
        d2 = _CLData(cl=2, info=info)
        d2.streaming = True
        d2.normal_files = [(_fa("//d/a.txt"), "a.txt"), (_fa("//d/b.txt"), "dir/b.txt")]
        imp._write_cl_to_importer(d2, 0, "stream", fi2)
        fi2.finish()

        def tree(branch: str) -> str:
            r = subprocess.run(
                ["git", "rev-parse", f"refs/heads/{branch}^{{tree}}"],
                cwd=str(tmp_path), capture_output=True, text=True,
            )
            return r.stdout.strip()

        assert tree("nonstream") == tree("stream") != ""
