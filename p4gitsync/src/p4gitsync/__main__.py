import argparse
import logging
import signal
import sys
import tomllib
from pathlib import Path

from p4gitsync.config.logging_config import setup_logging
from p4gitsync.config.sync_config import AppConfig, apply_env_overrides
from p4gitsync.services.sync_orchestrator import SyncOrchestrator

logger = logging.getLogger("p4gitsync")


def load_config(path: str = "config.toml") -> AppConfig:
    config_path = Path(path)
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    else:
        raw = {}
    raw = apply_env_overrides(raw)
    if not raw.get("p4") and not raw.get("git") and not raw.get("state"):
        print(
            f"설정 파일({path})이 없고 환경변수(P4GITSYNC_*)도 설정되지 않았습니다.",
            file=sys.stderr,
        )
        sys.exit(1)
    return AppConfig.from_dict(raw)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="p4gitsync",
        description="P4 -> Git 동기화 도구",
    )
    parser.add_argument(
        "--config", default="config.toml", help="설정 파일 경로 (기본: config.toml)",
    )

    subparsers = parser.add_subparsers(dest="command", help="실행할 명령")

    subparsers.add_parser("run", help="동기화 루프 실행 (기본)")

    import_parser = subparsers.add_parser("import", help="초기 히스토리 import")
    import_parser.add_argument(
        "--stream", help="P4 stream 경로 (미지정 시 설정 파일의 p4.stream 사용)",
    )
    import_parser.add_argument(
        "--streams", nargs="+",
        help="다중 stream import (branch 관계 보존). 예: //depot/main //depot/develop",
    )

    subparsers.add_parser(
        "rebuild-state", help="Git log에서 State DB 재구성",
    )

    resync_parser = subparsers.add_parser("resync", help="특정 CL 범위 재동기화")
    resync_parser.add_argument("--from", dest="from_cl", type=int, required=True, help="시작 CL")
    resync_parser.add_argument("--to", dest="to_cl", type=int, required=True, help="종료 CL")
    resync_parser.add_argument(
        "--stream", help="P4 stream 경로 (미지정 시 설정 파일의 p4.stream 사용)",
    )

    reinit_parser = subparsers.add_parser("reinit-git", help="Git repo 재초기화 (remote clone)")
    reinit_parser.add_argument("--remote", required=True, help="Git remote URL")

    cutover_parser = subparsers.add_parser("cutover", help="P4→Git 컷오버 실행")
    cutover_group = cutover_parser.add_mutually_exclusive_group(required=True)
    cutover_group.add_argument("--dry-run", action="store_true", help="컷오버 시뮬레이션 (실제 변경 없음)")
    cutover_group.add_argument("--execute", action="store_true", help="컷오버 실행")

    tree_parser = subparsers.add_parser("tree", help="P4 Stream 계층 트리 미리보기")
    tree_parser.add_argument(
        "--depot", help="P4 depot 경로 (미지정 시 p4.stream에서 추출)",
    )
    tree_parser.add_argument(
        "--include-deleted", action="store_true", help="삭제된 stream 포함",
    )
    tree_parser.add_argument(
        "--include-virtual", action="store_true", help="virtual stream 포함",
    )

    return parser


def _run_sync(config: AppConfig) -> None:
    with SyncOrchestrator(config) as orchestrator:
        if config.api.enabled:
            from p4gitsync.api.api_server import ApiServer

            api_server = ApiServer(
                host=config.api.host,
                port=config.api.port,
                trigger_secret=config.api.trigger_secret,
                redis_config=config.redis if config.redis.enabled else None,
                state_store=orchestrator.state_store,
                event_consumer=orchestrator.event_consumer,
                circuit_breaker=orchestrator.circuit_breaker,
            )
            api_server.start_in_thread()

        def _signal_handler(signum: int, frame: object) -> None:
            orchestrator.stop()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        orchestrator.start()


def _run_import(config: AppConfig, stream: str | None, streams: list[str] | None = None) -> None:
    from p4gitsync.p4.p4_client import P4Client
    from p4gitsync.state.state_store import StateStore

    state_store = StateStore(config.state.db_path)
    state_store.initialize()

    p4_client = P4Client(
        port=config.p4.port,
        user=config.p4.user,
        workspace=config.p4.workspace,
    )
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


def _run_tree(config: AppConfig, depot: str | None, include_deleted: bool, include_virtual: bool = False) -> None:
    from p4gitsync.p4.p4_client import P4Client
    from p4gitsync.services.stream_tree_viewer import StreamTreeViewer

    # depot 추출
    if depot:
        p4_depot = depot
    else:
        stream = config.p4.stream
        parts = stream.rstrip("/").split("/")
        p4_depot = "/".join(parts[:3])  # //depot

    p4_client = P4Client(
        port=config.p4.port,
        user=config.p4.user,
        workspace=config.p4.workspace,
    )
    p4_client.connect()

    try:
        viewer = StreamTreeViewer(p4_client)
        roots = viewer.build_tree(
            p4_depot,
            default_branch=config.git.default_branch,
            include_deleted=include_deleted,
            include_virtual=include_virtual,
        )

        if not roots:
            print(f"Stream을 찾을 수 없습니다: {p4_depot}")
            return

        print(f"\nP4 Stream Tree: {p4_depot}")
        print("=" * 60)
        print(viewer.format_tree(roots))
        print(viewer.format_summary(roots))
    finally:
        p4_client.disconnect()


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.logging.level, config.logging.format, config.logging.file)

    command = args.command or "run"

    if command == "import":
        _run_import(config, args.stream, getattr(args, "streams", None))
    elif command == "rebuild-state":
        _run_rebuild_state(config)
    elif command == "resync":
        _run_resync(config, args.from_cl, args.to_cl, args.stream)
    elif command == "reinit-git":
        _run_reinit_git(config, args.remote)
    elif command == "cutover":
        _run_cutover(config, args.dry_run)
    elif command == "tree":
        _run_tree(config, args.depot, args.include_deleted, args.include_virtual)
    else:
        _run_sync(config)


if __name__ == "__main__":
    main()
