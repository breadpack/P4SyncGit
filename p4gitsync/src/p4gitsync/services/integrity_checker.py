from __future__ import annotations

import hashlib
import logging
import random
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum

from p4gitsync.p4.p4_client import P4Client
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.integrity")


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


class IntegrityChecker:
    """P4 파일과 Git 파일의 내용을 비교하여 무결성을 검증한다."""

    def __init__(
        self,
        p4_client: P4Client,
        repo_path: str,
        stream: str,
        daily_sample_count: int = 100,
    ) -> None:
        self._p4 = p4_client
        self._repo_path = repo_path
        self._stream = stream
        self._daily_sample_count = daily_sample_count
        self._last_daily: float = 0.0
        self._last_weekly: float = 0.0
        self._last_monthly: float = 0.0

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

    def verify_sample(self, sample_count: int) -> IntegrityResult:
        """N개 파일을 샘플링하여 해시 비교."""
        git_files = self._list_git_files()
        if not git_files:
            return IntegrityResult(passed=True, checked_files=0)

        sample = random.sample(git_files, min(sample_count, len(git_files)))
        return self._compare_files(sample)

    def verify_full(self) -> IntegrityResult:
        """전체 파일 해시 비교."""
        git_files = self._list_git_files()
        return self._compare_files(git_files)

    def verify_random(self) -> IntegrityResult:
        """랜덤 비율(10~30%)의 파일 비교."""
        git_files = self._list_git_files()
        if not git_files:
            return IntegrityResult(passed=True, checked_files=0)

        ratio = random.uniform(0.1, 0.3)
        count = max(1, int(len(git_files) * ratio))
        sample = random.sample(git_files, min(count, len(git_files)))
        return self._compare_files(sample)

    def _list_git_files(self) -> list[str]:
        """Git HEAD의 파일 목록을 조회."""
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

    def _compare_files(self, git_paths: list[str]) -> IntegrityResult:
        """Git 파일과 P4 파일의 해시를 비교."""
        mismatched: list[str] = []
        checked = 0
        stream_prefix = self._stream + "/"

        for git_path in git_paths:
            depot_path = f"{stream_prefix}{git_path}"
            try:
                git_hash = self._get_git_file_hash(git_path)
                p4_hash = self._get_p4_file_hash(depot_path)

                if git_hash is None or p4_hash is None:
                    continue

                checked += 1
                if git_hash != p4_hash:
                    mismatched.append(git_path)
                    logger.warning(
                        "무결성 불일치: %s (git=%s, p4=%s)",
                        git_path, git_hash[:12], p4_hash[:12],
                    )
            except Exception:
                logger.debug("무결성 검증 건너뜀: %s", git_path, exc_info=True)

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

    def _get_git_file_hash(self, git_path: str) -> str | None:
        """Git에서 파일 내용의 SHA256 해시를 가져온다."""
        result = subprocess.run(
            ["git", "show", f"HEAD:{git_path}"],
            cwd=self._repo_path,
            capture_output=True,
        )
        if result.returncode != 0:
            return None
        return hashlib.sha256(result.stdout).hexdigest()

    def _get_p4_file_hash(self, depot_path: str) -> str | None:
        """P4에서 최신 리비전의 파일 내용 SHA256 해시를 가져온다."""
        content = self._p4.print_file_to_bytes_head(depot_path)
        if content is None:
            return None
        return hashlib.sha256(content).hexdigest()
