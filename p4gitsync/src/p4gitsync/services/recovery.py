"""장애 복구 서비스: State DB 재구성, CL 범위 재동기화, Git 재초기화."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from p4gitsync.config.sync_config import AppConfig
from p4gitsync.git.git_operator import GitOperator
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.services.commit_builder import CommitBuilder
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.recovery")

# commit message에서 P4CL 메타데이터를 추출하는 패턴
_P4CL_PATTERN = re.compile(r"\[P4CL:\s*(\d+)\]")
_INTEGRATION_PATTERN = re.compile(
    r"\[Integration:\s*(//\S+)\s*->\s*(//\S+)\]"
)


def rebuild_state_from_git(
    config: AppConfig,
    git_operator: GitOperator,
) -> int:
    """Git log에서 P4CL 메타데이터를 추출하여 State DB를 재구성.

    Returns:
        복구된 commit 수.
    """
    state_store = StateStore(config.state.db_path)
    state_store.initialize()

    branch = config.git.default_branch
    repo_path = config.git.repo_path

    result = subprocess.run(
        ["git", "log", "--format=%H%n%B%n---END---", "--reverse", branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git log 실패: {result.stderr}")

    recovered = 0
    entries = result.stdout.split("---END---\n")

    stream = config.p4.stream

    with state_store.transaction():
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue

            lines = entry.split("\n", 1)
            if len(lines) < 2:
                continue

            sha = lines[0].strip()
            body = lines[1]

            cl_match = _P4CL_PATTERN.search(body)
            if not cl_match:
                continue

            cl = int(cl_match.group(1))
            has_integration = bool(_INTEGRATION_PATTERN.search(body))

            state_store.record_commit(
                cl, sha, stream, branch, has_integration=has_integration,
            )
            state_store.set_last_synced_cl(stream, cl, sha)
            recovered += 1

    state_store.close()
    logger.info("State DB 재구성 완료: %d commits 복구", recovered)
    return recovered


def resync_range(
    config: AppConfig,
    from_cl: int,
    to_cl: int,
    stream: str,
) -> int:
    """특정 CL 범위를 재동기화.

    Returns:
        재동기화된 CL 수.
    """
    state_store = StateStore(config.state.db_path)
    state_store.initialize()

    p4_client = P4Client(
        port=config.p4.port,
        user=config.p4.user,
        workspace=config.p4.workspace,
    )
    p4_client.connect()

    git_operator = _create_git_operator(config)
    git_operator.init_repo()

    mapping = state_store.get_stream_mapping(stream)
    branch = mapping.branch if mapping else config.git.default_branch

    commit_builder = CommitBuilder(
        p4_client=p4_client,
        git_operator=git_operator,
        state_store=state_store,
        stream=stream,
    )

    synced = 0
    try:
        changes = p4_client.get_changes_after(stream, from_cl - 1)
        target_changes = [cl for cl in changes if from_cl <= cl <= to_cl]

        for cl in target_changes:
            info = p4_client.describe(cl)

            last_cl = state_store.get_last_synced_cl(stream)
            parent_sha = (
                state_store.get_commit_sha(last_cl, stream) if last_cl > 0 else None
            )

            sha = commit_builder.build_commit(info, branch, parent_sha)
            state_store.record_commit(
                cl, sha, stream, branch,
                has_integration=commit_builder._last_has_integration,
            )
            state_store.set_last_synced_cl(stream, cl, sha)
            synced += 1
            logger.info("재동기화 CL %d -> %s", cl, sha[:8])

    finally:
        p4_client.disconnect()
        state_store.close()

    logger.info("재동기화 완료: %d CLs (CL %d ~ %d)", synced, from_cl, to_cl)
    return synced


def reinit_git(config: AppConfig, remote_url: str) -> None:
    """Git 리포지토리를 재초기화하고 remote에서 clone.

    기존 로컬 repo를 백업 후 삭제하고, remote에서 새로 clone합니다.
    """
    repo_path = Path(config.git.repo_path)
    backup_path = repo_path.parent / f"{repo_path.name}.backup"

    if repo_path.exists():
        if backup_path.exists():
            shutil.rmtree(backup_path)
        repo_path.rename(backup_path)
        logger.info("기존 repo 백업: %s -> %s", repo_path, backup_path)

    result = subprocess.run(
        ["git", "clone", remote_url, str(repo_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if backup_path.exists():
            backup_path.rename(repo_path)
            logger.error("clone 실패, 백업에서 복원: %s", result.stderr)
        raise RuntimeError(f"git clone 실패: {result.stderr}")

    logger.info("Git 리포지토리 재초기화 완료: %s (from %s)", repo_path, remote_url)


def _create_git_operator(config: AppConfig) -> GitOperator:
    """설정 기반 GitOperator 생성 (sync_orchestrator와 동일 로직)."""
    backend = config.git.backend
    repo_path = config.git.repo_path
    remote_url = config.git.remote_url

    if backend == "cli":
        from p4gitsync.git.git_cli_operator import GitCliOperator
        return GitCliOperator(repo_path=repo_path, remote_url=remote_url)

    from p4gitsync.git.pygit2_git_operator import Pygit2GitOperator
    return Pygit2GitOperator(repo_path=repo_path, remote_url=remote_url)
