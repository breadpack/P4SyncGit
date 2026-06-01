"""lfs_pusher 테스트 — OID 파싱·chunk·진행상태·배치/재개 (push_fn 주입)."""

import pytest

from p4gitsync.services.lfs_pusher import (
    LfsPushProgress,
    LfsPusher,
    chunk,
    parse_ls_files_oids,
)

_OID_A = "a" * 64
_OID_B = "b" * 64
_OID_C = "c" * 64


class TestParseLsFiles:
    def test_extracts_and_dedupes(self):
        out = (
            f"{_OID_A} * path/one.png\n"
            f"{_OID_B} - path/two.fbx\n"
            f"{_OID_A} * path/dup.png\n"   # 동일 oid 중복
        )
        assert parse_ls_files_oids(out) == [_OID_A, _OID_B]

    def test_ignores_non_oid_and_blank(self):
        out = f"\n# comment\n{_OID_C} * x\nshort * y\n"
        assert parse_ls_files_oids(out) == [_OID_C]


class TestChunk:
    def test_splits(self):
        assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]

    def test_invalid_size(self):
        with pytest.raises(ValueError):
            chunk([1], 0)


class TestProgress:
    def test_roundtrip(self, tmp_path):
        p = LfsPushProgress(tmp_path / "prog.json")
        p.load()
        p.mark([_OID_A, _OID_B])
        p.save()

        p2 = LfsPushProgress(tmp_path / "prog.json")
        p2.load()
        assert p2.is_done(_OID_A)
        assert p2.is_done(_OID_B)
        assert not p2.is_done(_OID_C)
        assert p2.count == 2

    def test_reset(self, tmp_path):
        p = LfsPushProgress(tmp_path / "prog.json")
        p.mark([_OID_A])
        p.save()
        p.reset()
        assert p.count == 0
        assert not (tmp_path / "prog.json").exists()


class _RecordingPush:
    def __init__(self, fail_on_batch=None):
        self.batches = []
        self._fail_on = fail_on_batch  # 1-based 배치 번호

    def __call__(self, oids):
        self.batches.append(list(oids))
        if self._fail_on and len(self.batches) == self._fail_on:
            return False
        return True


def _pusher(tmp_path, oids, push_fn):
    progress = LfsPushProgress(tmp_path / "prog.json")
    return LfsPusher(
        repo_path=str(tmp_path),
        progress=progress,
        push_fn=push_fn,
        ls_files_fn=lambda: "\n".join(f"{o} * f" for o in oids),
    )


class TestRun:
    def test_all_success(self, tmp_path):
        oids = [_OID_A, _OID_B, _OID_C]
        push = _RecordingPush()
        s = _pusher(tmp_path, oids, push).run(batch_size=2)
        assert s.total == 3
        assert s.pushed == 3
        assert s.batches == 2          # [A,B] [C]
        assert s.ok is True

    def test_resume_skips_completed(self, tmp_path):
        oids = [_OID_A, _OID_B, _OID_C]
        # A를 미리 완료 처리
        pre = LfsPushProgress(tmp_path / "prog.json")
        pre.mark([_OID_A])
        pre.save()
        push = _RecordingPush()
        s = _pusher(tmp_path, oids, push).run(batch_size=10)
        assert s.skipped == 1
        assert s.pushed == 2
        assert push.batches == [[_OID_B, _OID_C]]   # A 제외

    def test_failure_stops_by_default(self, tmp_path):
        oids = [_OID_A, _OID_B, _OID_C, "d" * 64]
        push = _RecordingPush(fail_on_batch=2)
        s = _pusher(tmp_path, oids, push).run(batch_size=2)
        assert s.pushed == 2                  # 1번째 배치만
        assert s.ok is False
        assert len(s.failed_oids) == 2        # 2번째 배치 OID
        assert len(push.batches) == 2          # 중단 → 3번째 배치 없음

    def test_continue_on_error(self, tmp_path):
        oids = [_OID_A, _OID_B, _OID_C, "d" * 64]
        push = _RecordingPush(fail_on_batch=1)
        s = _pusher(tmp_path, oids, push).run(batch_size=2, continue_on_error=True)
        assert len(push.batches) == 2          # 실패해도 계속
        assert s.pushed == 2                   # 2번째 배치 성공분
        assert len(s.failed_oids) == 2

    def test_progress_persisted_after_success(self, tmp_path):
        oids = [_OID_A, _OID_B]
        _pusher(tmp_path, oids, _RecordingPush()).run(batch_size=10)
        reloaded = LfsPushProgress(tmp_path / "prog.json")
        reloaded.load()
        assert reloaded.is_done(_OID_A) and reloaded.is_done(_OID_B)
