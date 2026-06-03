"""마이그레이션/복구/컷오버 계열 CLI 핸들러.

import / resync / rebuild-state / reinit-git / preflight / cutover / lfs-push
명령을 처리한다.
"""

from __future__ import annotations

import sys

from p4gitsync.config.sync_config import AppConfig
from p4gitsync.lfs.lfs_store_factory import build_lfs_store


def _run_import(config: AppConfig, stream: str | None, streams: list[str] | None = None) -> None:
    from p4gitsync.git.pack_tuning import ensure_repo_tuned
    from p4gitsync.state.state_store import StateStore

    # 대용량 pack 튜닝을 import 전에 주입 → 이후 checkpoint repack·최종 gc 가 적용받음
    ensure_repo_tuned(config.git.repo_path, config.pack_tuning, bare=config.git.bare)

    state_store = StateStore(config.state.db_path)
    state_store.initialize()

    # LFS object store (활성 시) — import 가 LFS 포인터/객체를 생성하고 tmp 를 정리하도록
    lfs_store = build_lfs_store(
        config.git.repo_path, bare=config.git.bare, enabled=config.lfs.enabled
    )

    p4_client = config.p4.create_client()
    p4_client.connect()

    try:
        if streams:
            # 다중 stream import (branch 관계 보존)
            from p4gitsync.services.multi_stream_importer import MultiStreamImporter
            from p4gitsync.services.user_mapper import UserMapper

            user_mapper = UserMapper(config=config.user_mapping, state_store=state_store)
            importer = MultiStreamImporter(
                p4_client=p4_client,
                state_store=state_store,
                repo_path=config.git.repo_path,
                config=config.initial_import,
                lfs_config=config.lfs if config.lfs.enabled else None,
                lfs_store=lfs_store,
                user_mapper=user_mapper,
            )
            importer.run(streams, config.git.default_branch)
        else:
            # 단일 stream import (기존 동작)
            from p4gitsync.services.initial_importer import InitialImporter

            p4_stream = stream or config.p4.stream
            importer = InitialImporter(
                p4_client=p4_client,
                state_store=state_store,
                repo_path=config.git.repo_path,
                stream=p4_stream,
                config=config.initial_import,
                lfs_config=config.lfs if config.lfs.enabled else None,
                lfs_store=lfs_store,
            )
            importer.run(config.git.default_branch)
    finally:
        p4_client.disconnect()
        state_store.close()


def _run_rebuild_state(config: AppConfig) -> None:
    from p4gitsync.services.recovery import rebuild_state_from_git, _create_git_operator

    git_operator = _create_git_operator(config)
    git_operator.init_repo()

    count = rebuild_state_from_git(config, git_operator)
    print(f"State DB 재구성 완료: {count} commits 복구")


def _run_resync(config: AppConfig, from_cl: int, to_cl: int, stream: str | None) -> None:
    from p4gitsync.services.recovery import resync_range

    p4_stream = stream or config.p4.stream
    count = resync_range(config, from_cl, to_cl, p4_stream)
    print(f"재동기화 완료: {count} CLs (CL {from_cl} ~ {to_cl})")


def _run_reinit_git(config: AppConfig, remote: str) -> None:
    from p4gitsync.services.recovery import reinit_git

    reinit_git(config, remote)
    print(f"Git 리포지토리 재초기화 완료 (from {remote})")


def _run_cutover(config: AppConfig, dry_run: bool) -> None:
    from p4gitsync.services.cutover import CutoverManager

    manager = CutoverManager(config)

    if dry_run:
        result = manager.dry_run()
    else:
        result = manager.execute()

    print(f"\n{'=' * 50}")
    print(f"결과: {result.message}")
    print(f"Phase: {result.phase.value}")
    for detail in result.details:
        print(f"  - {detail}")
    print(f"{'=' * 50}")

    if not result.success:
        sys.exit(1)


def _run_preflight(
    config: AppConfig,
    stream: str | None,
    top_dirs: list[str] | None,
    large_threshold_mb: float,
    output: str | None,
) -> None:
    from p4gitsync.services.preflight import PreflightChecker

    p4_stream = stream or config.p4.stream
    threshold = int(large_threshold_mb * 1024 * 1024)

    p4_client = config.p4.create_client()
    p4_client.connect()
    try:
        checker = PreflightChecker(
            p4_client,
            lfs_config=config.lfs if config.lfs.enabled else None,
        )
        report = checker.run(
            p4_stream,
            top_dirs=top_dirs,
            large_threshold=threshold,
            repo_path=config.git.repo_path or None,
        )
    finally:
        p4_client.disconnect()

    text = report.format_report()
    print(text)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\n리포트 저장: {output}")

    if report.has_blockers:
        sys.exit(1)


def _run_lfs_push(config: AppConfig, args) -> None:
    import os

    from p4gitsync.services.lfs_pusher import LfsPusher, LfsPushProgress

    repo = config.git.repo_path
    progress_path = args.progress_file or os.path.join(repo, ".lfs-push-progress.json")
    progress = LfsPushProgress(progress_path)
    pusher = LfsPusher(repo, remote=args.remote, progress=progress)

    if args.reset_progress:
        pusher.reset_progress()
        print("진행 상태 초기화됨")

    summary = pusher.run(
        batch_size=args.batch_size,
        continue_on_error=args.continue_on_error,
    )
    print(f"LFS push 결과: {summary}")
    if not summary.ok:
        print(f"  실패 OID {len(summary.failed_oids)}건 — 재실행하면 완료분은 건너뛰고 이어서 진행됩니다.")
        sys.exit(1)
