"""동기화 서비스 상태 조회 (status 명령)."""

from __future__ import annotations

import sqlite3
import sys
import unicodedata
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

from p4gitsync.cli.service_manager import create_service_manager
from p4gitsync.cli.service_registry import ServiceRegistry


# ---------------------------------------------------------------------------
# 유틸 함수
# ---------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """초 단위 값을 사람이 읽기 쉬운 문자열로 변환한다.

    - 60 미만: "N초"
    - 60~3600: "N분 N초"
    - 3600~86400: "N시간 N분"
    - 86400 이상: "N일 N시간"
    """
    total = int(seconds)
    if total < 60:
        return f"{total}초"
    if total < 3600:
        m, s = divmod(total, 60)
        return f"{m}분 {s}초"
    if total < 86400:
        h, remainder = divmod(total, 3600)
        m = remainder // 60
        return f"{h}시간 {m}분"
    d, remainder = divmod(total, 86400)
    h = remainder // 3600
    return f"{d}일 {h}시간"


def _display_width(text: str) -> int:
    """문자열의 터미널 표시 너비를 계산한다 (한글·전각 문자 2칸)."""
    width = 0
    for ch in text:
        eaw = unicodedata.east_asian_width(ch)
        width += 2 if eaw in ("W", "F") else 1
    return width


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """헤더와 행 데이터로 텍스트 테이블을 생성한다."""
    ncols = len(headers)
    # 각 컬럼의 최대 표시 너비 계산
    col_widths = [_display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row[:ncols]):
            col_widths[i] = max(col_widths[i], _display_width(cell))

    def _pad(text: str, width: int) -> str:
        pad = width - _display_width(text)
        return text + " " * max(pad, 0)

    lines: list[str] = []
    # 헤더
    line = "  ".join(_pad(h, col_widths[i]) for i, h in enumerate(headers))
    lines.append(line)
    # 구분선
    sep = "  ".join("-" * w for w in col_widths)
    lines.append(sep)
    # 데이터
    for row in rows:
        cells = [row[i] if i < len(row) else "" for i in range(ncols)]
        line = "  ".join(_pad(c, col_widths[i]) for i, c in enumerate(cells))
        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# config.toml 파싱 헬퍼
# ---------------------------------------------------------------------------


def _load_config(config_path: str) -> dict:
    """TOML 설정 파일을 읽어서 dict로 반환한다."""
    p = Path(config_path)
    if not p.exists():
        return {}
    with p.open("rb") as f:
        return tomllib.load(f)


def _get_last_cl(config: dict, stream: str) -> str:
    """state.db에서 해당 stream의 last_cl을 조회한다."""
    state_cfg = config.get("state", {})
    db_path = state_cfg.get("db_path", "")
    if not db_path:
        # 기본 경로: config의 git.repo_path 기준
        repo_path = config.get("git", {}).get("repo_path", "")
        if repo_path:
            db_path = str(Path(repo_path).parent / "state.db")
    if not db_path or not Path(db_path).exists():
        return "-"
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT last_cl FROM sync_state WHERE stream = ?", (stream,)
        )
        row = cur.fetchone()
        conn.close()
        return str(row[0]) if row else "-"
    except Exception:
        return "-"


# ---------------------------------------------------------------------------
# 출력 함수
# ---------------------------------------------------------------------------


def _show_summary(registry: ServiceRegistry) -> None:
    """등록된 모든 서비스의 요약 테이블을 출력한다."""
    services = registry.list_all()
    if not services:
        print("등록된 동기화 서비스가 없습니다.")
        return

    mgr = create_service_manager(registry)

    headers = ["이름", "상태", "Stream", "Last CL"]
    rows: list[list[str]] = []

    for name, info in services.items():
        running = mgr.is_running(name)
        status = "\u25cf 실행중" if running else "\u25cb 중지"

        config = _load_config(info.get("config", ""))
        stream = config.get("p4", {}).get("stream", "-")
        last_cl = _get_last_cl(config, stream)

        rows.append([name, status, stream, last_cl])

    print("등록된 동기화 서비스:")
    print(format_table(headers, rows))


def _show_detail(name: str, registry: ServiceRegistry) -> None:
    """단일 서비스의 상세 정보를 출력한다."""
    info = registry.get(name)
    if info is None:
        print(f"서비스를 찾을 수 없습니다: {name}")
        return

    mgr = create_service_manager(registry)
    running = mgr.is_running(name)
    pid = mgr.get_pid(name) if running else None

    status = "\u25cf 실행중"
    if running and pid:
        status += f" (PID {pid})"
    elif not running:
        status = "\u25cb 중지"

    config_path = info.get("config", "-")
    config = _load_config(config_path)

    p4_cfg = config.get("p4", {})
    git_cfg = config.get("git", {})
    sync_cfg = config.get("sync", {})
    lfs_cfg = config.get("lfs", {})

    stream = p4_cfg.get("stream", "-")
    direction = sync_cfg.get("direction", "p4_to_git")
    repo_path = git_cfg.get("repo_path", "-")
    last_cl = _get_last_cl(config, stream)

    # LFS 정보
    lfs_enabled = lfs_cfg.get("enabled", False)
    if lfs_enabled:
        extensions = lfs_cfg.get("extensions", [])
        lfs_text = f"활성화 ({len(extensions)} 확장자)" if extensions else "활성화"
    else:
        lfs_text = "비활성화"

    print(f"서비스: {name}")
    print(f"  상태:        {status}")
    print(f"  Config:      {config_path}")
    print(f"  Stream:      {stream}")
    print(f"  방향:        {direction}")
    print(f"  Git repo:    {repo_path}")
    print(f"  Last CL:     {last_cl}")
    print(f"  LFS:         {lfs_text}")


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def show_status(name: str | None = None) -> None:
    """동기화 서비스 상태를 출력한다.

    name이 None이면 요약 테이블, 지정하면 상세 정보를 출력한다.
    """
    registry = ServiceRegistry()
    if name is None:
        _show_summary(registry)
    else:
        _show_detail(name, registry)
