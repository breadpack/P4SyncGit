"""대용량 repo 를 위한 git pack/gc 저수준 튜닝.

5TB급 마이그레이션 repo 는 packfile 이 거대하고 gc/repack 시 메모리·시간 부담이
크다. 다음을 repo 설정으로 주입해 repack/gc 효율과 전송/서빙 성능을 끌어올린다:

- core.bigFileThreshold : 이 이상 blob 은 delta 시도 안 함(시간/메모리 절감).
- pack.packSizeLimit    : 단일 packfile 상한(전송/호스팅 제약 + 재개 분할).
- pack.window/depth     : delta 압축 탐색 폭/체인 깊이.
- pack.threads          : gc/repack 병렬(0=코어 수 자동).
- pack.windowMemory     : 스레드당 윈도우 메모리 상한(대형 gc OOM 방지).
- pack.writeBitmaps(+hashCache) : bitmap 인덱스로 clone/fetch 서빙 가속.
- core.commitGraph / gc.writeCommitGraph / fetch.writeCommitGraph.
- index.version=4       : 많은 파일에서 인덱스 작고 빠름.

순수 빌더(build_pack_config/render_gitconfig)는 git 없이 단위 테스트한다.
적용(apply_to_repo/ensure_repo_tuned)은 `git config` 로 repo 에 기록한다.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("p4gitsync.pack_tuning")


def build_pack_config(cfg) -> list[tuple[str, str]]:
    """PackTuningConfig → 적용할 (git config key, value) 목록(순서 보존).

    enabled=False 면 빈 목록. index.version=0 또는 음수면 인덱스 항목 생략.
    """
    if not getattr(cfg, "enabled", True):
        return []
    settings: list[tuple[str, str]] = [
        ("core.bigFileThreshold", str(cfg.big_file_threshold)),
        ("pack.packSizeLimit", str(cfg.pack_size_limit)),
        ("pack.window", str(cfg.window)),
        ("pack.depth", str(cfg.depth)),
        ("pack.threads", str(cfg.threads)),
        ("pack.windowMemory", str(cfg.window_memory)),
    ]
    if cfg.write_bitmaps:
        settings.append(("pack.writeBitmaps", "true"))
    if cfg.write_bitmap_hash_cache:
        settings.append(("pack.writeBitmapHashCache", "true"))
    if cfg.commit_graph:
        settings += [
            ("core.commitGraph", "true"),
            ("gc.writeCommitGraph", "true"),
            ("fetch.writeCommitGraph", "true"),
        ]
    if cfg.index_version and cfg.index_version > 0:
        settings.append(("index.version", str(cfg.index_version)))
    if getattr(cfg, "serve_partial_clone", False):
        # 클라이언트 blobless/partial clone(--filter)을 서버가 허용
        settings += [
            ("uploadpack.allowFilter", "true"),
            ("uploadpack.allowAnySHA1InWant", "true"),
        ]
    return settings


def render_gitconfig(settings: list[tuple[str, str]]) -> str:
    """(key, value) 목록을 git config 파일 형식([section] + key=value)으로 렌더."""
    if not settings:
        return ""
    by_section: dict[str, list[tuple[str, str]]] = {}
    order: list[str] = []
    for key, value in settings:
        section, _, name = key.partition(".")
        if section not in by_section:
            by_section[section] = []
            order.append(section)
        by_section[section].append((name, value))
    lines: list[str] = []
    for section in order:
        lines.append(f"[{section}]")
        for name, value in by_section[section]:
            lines.append(f"\t{name} = {value}")
    return "\n".join(lines) + "\n"


def _is_git_repo(repo_path: str) -> bool:
    p = Path(repo_path)
    return (p / ".git").exists() or (p / "HEAD").exists()


def apply_to_repo(repo_path: str, settings: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """settings 를 `git config` 로 repo 에 기록. 적용된 항목 목록 반환.

    개별 실패는 경고 후 건너뛴다(전체 import 를 막지 않는다).
    """
    applied: list[tuple[str, str]] = []
    for key, value in settings:
        result = subprocess.run(
            ["git", "config", key, value],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            applied.append((key, value))
        else:
            logger.warning(
                "git config 적용 실패(스킵): %s=%s — %s",
                key, value, result.stderr.strip(),
            )
    return applied


def ensure_repo_tuned(repo_path: str, cfg, bare: bool = False) -> list[tuple[str, str]]:
    """repo 가 없으면 init 후, pack 튜닝을 적용. 적용된 항목 목록 반환.

    import 진입점에서 호출하면 이후 checkpoint repack·최종 gc 가 튜닝을 적용받는다.
    cfg.enabled=False 면 아무것도 하지 않는다.
    """
    settings = build_pack_config(cfg)
    if not settings:
        return []

    if not _is_git_repo(repo_path):
        os.makedirs(repo_path, exist_ok=True)
        init_args = ["git", "init", "--bare"] if bare else ["git", "init"]
        result = subprocess.run(init_args, cwd=repo_path, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning("git init 실패 — pack 튜닝 생략: %s", result.stderr.strip())
            return []
        logger.info("git repo 초기화: %s (bare=%s)", repo_path, bare)

    applied = apply_to_repo(repo_path, settings)
    if applied:
        logger.info("대용량 pack 튜닝 적용: %d개 설정 (%s)", len(applied), repo_path)
    return applied
