"""TiB급 LFS 객체를 배치 단위로 업로드(재개 가능).

`git lfs push --all`은 한 번의 호출로 모든 LFS 객체를 전송하므로, TiB 규모에서
중간 실패 시 진행 상황이 날아가고 제어가 불가능하다. 이 모듈은:

1. repo의 LFS OID를 열거(`git lfs ls-files --all --long`)하고 중복 제거,
2. OID를 배치로 나눠 `git lfs push --object-id <remote> <oids...>`로 업로드,
3. 완료한 OID를 진행 파일(JSON)에 기록 → 재실행 시 이미 올린 것은 건너뜀.

push_fn / ls-files 실행부는 주입 가능해 네트워크 없이 단위 테스트된다.

워크플로우: `git push`(refs) 와 별개로 이 명령으로 LFS 객체를 배치 업로드한다.
LFS 객체는 ref와 독립적으로 업로드 가능하므로 순서 무관(권장: 객체 먼저).
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Callable

logger = logging.getLogger("p4gitsync.lfs_pusher")

_OID_LEN = 64  # sha256 hex


# ── 순수 함수 (단위 테스트 대상) ──────────────────────────────


def parse_ls_files_oids(output: str) -> list[str]:
    """`git lfs ls-files --long` 출력에서 OID를 추출(순서 보존 중복 제거).

    각 줄 형식: "<oid> <*|-> <path>" (oid는 64자 hex sha256).
    """
    seen: set[str] = set()
    oids: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split(None, 1)[0]
        if len(token) == _OID_LEN and all(c in "0123456789abcdef" for c in token.lower()):
            if token not in seen:
                seen.add(token)
                oids.append(token)
    return oids


def chunk(seq: list[str], size: int) -> list[list[str]]:
    """리스트를 size 단위 배치로 분할."""
    if size <= 0:
        raise ValueError("batch size는 1 이상이어야 합니다")
    return [seq[i:i + size] for i in range(0, len(seq), size)]


# ── 진행 상태 ────────────────────────────────────────────────


class LfsPushProgress:
    """완료한 OID 집합을 JSON 파일에 영속화."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._done: set[str] = set()

    def load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._done = set(data.get("pushed", []))
            except (json.JSONDecodeError, OSError):
                logger.warning("진행 파일 손상/읽기 실패, 새로 시작: %s", self._path)
                self._done = set()

    def save(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"pushed": sorted(self._done)}), encoding="utf-8",
        )
        tmp.replace(self._path)

    def reset(self) -> None:
        self._done = set()
        self._path.unlink(missing_ok=True)

    def is_done(self, oid: str) -> bool:
        return oid in self._done

    def mark(self, oids: list[str]) -> None:
        self._done.update(oids)

    @property
    def count(self) -> int:
        return len(self._done)


# ── 결과 ─────────────────────────────────────────────────────


class PushSummary:
    def __init__(self) -> None:
        self.total = 0
        self.skipped = 0
        self.pushed = 0
        self.batches = 0
        self.failed_oids: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.failed_oids

    def __str__(self) -> str:
        return (
            f"총 {self.total} OID / 건너뜀 {self.skipped} / 업로드 {self.pushed} "
            f"/ 배치 {self.batches} / 실패 {len(self.failed_oids)}"
        )


# ── 푸셔 ─────────────────────────────────────────────────────


PushFn = Callable[[list[str]], bool]


class LfsPusher:
    """LFS 객체를 배치로 업로드(재개 가능)."""

    def __init__(
        self,
        repo_path: str,
        remote: str = "origin",
        progress: LfsPushProgress | None = None,
        push_fn: PushFn | None = None,
        ls_files_fn: Callable[[], str] | None = None,
    ) -> None:
        self._repo_path = repo_path
        self._remote = remote
        self._progress = progress or LfsPushProgress(
            Path(repo_path) / ".lfs-push-progress.json",
        )
        self._push_fn = push_fn or self._default_push
        self._ls_files_fn = ls_files_fn or self._default_ls_files

    def _default_ls_files(self) -> str:
        result = subprocess.run(
            ["git", "lfs", "ls-files", "--all", "--long"],
            cwd=self._repo_path, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git lfs ls-files 실패: {result.stderr}")
        return result.stdout

    def _default_push(self, oids: list[str]) -> bool:
        result = subprocess.run(
            ["git", "lfs", "push", "--object-id", self._remote, *oids],
            cwd=self._repo_path, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error("LFS push 배치 실패: %s", (result.stderr or "")[-500:])
        return result.returncode == 0

    def enumerate_oids(self) -> list[str]:
        return parse_ls_files_oids(self._ls_files_fn())

    def run(
        self,
        oids: list[str] | None = None,
        batch_size: int = 200,
        continue_on_error: bool = False,
    ) -> PushSummary:
        self._progress.load()
        if oids is None:
            oids = self.enumerate_oids()

        summary = PushSummary()
        summary.total = len(oids)
        pending = [o for o in oids if not self._progress.is_done(o)]
        summary.skipped = summary.total - len(pending)
        if summary.skipped:
            logger.info("이미 업로드됨(재개): %d OID 건너뜀", summary.skipped)

        for batch in chunk(pending, batch_size):
            summary.batches += 1
            logger.info(
                "배치 %d 업로드 중 (%d OID)...", summary.batches, len(batch),
            )
            if self._push_fn(batch):
                self._progress.mark(batch)
                self._progress.save()
                summary.pushed += len(batch)
            else:
                summary.failed_oids.extend(batch)
                if not continue_on_error:
                    logger.error(
                        "배치 실패 — 중단. 재실행하면 완료분은 건너뛰고 이어서 진행됩니다.",
                    )
                    break
                logger.warning("배치 실패 — continue-on-error로 계속 진행")

        return summary

    def reset_progress(self) -> None:
        self._progress.reset()
