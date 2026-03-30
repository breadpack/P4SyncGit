"""대화형 setup wizard — config.toml 생성/수정."""

from __future__ import annotations

import getpass
import os
import sys
import tomllib
from pathlib import Path
from typing import Any


def _input_with_default(prompt: str, default: str = "") -> str:
    """기본값을 표시하며 사용자 입력을 받는다. 빈 입력 시 기본값 반환."""
    if default:
        raw = input(f"{prompt} [{default}]: ").strip()
        return raw or default
    return input(f"{prompt}: ").strip()


def _input_bool(prompt: str, default: bool = True) -> bool:
    """Y/n 또는 y/N 형태로 boolean 입력을 받는다."""
    hint = "Y/n" if default else "y/N"
    raw = input(f"{prompt} ({hint}): ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _input_int(prompt: str, default: int) -> int:
    """정수 입력. 빈 입력 시 기본값 반환."""
    raw = input(f"{prompt} [{default}]: ").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"  잘못된 숫자입니다. 기본값({default})을 사용합니다.")
        return default


def _input_choice(prompt: str, choices: list[str], default: str) -> str:
    """선택지 중 하나를 입력받는다."""
    choices_str = "/".join(choices)
    raw = input(f"{prompt} ({choices_str}) [{default}]: ").strip()
    if not raw:
        return default
    if raw in choices:
        return raw
    print(f"  잘못된 선택입니다. 기본값({default})을 사용합니다.")
    return default


# ── 섹션별 입력 ──────────────────────────────────────────────────


def _setup_p4(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """P4 서버 설정 입력 및 연결 테스트."""
    ex = existing or {}
    print("\n── P4 서버 설정 ──")

    port = _input_with_default(
        "P4 서버 주소 (예: ssl:perforce:1666)", ex.get("port", "")
    )
    user = _input_with_default("P4 사용자", ex.get("user", ""))

    # password는 기존 값 표시 없이 항상 새로 입력
    password = getpass.getpass("P4 비밀번호 (입력 없으면 기존 유지): ")
    if not password and ex.get("password"):
        password = ex["password"]

    stream = _input_with_default(
        "P4 Stream 경로 (예: //depot/main)", ex.get("stream", "")
    )

    # 연결 테스트
    print("\n  P4 연결 테스트 중...")
    try:
        from p4gitsync.config.sync_config import P4Config

        test_cfg = P4Config(port=port, user=user, password=password, stream=stream)
        client = test_cfg.create_client()
        client.connect()
        parent, excludes = client.resolve_virtual_stream(stream)
        client.disconnect()

        if parent != stream:
            print(f"  virtual stream 감지: parent={parent}, excludes={len(excludes)}개")
        print("  P4 연결 성공!")
    except Exception as e:
        print(f"  P4 연결 실패: {e}")
        if not _input_bool(
            "  연결 실패했지만 설정을 계속 저장하시겠습니까?", default=False
        ):
            print("  설정을 중단합니다.")
            sys.exit(1)

    result: dict[str, Any] = {"port": port, "user": user, "stream": stream}
    if password:
        result["password"] = password

    # 기존 설정에서 나머지 필드 유지
    for key in (
        "workspace",
        "filelog_batch_size",
        "submit_workspace",
        "submit_as_user",
    ):
        if key in ex:
            result[key] = ex[key]

    return result


def _setup_git(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Git 설정 입력."""
    ex = existing or {}
    print("\n── Git 설정 ──")

    repo_path = _input_with_default("Git 저장소 경로", ex.get("repo_path", ""))
    bare = _input_bool("Bare 저장소 사용", ex.get("bare", True))
    default_branch = _input_with_default(
        "기본 브랜치", ex.get("default_branch", "main")
    )
    remote_url = _input_with_default(
        "Remote URL (없으면 비워두기)", ex.get("remote_url", "")
    )

    result: dict[str, Any] = {
        "repo_path": repo_path,
        "bare": bare,
        "default_branch": default_branch,
    }
    if remote_url:
        result["remote_url"] = remote_url

    # 기존 설정에서 나머지 필드 유지
    for key in ("backend", "watch_remote", "reverse_sync_interval_seconds"):
        if key in ex:
            result[key] = ex[key]

    return result


def _setup_sync(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """동기화 설정 입력."""
    ex = existing or {}
    print("\n── 동기화 설정 ──")

    direction = _input_choice(
        "동기화 방향",
        ["p4_to_git", "bidirectional"],
        ex.get("_direction", "p4_to_git"),
    )
    polling_interval = _input_int(
        "폴링 간격 (초)",
        ex.get("polling_interval_seconds", 30),
    )

    result: dict[str, Any] = {"polling_interval_seconds": polling_interval}

    # 기존 설정에서 나머지 필드 유지
    for key in (
        "batch_size",
        "push_after_every_commit",
        "file_extraction_mode",
        "print_to_sync_threshold",
        "git_gc_interval",
        "error_retry_threshold",
        "push_batch_size",
        "push_interval_seconds",
    ):
        if key in ex:
            result[key] = ex[key]

    # stream_policy
    print("\n  Stream 정책:")
    auto_discover = _input_bool("  Stream 자동 감지", True)
    stream_policy: dict[str, Any] = {"auto_discover": auto_discover}

    if auto_discover:
        include_raw = _input_with_default("  포함 패턴 (쉼표 구분, 비우면 전체)", "")
        if include_raw:
            stream_policy["include_patterns"] = [
                p.strip() for p in include_raw.split(",") if p.strip()
            ]
        exclude_raw = _input_with_default("  제외 Stream (쉼표 구분, 비우면 없음)", "")
        if exclude_raw:
            stream_policy["exclude_streams"] = [
                p.strip() for p in exclude_raw.split(",") if p.strip()
            ]

    # direction을 sync_directions로 변환
    if direction == "bidirectional":
        stream_policy["sync_directions"] = [{"direction": "bidirectional"}]

    return result, stream_policy, direction  # type: ignore[return-value]


def _detect_binary_extensions(p4_config: dict[str, Any]) -> list[str]:
    """P4 typemap + 최근 CL에서 binary 확장자를 자동 감지."""
    extensions: set[str] = set()

    try:
        from p4gitsync.config.sync_config import P4Config

        cfg = P4Config(
            port=p4_config.get("port", ""),
            user=p4_config.get("user", ""),
            password=p4_config.get("password", ""),
            stream=p4_config.get("stream", ""),
        )
        client = cfg.create_client()
        client.connect()

        # 1) typemap에서 binary 타입 확장자 수집
        try:
            typemap = client._p4.run_typemap("-o")
            if typemap:
                mapping = typemap[0].get("TypeMap", [])
                # TypeMap은 ["binary //....ext", ...] 형태
                for i in range(0, len(mapping), 2):
                    type_name = mapping[i] if i < len(mapping) else ""
                    pattern = mapping[i + 1] if i + 1 < len(mapping) else ""
                    if "binary" in type_name.lower():
                        # 패턴에서 확장자 추출: "//....ext" -> ".ext"
                        if "." in pattern:
                            ext = os.path.splitext(pattern)[1]
                            if ext:
                                extensions.add(ext.lower())
        except Exception:
            pass  # typemap이 없을 수 있음

        # 2) 최근 100 CL에서 binary 파일 확장자 수집
        try:
            stream = p4_config.get("stream", "")
            if stream:
                changes = client._p4.run_changes(
                    "-s", "submitted", "-m", "100", f"{stream}/..."
                )
                if changes:
                    cl_nums = [c["change"] for c in changes]
                    # 한 번에 describe
                    descs = client._p4.run_describe("-s", *cl_nums)
                    for desc in descs:
                        types = desc.get("type", [])
                        files = desc.get("depotFile", [])
                        for j, ft in enumerate(types):
                            if "binary" in ft.lower():
                                if j < len(files):
                                    ext = os.path.splitext(files[j])[1]
                                    if ext:
                                        extensions.add(ext.lower())
        except Exception:
            pass

        client.disconnect()
    except Exception as e:
        print(f"  binary 확장자 감지 실패: {e}")

    return sorted(extensions)


def _setup_lfs(
    existing: dict[str, Any] | None = None,
    p4_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """LFS 설정 입력."""
    ex = existing or {}
    print("\n── LFS 설정 ──")

    enabled = _input_bool("LFS 사용", ex.get("enabled", False))
    if not enabled:
        return {"enabled": False}

    # binary 확장자 자동 감지
    detected: list[str] = []
    if p4_config:
        print("\n  P4에서 binary 확장자를 감지하는 중...")
        detected = _detect_binary_extensions(p4_config)
        if detected:
            print(f"  감지된 확장자: {', '.join(detected)}")
        else:
            print("  감지된 확장자 없음 (기본값 사용)")

    # 기존 확장자 또는 기본값
    current_exts = ex.get("extensions", [])
    if not current_exts:
        from p4gitsync.config.lfs_config import LfsConfig

        current_exts = LfsConfig().extensions

    # 감지된 확장자를 기존 목록에 병합
    merged = sorted(set(current_exts) | set(detected))
    print(f"\n  현재 LFS 확장자 목록: {', '.join(merged)}")

    add_raw = _input_with_default("  추가할 확장자 (쉼표 구분, 비우면 스킵)", "")
    if add_raw:
        for ext in add_raw.split(","):
            ext = ext.strip()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if ext:
                merged.append(ext.lower())
        merged = sorted(set(merged))

    remove_raw = _input_with_default("  제외할 확장자 (쉼표 구분, 비우면 스킵)", "")
    if remove_raw:
        to_remove = set()
        for ext in remove_raw.split(","):
            ext = ext.strip()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if ext:
                to_remove.add(ext.lower())
        merged = sorted(e for e in merged if e not in to_remove)

    result: dict[str, Any] = {
        "enabled": True,
        "extensions": merged,
    }

    # 기존 설정에서 나머지 필드 유지
    for key in (
        "size_threshold_bytes",
        "lockable_extensions",
        "server_type",
        "server_url",
        "auth_type",
    ):
        if key in ex:
            result[key] = ex[key]

    return result


def _setup_state(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """State DB 설정 입력."""
    ex = existing or {}
    print("\n── State DB 설정 ──")

    db_path = _input_with_default("State DB 경로", ex.get("db_path", "state.db"))
    return {"db_path": db_path}


# ── 메인 흐름 ──────────────────────────────────────────────────


def _save_config(config: dict[str, Any], config_path: str) -> None:
    """config dict를 TOML 파일로 저장."""
    import tomli_w

    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "wb") as f:
        tomli_w.dump(config, f)

    print(f"\n설정 파일 저장 완료: {path.resolve()}")


def _new_setup(config_path: str) -> None:
    """신규 모드: config 파일이 없을 때 전체 설정을 순차 입력."""
    print("=" * 50)
    print("P4GitSync 초기 설정 마법사")
    print("=" * 50)

    # 1. P4
    p4 = _setup_p4()

    # 2. Git
    git = _setup_git()

    # 3. Sync + stream_policy
    sync, stream_policy, direction = _setup_sync()

    # 4. LFS
    lfs = _setup_lfs(p4_config=p4)

    # 5. State
    state = _setup_state()

    config: dict[str, Any] = {
        "p4": p4,
        "git": git,
        "sync": sync,
        "state": state,
        "stream_policy": stream_policy,
    }
    if lfs.get("enabled"):
        config["lfs"] = lfs

    _save_config(config, config_path)


def _edit_setup(config_path: str) -> None:
    """수정 모드: 기존 config 파일에서 섹션 선택하여 수정."""
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    print("=" * 50)
    print("P4GitSync 설정 수정")
    print("=" * 50)

    while True:
        print("\n수정할 섹션을 선택하세요:")
        print("  1. P4 서버 설정")
        print("  2. Git 설정")
        print("  3. 동기화 설정")
        print("  4. LFS 설정")
        print("  5. State DB 설정")
        print("  0. 저장 후 완료")

        choice = input("\n선택 (0-5): ").strip()

        if choice == "0":
            break
        elif choice == "1":
            config["p4"] = _setup_p4(config.get("p4"))
        elif choice == "2":
            config["git"] = _setup_git(config.get("git"))
        elif choice == "3":
            sync, stream_policy, direction = _setup_sync(config.get("sync"))
            config["sync"] = sync
            config["stream_policy"] = stream_policy
        elif choice == "4":
            config["lfs"] = _setup_lfs(config.get("lfs"), config.get("p4"))
        elif choice == "5":
            config["state"] = _setup_state(config.get("state"))
        else:
            print("  잘못된 선택입니다.")

    _save_config(config, config_path)


def run_setup(config_path: str = "config.toml") -> None:
    """Setup wizard 진입점.

    - config 파일이 없으면 신규 모드
    - config 파일이 있으면 수정 모드
    """
    if Path(config_path).exists():
        _edit_setup(config_path)
    else:
        _new_setup(config_path)
