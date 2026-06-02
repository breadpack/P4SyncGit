from __future__ import annotations

import hashlib
import logging
import os
import random
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.lfs.lfs_pointer_utils import is_lfs_pointer, parse_lfs_pointer
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.services import integrity_compare as ic

logger = logging.getLogger("p4gitsync.integrity")

_MD5_CHUNK = 4 * 1024 * 1024  # 4MB


class CheckSchedule(Enum):
    DAILY_SAMPLE = "daily_sample"
    WEEKLY_FULL = "weekly_full"
    MONTHLY_RANDOM = "monthly_random"


@dataclass
class IntegrityResult:
    passed: bool
    checked_files: int
    mismatched_files: list[str] = field(default_factory=list)
    schedule: str = ""
    error: str = ""
    total_files: int = 0      # 검증 대상 모집단(샘플링 시 커버리지 표시)
    strategy: str = ""        # full | sample | smart | random


class IntegrityChecker:
    """P4 파일과 Git 파일의 내용을 비교하여 무결성을 검증한다.

    - 일반 파일: git blob 과 P4 콘텐츠의 SHA256 비교.
    - LFS 파일: git tree 에는 pointer 텍스트만 있으므로, 로컬 LFS object 의 MD5 +
      크기를 P4 fstat digest(콘텐츠 전송 없음)와 교차검증한다. 메타가 없으면
      P4 콘텐츠 SHA256 ↔ pointer.oid 로 fallback.
    - 대형 depot(컷오버)에서는 ``p4_config`` + ``max_workers`` 로 다중 P4 연결
      병렬 검증, ``verify_cutover`` 로 대형 파일 전수 + 나머지 샘플 전략 사용.
    """

    def __init__(
        self,
        p4_client: P4Client,
        repo_path: str,
        stream: str,
        daily_sample_count: int = 100,
        lfs_store: LfsObjectStore | None = None,
        p4_config=None,
        max_workers: int = 1,
    ) -> None:
        self._p4 = p4_client
        self._repo_path = repo_path
        self._stream = stream
        self._daily_sample_count = daily_sample_count
        self._lfs_store = lfs_store
        self._p4_config = p4_config
        self._max_workers = max(1, max_workers)
        self._last_daily: float = 0.0
        self._last_weekly: float = 0.0
        self._last_monthly: float = 0.0

    # ── 스케줄 ───────────────────────────────────────────

    def check_due_schedule(self) -> CheckSchedule | None:
        """현재 시간 기준으로 실행해야 할 검증 스케줄을 반환."""
        now = time.time()
        day = 86400
        if now - self._last_monthly >= 30 * day:
            return CheckSchedule.MONTHLY_RANDOM
        if now - self._last_weekly >= 7 * day:
            return CheckSchedule.WEEKLY_FULL
        if now - self._last_daily >= day:
            return CheckSchedule.DAILY_SAMPLE
        return None

    def run_scheduled_check(self) -> IntegrityResult | None:
        """스케줄에 따라 무결성 검증을 실행한다. 실행할 것이 없으면 None."""
        schedule = self.check_due_schedule()
        if schedule is None:
            return None

        if schedule == CheckSchedule.DAILY_SAMPLE:
            result = self.verify_sample(self._daily_sample_count)
            self._last_daily = time.time()
        elif schedule == CheckSchedule.WEEKLY_FULL:
            result = self.verify_full()
            self._last_weekly = time.time()
            self._last_daily = time.time()
        else:
            result = self.verify_random()
            self._last_monthly = time.time()
            self._last_weekly = time.time()
            self._last_daily = time.time()

        result.schedule = schedule.value
        return result

    # ── 검증 진입점 ──────────────────────────────────────

    def verify_sample(self, sample_count: int) -> IntegrityResult:
        """N개 파일을 샘플링하여 비교."""
        git_files = self._list_git_files()
        if not git_files:
            return IntegrityResult(passed=True, checked_files=0)
        sample = random.sample(git_files, min(sample_count, len(git_files)))
        res = self._compare_files(sample)
        res.total_files = len(git_files)
        res.strategy = "sample"
        return res

    def verify_full(self) -> IntegrityResult:
        """전체 파일 비교."""
        git_files = self._list_git_files()
        res = self._compare_files(git_files)
        res.total_files = len(git_files)
        res.strategy = "full"
        return res

    def verify_random(self) -> IntegrityResult:
        """랜덤 비율(10~30%)의 파일 비교."""
        git_files = self._list_git_files()
        if not git_files:
            return IntegrityResult(passed=True, checked_files=0)
        ratio = random.uniform(0.1, 0.3)
        count = max(1, int(len(git_files) * ratio))
        sample = random.sample(git_files, min(count, len(git_files)))
        res = self._compare_files(sample)
        res.total_files = len(git_files)
        res.strategy = "random"
        return res

    def verify_cutover(
        self,
        *,
        mode: str = "smart",
        sample_count: int = 1000,
        large_threshold_bytes: int = 5 * 1024 * 1024,
    ) -> IntegrityResult:
        """컷오버용 검증. 대형 depot 에서 전수 검사 시간을 통제한다.

        - ``full``  : 전체 파일(병렬).
        - ``sample``: 무작위 N개(병렬).
        - ``smart`` : 임계 이상 대형 파일은 **전수**(고위험·LFS), 나머지 코드 파일은
          무작위 N개 샘플. 대형 에셋 정합성을 보장하면서 시간을 통제.
        """
        git_files = self._list_git_files()
        if not git_files:
            return IntegrityResult(passed=True, checked_files=0, strategy=mode)

        if mode == "full":
            targets = git_files
        elif mode == "sample":
            targets = random.sample(git_files, min(sample_count, len(git_files)))
        else:
            mode = "smart"
            targets = self._select_smart(git_files, sample_count, large_threshold_bytes)

        logger.info(
            "컷오버 무결성 검증 시작: mode=%s, 대상 %d/%d 파일, 워커=%d",
            mode, len(targets), len(git_files), self._max_workers,
        )
        res = self._compare_files(targets)
        res.total_files = len(git_files)
        res.strategy = mode
        return res

    def _select_smart(
        self, git_files: list[str], sample_count: int, threshold: int,
    ) -> list[str]:
        """대형 파일(>=threshold) 전수 + 나머지 무작위 샘플."""
        try:
            sizes = self._p4.iter_file_sizes(f"{self._stream}/...#head")
        except Exception:
            logger.warning("head 크기 조회 실패 — 무작위 샘플로 대체", exc_info=True)
            return random.sample(git_files, min(sample_count, len(git_files)))

        prefix_len = len(self._stream) + 1
        size_by_git = {
            depot[prefix_len:]: size
            for depot, size in sizes
            if depot.startswith(self._stream + "/")
        }
        large = [g for g in git_files if size_by_git.get(g, 0) >= threshold]
        rest = [g for g in git_files if size_by_git.get(g, 0) < threshold]
        sampled = random.sample(rest, min(sample_count, len(rest))) if rest else []
        # 대형 전수 + 샘플, 순서 유지 dedup
        return list(dict.fromkeys(large + sampled))

    # ── 비교 디스패치 (직렬/병렬) ─────────────────────────

    def _compare_files(self, git_paths: list[str]) -> IntegrityResult:
        if (
            self._max_workers > 1
            and self._p4_config is not None
            and len(git_paths) > 1
        ):
            checked, mismatched = self._compare_parallel(git_paths)
        else:
            checked, mismatched = self._verify_slice(git_paths, self._p4)

        passed = len(mismatched) == 0
        if passed:
            logger.info("무결성 검증 통과: %d개 파일 확인", checked)
        else:
            logger.error(
                "무결성 검증 실패: %d/%d개 파일 불일치", len(mismatched), checked,
            )
        return IntegrityResult(
            passed=passed, checked_files=checked, mismatched_files=mismatched,
        )

    def _compare_parallel(
        self, git_paths: list[str],
    ) -> tuple[int, list[str]]:
        """다중 P4 연결로 git_paths 를 분할 병렬 검증."""
        n = min(self._max_workers, len(git_paths))
        slices: list[list[str]] = [git_paths[i::n] for i in range(n)]
        clients = [self._p4_config.create_client() for _ in range(n)]
        for c in clients:
            c.connect()

        results: list[tuple[int, list[str]]] = [(0, []) for _ in range(n)]
        threads: list[threading.Thread] = []

        def work(idx: int) -> None:
            results[idx] = self._verify_slice(slices[idx], clients[idx])

        try:
            for i in range(n):
                t = threading.Thread(target=work, args=(i,), name=f"verify-{i}", daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
        finally:
            for c in clients:
                try:
                    c.disconnect()
                except Exception:
                    pass

        checked = sum(c for c, _ in results)
        mismatched: list[str] = []
        for _, m in results:
            mismatched.extend(m)
        return checked, mismatched

    # ── 슬라이스 검증 (단일 P4 연결) ──────────────────────

    def _verify_slice(
        self, git_paths: list[str], p4: P4Client,
    ) -> tuple[int, list[str]]:
        """주어진 P4 연결로 git_paths 를 분류 후 검증. (checked, mismatched) 반환."""
        stream_prefix = self._stream + "/"

        normals: list[tuple[str, str, str]] = []   # (git_path, depot, git_sha256)
        lfs_items: list[tuple[str, str, object]] = []  # (git_path, depot, LfsPointer)
        for git_path in git_paths:
            blob = self._git_blob(git_path)
            if blob is None:
                continue
            depot = stream_prefix + git_path
            if is_lfs_pointer(blob):
                try:
                    ptr = parse_lfs_pointer(blob)
                except ValueError:
                    logger.debug("LFS pointer 파싱 실패(스킵): %s", git_path)
                    continue
                lfs_items.append((git_path, depot, ptr))
            else:
                normals.append((git_path, depot, hashlib.sha256(blob).hexdigest()))

        checked = 0
        mismatched: list[str] = []

        # LFS: digest 를 한 번에 배치 조회(콘텐츠 전송 없음)
        lfs_meta: dict[str, tuple[str, int]] = {}
        if lfs_items:
            lfs_meta = p4.head_digests([depot for _, depot, _ in lfs_items])

        for git_path, depot, ptr in lfs_items:
            verdict = self._verify_lfs(git_path, depot, ptr, lfs_meta.get(depot), p4)
            if verdict is None:
                continue
            checked += 1
            if verdict:
                mismatched.append(git_path)

        for git_path, depot, git_sha in normals:
            content = p4.print_file_to_bytes_head(depot)
            if content is None:
                continue
            checked += 1
            status, reason = ic.decide_normal(
                git_sha, hashlib.sha256(content).hexdigest(),
            )
            if status == ic.MISMATCH:
                mismatched.append(git_path)
                logger.warning("무결성 불일치: %s — %s", git_path, reason)

        return checked, mismatched

    def _verify_lfs(
        self, git_path: str, depot: str, ptr, meta: tuple[str, int] | None, p4: P4Client,
    ) -> bool | None:
        """단일 LFS 파일 검증. True=불일치, False=일치, None=스킵."""
        object_exists = self._lfs_store.exists(ptr.oid) if self._lfs_store else None
        local_md5: str | None = None
        if self._lfs_store and object_exists:
            try:
                local_md5 = self._md5_file(self._lfs_store.object_path(ptr.oid))
            except OSError:
                local_md5 = None
        p4_md5, p4_size = (meta if meta else (None, None))

        status, reason = ic.decide_lfs(
            ptr_oid=ptr.oid, ptr_size=ptr.size,
            object_exists=object_exists,
            p4_size=p4_size, p4_md5=p4_md5, local_md5=local_md5,
        )

        if status == ic.NEED_CONTENT:
            status, reason = self._verify_lfs_by_content(ptr, depot, p4)
            if status is None:
                return None

        if status == ic.MISMATCH:
            logger.warning("무결성 불일치(LFS): %s — %s", git_path, reason)
            return True
        return False

    def _verify_lfs_by_content(
        self, ptr, depot: str, p4: P4Client,
    ) -> tuple[str | None, str]:
        """메타로 판정 불가 시 P4 콘텐츠를 스트리밍 해시해 pointer.oid 와 비교."""
        tmp_dir = self._lfs_store.tmp_dir if self._lfs_store else self._repo_path
        tmp_path = None
        try:
            tmp_path = p4.print_head_to_disk(depot, tmp_dir)
            sha = self._sha256_file(tmp_path)
        except (OSError, RuntimeError):
            logger.debug("LFS 콘텐츠 해시 실패(스킵): %s", depot, exc_info=True)
            return (None, "")
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return ic.decide_lfs_content(ptr.oid, sha)

    # ── 저수준 유틸 ──────────────────────────────────────

    def _list_git_files(self) -> list[str]:
        """Git HEAD의 파일 목록을 조회(.* 최상위 메타 제외)."""
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=self._repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        return [
            f for f in result.stdout.strip().split("\n")
            if f and not f.startswith(".")
        ]

    def _git_blob(self, git_path: str) -> bytes | None:
        """Git HEAD 의 blob 바이트(LFS면 pointer 텍스트). smudge 미적용."""
        result = subprocess.run(
            ["git", "show", f"HEAD:{git_path}"],
            cwd=self._repo_path,
            capture_output=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout

    @staticmethod
    def _md5_file(path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_MD5_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _sha256_file(path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_MD5_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()
