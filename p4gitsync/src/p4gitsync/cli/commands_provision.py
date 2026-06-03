"""프로비저닝/리포 튜닝 계열 CLI 핸들러.

provision / provision-gitlab / provision-github / tune-repo / bundle 명령을
처리한다. argparse 정의는 `__main__._build_parser` 에 유지되고, 여기서는
dispatch 대상 핸들러만 구현한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

from p4gitsync.config.sync_config import AppConfig


def _run_provision_gitlab(args) -> None:
    import os

    from p4gitsync.services.gitlab_provisioner import (
        GitLabClient,
        GitLabProvisioner,
        ProvisionSpec,
    )

    url = args.gitlab_url or os.environ.get("P4GITSYNC_GITLAB_URL")
    project = args.project or os.environ.get("P4GITSYNC_GITLAB_PROJECT")
    token = (
        args.token
        or os.environ.get("GITLAB_TOKEN")
        or os.environ.get("P4GITSYNC_GITLAB_TOKEN")
    )

    if not url or not project:
        print("--gitlab-url 과 --project (또는 P4GITSYNC_GITLAB_URL/PROJECT) 가 필요합니다.", file=sys.stderr)
        sys.exit(2)
    if not token and not args.dry_run:
        print("GitLab 토큰이 필요합니다 (env GITLAB_TOKEN 또는 --token). --dry-run 은 토큰 없이 가능.", file=sys.stderr)
        sys.exit(2)

    spec = ProvisionSpec(
        max_file_size_mb=args.max_file_size_mb,
        lfs_enabled=not args.no_lfs,
        merge_trains=args.merge_train,
        protected_branches=args.protect,
    )
    client = GitLabClient(url, token or "")
    provisioner = GitLabProvisioner(client, project)
    results = provisioner.apply(spec, dry_run=args.dry_run)

    print(f"GitLab 프로비저닝: {project} ({'DRY-RUN' if args.dry_run else url})")
    failed = 0
    for r in results:
        mark = "[OK]" if r.ok else "[FAIL]"
        if not r.ok:
            failed += 1
        print(f"  {mark} [{r.action}] {r.detail}")
    if failed and not args.dry_run:
        sys.exit(1)


def _run_provision_github(args) -> None:
    import os

    from p4gitsync.services.github_provisioner import (
        GitHubClient,
        GitHubProvisioner,
        GitHubSpec,
    )

    repository = args.repo or os.environ.get("P4GITSYNC_GITHUB_REPO")
    api_url = (
        args.api_url
        or os.environ.get("P4GITSYNC_GITHUB_API_URL")
        or "https://api.github.com"
    )
    token = (
        args.token
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("P4GITSYNC_GITHUB_TOKEN")
    )

    if not repository or "/" not in repository:
        print("--repo owner/repo (또는 env P4GITSYNC_GITHUB_REPO) 가 필요합니다.", file=sys.stderr)
        sys.exit(2)
    if not token and not args.dry_run:
        print("GitHub 토큰이 필요합니다 (env GITHUB_TOKEN 또는 --token). --dry-run 은 토큰 없이 가능.", file=sys.stderr)
        sys.exit(2)

    spec = GitHubSpec(
        max_file_size_mb=args.max_file_size_mb,
        protected_branches=args.protect,
        require_pull_request=not args.no_require_pr,
        required_approving_review_count=args.required_approvals,
        require_status_checks=bool(args.status_check),
        status_check_contexts=args.status_check,
        merge_queue=args.merge_queue,
        merge_method=args.merge_method,
        enforcement=args.enforcement,
    )
    client = GitHubClient(token or "", base_url=api_url)
    provisioner = GitHubProvisioner(client, repository)
    results = provisioner.apply(spec, dry_run=args.dry_run)

    print(f"GitHub 프로비저닝: {repository} ({'DRY-RUN' if args.dry_run else api_url})")
    failed = 0
    for r in results:
        mark = "[OK]" if r.ok else "[FAIL]"
        if not r.ok:
            failed += 1
        print(f"  {mark} [{r.action}] {r.detail}")
    if failed and not args.dry_run:
        sys.exit(1)


def _run_tune_repo(config: AppConfig, dry_run: bool) -> None:
    from p4gitsync.git.pack_tuning import (
        apply_to_repo,
        build_pack_config,
        render_gitconfig,
    )

    settings = build_pack_config(config.pack_tuning)
    if not settings:
        print("pack_tuning.enabled=false — 적용할 설정이 없습니다.")
        return

    repo = config.git.repo_path
    if dry_run:
        print(f"[DRY-RUN] {repo} 에 적용할 pack 튜닝:\n")
        print(render_gitconfig(settings))
        return

    applied = apply_to_repo(repo, settings)
    print(f"pack 튜닝 적용 완료: {len(applied)}/{len(settings)}개 설정 ({repo})")
    for key, value in applied:
        print(f"  - {key} = {value}")


def _run_bundle(config: AppConfig, args) -> None:
    import os
    import subprocess

    repo = config.git.repo_path
    output = os.path.abspath(args.output)
    refs = ["--all"] if args.all else [config.git.default_branch]
    result = subprocess.run(
        ["git", "bundle", "create", output, *refs],
        cwd=repo, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"git bundle 생성 실패: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    size = os.path.getsize(output) if os.path.exists(output) else 0
    scope = "all refs" if args.all else config.git.default_branch
    print(f"번들 생성 완료: {output} ({size:,} bytes, refs={scope})")
    print(
        "배포: 이 번들을 CDN/오브젝트 스토리지에 업로드 후, clone 시 "
        "`git clone --bundle-uri=<URL>` 또는 bootstrap 의 BUNDLE_URI 환경변수로 사용하세요."
    )


def _run_provision(config: AppConfig, output: str, max_file_size_mb: float) -> None:
    import dataclasses

    from p4gitsync.services import provisioner

    out_dir = Path(output)
    out_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = int(max_file_size_mb * 1024 * 1024)
    remote = config.git.remote_url or "git@gitlab.example.com:org/repo.git"
    branch = config.git.default_branch

    from p4gitsync.git.pack_tuning import build_pack_config, render_gitconfig

    artifacts: dict[str, str] = {
        "bootstrap-clone.sh": provisioner.generate_bootstrap_sh(remote, branch),
        "bootstrap-clone.ps1": provisioner.generate_bootstrap_ps1(remote, branch),
        "pre-receive": provisioner.generate_pre_receive_hook(max_bytes),
        "recommended.gitconfig": provisioner.generate_gitconfig_snippet(),
        "GITLAB-SETUP.md": provisioner.generate_gitlab_checklist(max_bytes),
    }
    # 대용량 repo pack/gc 튜닝(서버/호스트 repo 적용용). `tune-repo` 가 동일 설정을 주입.
    pack_settings = build_pack_config(config.pack_tuning)
    if pack_settings:
        artifacts["recommended-repo.gitconfig"] = (
            "# p4gitsync provision — 대용량 repo pack/gc 튜닝\n"
            "# 적용: 대상 repo 에서 `p4gitsync tune-repo`, 또는 아래를 .git/config 에 병합.\n"
            + render_gitconfig(pack_settings)
        )
    # LFS 사용 시 하드닝된 .gitattributes도 함께 제공
    if config.lfs.enabled:
        hardened = dataclasses.replace(config.lfs, text_normalization=True)
        artifacts[".gitattributes"] = hardened.generate_gitattributes()

    for name, content in artifacts.items():
        (out_dir / name).write_text(content, encoding="utf-8", newline="\n")

    # 실행 권한 (POSIX)
    for name in ("bootstrap-clone.sh", "pre-receive"):
        try:
            path = out_dir / name
            path.chmod(path.stat().st_mode | 0o111)
        except OSError:
            pass

    print(f"권장 설정 생성 완료: {out_dir.resolve()}")
    for name in artifacts:
        print(f"  - {name}")
    print("\n다음: bootstrap-clone 으로 개발자 clone, pre-receive·GITLAB-SETUP.md 로 서버 게이트 설정")
