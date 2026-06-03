import argparse
import logging
import sys
import tomllib
from pathlib import Path

from p4gitsync.cli.commands_inspect import _run_preview, _run_tree
from p4gitsync.cli.commands_migrate import (
    _run_cutover,
    _run_import,
    _run_lfs_push,
    _run_preflight,
    _run_rebuild_state,
    _run_reinit_git,
    _run_resync,
)
from p4gitsync.cli.commands_provision import (
    _run_bundle,
    _run_provision,
    _run_provision_github,
    _run_provision_gitlab,
    _run_tune_repo,
)
from p4gitsync.cli.commands_run import _run_service, _run_sync
from p4gitsync.config.logging_config import setup_logging
from p4gitsync.config.sync_config import AppConfig, apply_env_overrides

# 하위호환: 핸들러는 cli/commands_*.py 로 분리됨. 위 import 는 기존
# `from p4gitsync.__main__ import _run_bundle` 류 호출 경로를 유지한다.
__all__ = [
    "load_config",
    "main",
    "_build_parser",
    "_run_sync",
    "_run_service",
    "_run_import",
    "_run_resync",
    "_run_rebuild_state",
    "_run_reinit_git",
    "_run_cutover",
    "_run_preflight",
    "_run_lfs_push",
    "_run_tree",
    "_run_preview",
    "_run_provision",
    "_run_provision_gitlab",
    "_run_provision_github",
    "_run_tune_repo",
    "_run_bundle",
]

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

    preview_parser = subparsers.add_parser(
        "preview", help="import 미리보기 — branch/merge 타임라인 문서 생성",
    )
    preview_parser.add_argument(
        "--depot", help="P4 depot 경로 (미지정 시 p4.stream에서 추출)",
    )
    preview_parser.add_argument(
        "--output", "-o", default="import-preview.md",
        help="출력 파일 경로 (기본: import-preview.md)",
    )
    preview_parser.add_argument(
        "--no-merge-scan", action="store_true",
        help="merge 스캔 생략 (빠른 미리보기, branch 구조만)",
    )
    preview_parser.add_argument(
        "--merge-scan-limit", type=int, default=0,
        help="stream당 merge 스캔 CL 수 제한 (0=전체, 예: 1000=최근 1000건만)",
    )

    preflight_parser = subparsers.add_parser(
        "preflight", help="마이그레이션 사전 점검 (사이징·case충돌·비LFS대용량)",
    )
    preflight_parser.add_argument(
        "--stream", help="P4 stream 경로 (미지정 시 설정 파일의 p4.stream 사용)",
    )
    preflight_parser.add_argument(
        "--top-dirs", nargs="+",
        help="용량을 분류해 볼 최상위 경로들 (예: //depot/main/CODE //depot/main/Art)",
    )
    preflight_parser.add_argument(
        "--large-threshold-mb", type=float, default=5.0,
        help="비-LFS 대용량 탐지 임계 (MiB, 기본: 5)",
    )
    preflight_parser.add_argument(
        "--output", "-o", help="리포트 저장 경로 (미지정 시 콘솔 출력)",
    )

    lfs_push_parser = subparsers.add_parser(
        "lfs-push",
        help="LFS 객체를 배치로 업로드 (재개 가능) — TiB급 마이그레이션용",
    )
    lfs_push_parser.add_argument("--remote", default="origin", help="대상 remote (기본: origin)")
    lfs_push_parser.add_argument("--batch-size", type=int, default=200, help="배치당 OID 수 (기본: 200)")
    lfs_push_parser.add_argument(
        "--continue-on-error", action="store_true",
        help="배치 실패 시 중단하지 않고 계속 (실패 OID는 다음 실행에서 재시도)",
    )
    lfs_push_parser.add_argument(
        "--reset-progress", action="store_true", help="진행 상태를 초기화하고 처음부터",
    )
    lfs_push_parser.add_argument("--progress-file", help="진행 상태 파일 경로")

    provision_parser = subparsers.add_parser(
        "provision",
        help="팀 사용을 위한 권장 설정 파일 생성 (bootstrap/훅/gitconfig/체크리스트)",
    )
    provision_parser.add_argument(
        "--output", "-o", default="provision",
        help="생성물 출력 디렉터리 (기본: provision)",
    )
    provision_parser.add_argument(
        "--max-file-size-mb", type=float, default=5.0,
        help="비-LFS 대용량 차단 임계 (MiB, 기본: 5)",
    )

    pg_parser = subparsers.add_parser(
        "provision-gitlab",
        help="GitLab API로 거버넌스 설정 적용 (push rule/protected branch/merge train)",
    )
    pg_parser.add_argument("--gitlab-url", help="GitLab base URL (또는 env P4GITSYNC_GITLAB_URL)")
    pg_parser.add_argument("--project", help="프로젝트 경로 또는 ID (또는 env P4GITSYNC_GITLAB_PROJECT)")
    pg_parser.add_argument("--token", help="GitLab 토큰 (미지정 시 env GITLAB_TOKEN / P4GITSYNC_GITLAB_TOKEN)")
    pg_parser.add_argument("--max-file-size-mb", type=float, default=5.0)
    pg_parser.add_argument("--protect", nargs="+", default=["main"], help="보호할 브랜치 (기본: main)")
    pg_parser.add_argument("--merge-train", action="store_true", help="merge train 활성화")
    pg_parser.add_argument("--no-lfs", action="store_true", help="LFS 비활성(기본: 활성)")
    pg_parser.add_argument("--dry-run", action="store_true", help="실제 호출 없이 적용 계획만 출력")

    gh_parser = subparsers.add_parser(
        "provision-github",
        help="GitHub Rulesets API로 거버넌스 적용 (push ruleset/branch protection/merge queue)",
    )
    gh_parser.add_argument("--repo", "--repository", dest="repo", help="owner/repo (또는 env P4GITSYNC_GITHUB_REPO)")
    gh_parser.add_argument("--token", help="GitHub 토큰 (미지정 시 env GITHUB_TOKEN / P4GITSYNC_GITHUB_TOKEN)")
    gh_parser.add_argument("--api-url", help="GitHub API base URL (기본: https://api.github.com, GHES는 https://HOST/api/v3, env P4GITSYNC_GITHUB_API_URL)")
    gh_parser.add_argument("--max-file-size-mb", type=float, default=5.0, help="push ruleset 대용량 차단 임계 (MB, 1~100, 기본: 5)")
    gh_parser.add_argument("--protect", nargs="+", default=["main"], help="보호할 브랜치 (기본: main)")
    gh_parser.add_argument("--merge-method", choices=["merge", "squash", "rebase"], default="merge", help="허용할 merge 방식 (기본: merge)")
    gh_parser.add_argument("--merge-queue", action="store_true", help="merge queue 활성화 (GitLab merge train 대응)")
    gh_parser.add_argument("--no-require-pr", action="store_true", help="PR 경유 강제 해제 (기본: PR 강제)")
    gh_parser.add_argument("--required-approvals", type=int, default=1, help="PR 필수 승인 수 (기본: 1)")
    gh_parser.add_argument("--status-check", nargs="+", default=[], help="필수 status check context 목록")
    gh_parser.add_argument("--enforcement", choices=["active", "evaluate", "disabled"], default="active", help="ruleset 적용 모드 (기본: active)")
    gh_parser.add_argument("--dry-run", action="store_true", help="실제 호출 없이 적용 계획만 출력")

    tune_parser = subparsers.add_parser(
        "tune-repo",
        help="대용량 repo git pack/gc 튜닝을 현재 repo 에 적용 (config [pack_tuning])",
    )
    tune_parser.add_argument(
        "--dry-run", action="store_true", help="적용할 설정만 출력(실제 변경 없음)",
    )

    bundle_parser = subparsers.add_parser(
        "bundle",
        help="repo 번들(.bundle) 생성 — 대형 repo 초기 clone 부하를 Bundle URI 로 오프로드",
    )
    bundle_parser.add_argument(
        "--output", "-o", default="repo.bundle", help="번들 출력 경로 (기본: repo.bundle)",
    )
    bundle_parser.add_argument(
        "--all", action="store_true", help="모든 ref 포함(기본: default_branch 만)",
    )

    subparsers.add_parser("setup", help="대화형 설정 마법사 (config.toml 생성/수정)")

    service_parser = subparsers.add_parser("service", help="서비스 관리")
    service_sub = service_parser.add_subparsers(dest="service_command")

    svc_install = service_sub.add_parser("install", help="서비스 등록")
    svc_install.add_argument("--name", default="p4gitsync", help="서비스 이름")

    svc_start = service_sub.add_parser("start", help="서비스 시작")
    svc_start.add_argument("--name", default="p4gitsync", help="서비스 이름")

    svc_stop = service_sub.add_parser("stop", help="서비스 중지")
    svc_stop.add_argument("--name", default="p4gitsync", help="서비스 이름")

    svc_uninstall = service_sub.add_parser("uninstall", help="서비스 제거")
    svc_uninstall.add_argument("--name", default="p4gitsync", help="서비스 이름")

    status_parser = subparsers.add_parser("status", help="동기화 상태 조회")
    status_parser.add_argument("--name", help="특정 서비스만 조회")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    command = args.command or "run"

    # setup, service, status는 config 파일 없이도 실행 가능
    if command == "setup":
        from p4gitsync.cli.setup_wizard import run_setup
        run_setup(args.config)
        return
    if command == "service":
        _run_service(args)
        return
    if command == "provision-gitlab":
        _run_provision_gitlab(args)
        return
    if command == "provision-github":
        _run_provision_github(args)
        return
    if command == "status":
        from p4gitsync.cli.status_reporter import show_status
        show_status(getattr(args, "name", None))
        return

    config = load_config(args.config)
    setup_logging(config.logging.level, config.logging.format, config.logging.file)

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
    elif command == "preview":
        _run_preview(
            config, args.depot, args.output,
            args.no_merge_scan, args.merge_scan_limit,
        )
    elif command == "preflight":
        _run_preflight(
            config, args.stream, getattr(args, "top_dirs", None),
            args.large_threshold_mb, args.output,
        )
    elif command == "provision":
        _run_provision(config, args.output, args.max_file_size_mb)
    elif command == "tune-repo":
        _run_tune_repo(config, args.dry_run)
    elif command == "bundle":
        _run_bundle(config, args)
    elif command == "lfs-push":
        _run_lfs_push(config, args)
    else:
        _run_sync(config)


if __name__ == "__main__":
    main()
