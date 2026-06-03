"""IntegrityChecker 테스트 — 실제 git repo + LFS store + 가짜 P4.

핵심 회귀: LFS 파일(git tree 에 pointer 텍스트만 존재)을 P4 원본과 올바르게
교차검증한다(과거에는 pointer 텍스트 ↔ 바이너리 비교로 항상 불일치였음).
"""

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.services.integrity_checker import IntegrityChecker

_STREAM = "//depot/main"


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(root: str, files: dict[str, bytes]) -> None:
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@e.com"], root)
    _git(["config", "user.name", "tester"], root)
    for path, content in files.items():
        full = Path(root) / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "init"], root)


def _depot(git_path: str) -> str:
    return f"{_STREAM}/{git_path}"


class _FakeP4:
    """무결성 검증에 필요한 최소 P4 인터페이스."""

    def __init__(self, contents=None, digests=None, sizes=None, fail_on=None):
        self._contents = contents or {}    # depot -> bytes (#head 콘텐츠)
        self._digests = digests or {}      # depot -> (md5_lower, size, head_type)
        self._sizes = sizes or []          # list[(depot, size)]
        self._fail_on = fail_on            # 호출 시 예외를 던질 메서드명(병렬 워커 테스트)

    def connect(self):
        pass

    def disconnect(self):
        pass

    def print_file_to_bytes_head(self, depot):
        if self._fail_on == "print_file_to_bytes_head":
            raise RuntimeError("injected failure")
        return self._contents.get(depot)

    def head_digests(self, depot_paths, batch_size=200):
        return {d: self._digests[d] for d in depot_paths if d in self._digests}

    def iter_file_sizes(self, path):
        return list(self._sizes)

    def print_head_to_disk(self, depot, dest_dir):
        content = self._contents.get(depot, b"")
        fd, p = tempfile.mkstemp(dir=dest_dir, suffix=".verify.tmp")
        os.write(fd, content)
        os.close(fd)
        return Path(p)


class _FakeP4Config:
    def __init__(self, p4):
        self._p4 = p4

    def create_client(self):
        return self._p4


@pytest.fixture
def repo_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _store_lfs_object(repo_dir: str, content: bytes):
    """LFS object 를 로컬 store 에 저장하고 (store, pointer) 반환."""
    git_dir = Path(repo_dir) / ".git"
    store = LfsObjectStore(git_dir=git_dir)
    pointer = store.store_from_stream([content])
    return store, pointer


class TestNormalFiles:
    def test_normal_match(self, repo_dir):
        _make_repo(repo_dir, {"src/code.txt": b"hello world"})
        p4 = _FakeP4(contents={_depot("src/code.txt"): b"hello world"})
        checker = IntegrityChecker(p4, repo_dir, _STREAM)
        res = checker.verify_full()
        assert res.passed is True
        assert res.checked_files == 1

    def test_normal_mismatch(self, repo_dir):
        _make_repo(repo_dir, {"src/code.txt": b"hello world"})
        p4 = _FakeP4(contents={_depot("src/code.txt"): b"DIFFERENT"})
        checker = IntegrityChecker(p4, repo_dir, _STREAM)
        res = checker.verify_full()
        assert res.passed is False
        assert res.mismatched_files == ["src/code.txt"]


class TestLfsFiles:
    def test_lfs_match_via_md5(self, repo_dir):
        content = b"\x00\x01\x02\x03" * 1000   # 4000 bytes 바이너리
        store, ptr = _store_lfs_object(repo_dir, content)
        # git tree 에는 pointer 텍스트만 커밋(실제 마이그레이션 산출물과 동일)
        _make_repo(repo_dir, {"assets/tex.bin": ptr.pointer_bytes})

        md5 = hashlib.md5(content).hexdigest()
        p4 = _FakeP4(digests={_depot("assets/tex.bin"): (md5, len(content), "binary")})
        checker = IntegrityChecker(p4, repo_dir, _STREAM, lfs_store=store)
        res = checker.verify_full()
        assert res.passed is True
        assert res.checked_files == 1

    def test_lfs_size_mismatch(self, repo_dir):
        content = b"\x00" * 500
        store, ptr = _store_lfs_object(repo_dir, content)
        _make_repo(repo_dir, {"a.bin": ptr.pointer_bytes})
        md5 = hashlib.md5(content).hexdigest()
        # P4 가 보고한 size 가 pointer 와 다름
        p4 = _FakeP4(digests={_depot("a.bin"): (md5, 999, "binary")})
        checker = IntegrityChecker(p4, repo_dir, _STREAM, lfs_store=store)
        res = checker.verify_full()
        assert res.passed is False
        assert res.mismatched_files == ["a.bin"]

    def test_lfs_md5_mismatch(self, repo_dir):
        content = b"\x00" * 500
        store, ptr = _store_lfs_object(repo_dir, content)
        _make_repo(repo_dir, {"a.bin": ptr.pointer_bytes})
        p4 = _FakeP4(digests={_depot("a.bin"): ("deadbeef" * 4, len(content), "binary")})
        checker = IntegrityChecker(p4, repo_dir, _STREAM, lfs_store=store)
        res = checker.verify_full()
        assert res.passed is False

    def test_lfs_object_missing_is_failure(self, repo_dir):
        content = b"\x00" * 500
        store, ptr = _store_lfs_object(repo_dir, content)
        _make_repo(repo_dir, {"a.bin": ptr.pointer_bytes})
        # 로컬 object 삭제 → 마이그레이션 산출물 깨짐
        store.object_path(ptr.oid).unlink()
        md5 = hashlib.md5(content).hexdigest()
        p4 = _FakeP4(digests={_depot("a.bin"): (md5, len(content), "binary")})
        checker = IntegrityChecker(p4, repo_dir, _STREAM, lfs_store=store)
        res = checker.verify_full()
        assert res.passed is False
        assert res.mismatched_files == ["a.bin"]

    def test_lfs_content_fallback_without_store(self, repo_dir):
        # store/digest 없음 → P4 콘텐츠 SHA256 ↔ pointer.oid fallback
        content = b"\xaa\xbb" * 2048
        _, ptr = _store_lfs_object(repo_dir, content)
        _make_repo(repo_dir, {"a.bin": ptr.pointer_bytes})
        p4 = _FakeP4(contents={_depot("a.bin"): content})   # digest 없음
        checker = IntegrityChecker(p4, repo_dir, _STREAM, lfs_store=None)
        res = checker.verify_full()
        assert res.passed is True
        assert res.checked_files == 1

    def test_lfs_text_type_uses_content_fallback_not_md5(self, repo_dir):
        # text 타입으로 체크인됐지만 확장자 규칙으로 LFS 라우팅된 파일.
        # P4 digest 는 줄바꿈 정규화된 MD5 라서 raw bytes MD5 와 다르다 →
        # MD5 fast-path 를 쓰면 오탐. binary 가 아니므로 콘텐츠 SHA256 fallback
        # 으로 가야 하고, 콘텐츠가 일치하면 통과해야 한다.
        content = b"line1\nline2\nline3\n"
        store, ptr = _store_lfs_object(repo_dir, content)
        _make_repo(repo_dir, {"data.csv": ptr.pointer_bytes})
        # 일부러 틀린 MD5 를 넣어, fast-path 가 동작했다면 불일치가 났을 상황을 만든다.
        wrong_md5 = "deadbeef" * 4
        p4 = _FakeP4(
            digests={_depot("data.csv"): (wrong_md5, len(content), "text")},
            contents={_depot("data.csv"): content},
        )
        checker = IntegrityChecker(p4, repo_dir, _STREAM, lfs_store=store)
        res = checker.verify_full()
        assert res.passed is True
        assert res.checked_files == 1


class TestCutoverStrategies:
    def _setup_mixed(self, repo_dir):
        """대형 LFS(불일치) + 다수 소형 코드(일치) repo 구성."""
        big = b"\x00" * (8 * 1024 * 1024)        # 8MB 대형
        store, ptr = _store_lfs_object(repo_dir, big)
        files = {"assets/huge.bin": ptr.pointer_bytes}
        for i in range(20):
            files[f"src/f{i}.txt"] = f"code{i}".encode()
        _make_repo(repo_dir, files)

        # 대형 파일은 P4 digest 가 일부러 불일치
        digests = {
            _depot("assets/huge.bin"): ("bad" * 10 + "0000", 8 * 1024 * 1024, "binary"),
        }
        contents = {_depot(f"src/f{i}.txt"): f"code{i}".encode() for i in range(20)}
        sizes = [(_depot("assets/huge.bin"), 8 * 1024 * 1024)]
        sizes += [(_depot(f"src/f{i}.txt"), 5) for i in range(20)]
        return store, _FakeP4(contents=contents, digests=digests, sizes=sizes)

    def test_smart_always_checks_large_file(self, repo_dir):
        # 코드 샘플 0개여도 대형 파일은 전수 → 불일치 탐지
        store, p4 = self._setup_mixed(repo_dir)
        checker = IntegrityChecker(p4, repo_dir, _STREAM, lfs_store=store)
        res = checker.verify_cutover(
            mode="smart", sample_count=0, large_threshold_bytes=1024 * 1024,
        )
        assert res.strategy == "smart"
        assert res.passed is False
        assert "assets/huge.bin" in res.mismatched_files

    def test_sample_mode_limits_scope(self, repo_dir):
        store, p4 = self._setup_mixed(repo_dir)
        checker = IntegrityChecker(p4, repo_dir, _STREAM, lfs_store=store)
        res = checker.verify_cutover(mode="sample", sample_count=3)
        assert res.strategy == "sample"
        assert res.checked_files <= 3
        assert res.total_files == 21


class TestParallelEqualsSerial:
    def test_parallel_matches_serial_result(self, repo_dir):
        files = {f"src/f{i}.txt": f"v{i}".encode() for i in range(10)}
        _make_repo(repo_dir, files)
        contents = {_depot(f"src/f{i}.txt"): f"v{i}".encode() for i in range(10)}
        # 하나는 일부러 불일치
        contents[_depot("src/f3.txt")] = b"WRONG"

        serial = IntegrityChecker(_FakeP4(contents=contents), repo_dir, _STREAM)
        r_serial = serial.verify_full()

        p4 = _FakeP4(contents=contents)
        parallel = IntegrityChecker(
            p4, repo_dir, _STREAM, p4_config=_FakeP4Config(p4), max_workers=4,
        )
        r_parallel = parallel.verify_full()

        assert r_serial.checked_files == r_parallel.checked_files == 10
        assert set(r_serial.mismatched_files) == set(r_parallel.mismatched_files) == {"src/f3.txt"}

    def test_parallel_worker_exception_raises_runtimeerror(self, repo_dir):
        # fail-closed: 워커가 예외를 던지면 일부 슬라이스가 검증되지 않으므로
        # 조용히 passed=True 가 되어선 안 되고 RuntimeError 로 실패해야 한다.
        files = {f"src/f{i}.txt": f"v{i}".encode() for i in range(10)}
        _make_repo(repo_dir, files)
        contents = {_depot(f"src/f{i}.txt"): f"v{i}".encode() for i in range(10)}
        p4 = _FakeP4(contents=contents, fail_on="print_file_to_bytes_head")
        checker = IntegrityChecker(
            p4, repo_dir, _STREAM, p4_config=_FakeP4Config(p4), max_workers=4,
        )
        with pytest.raises(RuntimeError):
            checker.verify_full()


class TestVerifyCutoverModeValidation:
    def test_invalid_mode_raises_valueerror(self, repo_dir):
        _make_repo(repo_dir, {"src/code.txt": b"hello"})
        p4 = _FakeP4(contents={_depot("src/code.txt"): b"hello"})
        checker = IntegrityChecker(p4, repo_dir, _STREAM)
        with pytest.raises(ValueError):
            checker.verify_cutover(mode="smrt")
        with pytest.raises(ValueError):
            checker.verify_cutover(mode="FULL")

    def test_valid_modes_do_not_raise(self, repo_dir):
        _make_repo(repo_dir, {"src/code.txt": b"hello"})
        p4 = _FakeP4(contents={_depot("src/code.txt"): b"hello"})
        checker = IntegrityChecker(p4, repo_dir, _STREAM)
        for mode in ("full", "sample", "smart"):
            res = checker.verify_cutover(mode=mode)
            assert res.strategy == mode
