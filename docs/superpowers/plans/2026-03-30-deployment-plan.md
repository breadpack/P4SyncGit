# P4GitSync 배포 환경 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `p4gitsync setup`, `service install/start/stop/uninstall`, `status` CLI 명령을 구현하여 Windows(NSSM)/Linux(systemd)에서 서비스로 배포 가능하게 한다.

**Architecture:** `cli/` 패키지에 setup_wizard, service_manager, status_reporter 3개 모듈을 추가하고, `__main__.py`에 서브커맨드를 등록한다. 서비스 레지스트리는 `~/.p4gitsync/services.json`으로 관리한다.

**Tech Stack:** Python 3.12+, tomli_w (TOML 쓰기), NSSM (Windows), systemd (Linux)

---

## 파일 구조

### 신규 파일

| 파일 | 책임 |
|------|------|
| `p4gitsync/src/p4gitsync/cli/__init__.py` | 패키지 초기화 |
| `p4gitsync/src/p4gitsync/cli/setup_wizard.py` | 대화형 config.toml 생성/수정 |
| `p4gitsync/src/p4gitsync/cli/service_registry.py` | services.json CRUD |
| `p4gitsync/src/p4gitsync/cli/service_manager.py` | 플랫폼별 서비스 관리 (추상 + Windows/Linux) |
| `p4gitsync/src/p4gitsync/cli/status_reporter.py` | 동기화 상태 조회/출력 |
| `p4gitsync/tests/test_service_registry.py` | 레지스트리 테스트 |
| `p4gitsync/tests/test_setup_wizard.py` | wizard 테스트 |
| `p4gitsync/tests/test_status_reporter.py` | status 테스트 |

### 수정 파일

| 파일 | 변경 |
|------|------|
| `p4gitsync/src/p4gitsync/__main__.py` | setup, service, status 서브커맨드 추가 |
| `p4gitsync/pyproject.toml` | tomli_w 의존성 추가 |

---

### Task 1: 서비스 레지스트리

**Files:**
- Create: `p4gitsync/src/p4gitsync/cli/__init__.py`
- Create: `p4gitsync/src/p4gitsync/cli/service_registry.py`
- Test: `p4gitsync/tests/test_service_registry.py`

- [ ] **Step 1: 테스트 작성**

```python
# p4gitsync/tests/test_service_registry.py
import json
from pathlib import Path
from p4gitsync.cli.service_registry import ServiceRegistry


class TestServiceRegistry:
    def test_add_and_get(self, tmp_path: Path):
        reg = ServiceRegistry(tmp_path / "services.json")
        reg.add("p4gitsync-dev", config="/opt/config.toml", platform="linux")
        entry = reg.get("p4gitsync-dev")
        assert entry is not None
        assert entry["config"] == "/opt/config.toml"
        assert entry["platform"] == "linux"

    def test_list_all(self, tmp_path: Path):
        reg = ServiceRegistry(tmp_path / "services.json")
        reg.add("svc-a", config="a.toml", platform="windows")
        reg.add("svc-b", config="b.toml", platform="linux")
        all_svcs = reg.list_all()
        assert len(all_svcs) == 2
        assert "svc-a" in all_svcs
        assert "svc-b" in all_svcs

    def test_remove(self, tmp_path: Path):
        reg = ServiceRegistry(tmp_path / "services.json")
        reg.add("svc-a", config="a.toml", platform="windows")
        reg.remove("svc-a")
        assert reg.get("svc-a") is None

    def test_persistence(self, tmp_path: Path):
        path = tmp_path / "services.json"
        reg1 = ServiceRegistry(path)
        reg1.add("svc-x", config="x.toml", platform="linux")
        reg2 = ServiceRegistry(path)
        assert reg2.get("svc-x") is not None

    def test_get_nonexistent(self, tmp_path: Path):
        reg = ServiceRegistry(tmp_path / "services.json")
        assert reg.get("nope") is None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd p4gitsync && pytest tests/test_service_registry.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: 구현**

```python
# p4gitsync/src/p4gitsync/cli/__init__.py
```

```python
# p4gitsync/src/p4gitsync/cli/service_registry.py
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _default_registry_path() -> Path:
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Local" / "p4gitsync"
    else:
        base = Path.home() / ".p4gitsync"
    return base / "services.json"


class ServiceRegistry:
    """서비스 등록 정보를 JSON 파일로 관리."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_registry_path()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def add(self, name: str, config: str, platform: str) -> None:
        self._data[name] = {
            "config": config,
            "name": name,
            "platform": platform,
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get(self, name: str) -> dict | None:
        return self._data.get(name)

    def remove(self, name: str) -> None:
        self._data.pop(name, None)
        self._save()

    def list_all(self) -> dict[str, dict]:
        return dict(self._data)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/test_service_registry.py -v`
Expected: 5 passed

- [ ] **Step 5: 커밋**

```bash
git add p4gitsync/src/p4gitsync/cli/ p4gitsync/tests/test_service_registry.py
git commit -m "feat: 서비스 레지스트리 (services.json CRUD)"
```

---

### Task 2: 서비스 매니저 — 추상 + 플랫폼 구현

**Files:**
- Create: `p4gitsync/src/p4gitsync/cli/service_manager.py`

- [ ] **Step 1: 구현**

```python
# p4gitsync/src/p4gitsync/cli/service_manager.py
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from p4gitsync.cli.service_registry import ServiceRegistry, _default_registry_path

logger = logging.getLogger("p4gitsync.service")


class ServiceManager(ABC):
    """서비스 관리 추상 클래스."""

    def __init__(self, registry: ServiceRegistry | None = None) -> None:
        self._registry = registry or ServiceRegistry()

    @abstractmethod
    def install(self, name: str, exe_path: str, config_path: str) -> None: ...

    @abstractmethod
    def uninstall(self, name: str) -> None: ...

    @abstractmethod
    def start(self, name: str) -> None: ...

    @abstractmethod
    def stop(self, name: str) -> None: ...

    @abstractmethod
    def is_running(self, name: str) -> bool: ...

    @abstractmethod
    def get_pid(self, name: str) -> int | None: ...


class WindowsServiceManager(ServiceManager):
    """NSSM 기반 Windows 서비스 관리."""

    def _nssm_path(self) -> Path:
        p = _default_registry_path().parent / "nssm.exe"
        if not p.exists():
            self._download_nssm(p)
        return p

    def _download_nssm(self, dest: Path) -> None:
        url = "https://nssm.cc/release/nssm-2.24.zip"
        logger.info("NSSM 다운로드 중: %s", url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        import io, zipfile
        with urllib.request.urlopen(url) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith("win64/nssm.exe"):
                    dest.write_bytes(zf.read(name))
                    logger.info("NSSM 설치 완료: %s", dest)
                    return
        raise RuntimeError("NSSM exe를 zip에서 찾을 수 없음")

    def _run_nssm(self, *args: str) -> subprocess.CompletedProcess:
        nssm = str(self._nssm_path())
        return subprocess.run([nssm, *args], capture_output=True, text=True)

    def install(self, name: str, exe_path: str, config_path: str) -> None:
        config_abs = str(Path(config_path).resolve())
        log_dir = Path(config_abs).parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        self._run_nssm("install", name, exe_path, "--config", config_abs, "run")
        self._run_nssm("set", name, "AppStdout", str(log_dir / "output.log"))
        self._run_nssm("set", name, "AppStderr", str(log_dir / "error.log"))
        self._run_nssm("set", name, "AppRotateFiles", "1")
        self._run_nssm("set", name, "AppRotateSeconds", "86400")
        self._run_nssm("set", name, "Start", "SERVICE_AUTO_START")
        self._run_nssm("set", name, "AppRestartDelay", "10000")

        self._registry.add(name, config=config_abs, platform="windows")
        logger.info("Windows 서비스 등록 완료: %s", name)

    def uninstall(self, name: str) -> None:
        self._run_nssm("stop", name)
        self._run_nssm("remove", name, "confirm")
        self._registry.remove(name)
        logger.info("Windows 서비스 제거 완료: %s", name)

    def start(self, name: str) -> None:
        result = self._run_nssm("start", name)
        if result.returncode != 0:
            raise RuntimeError(f"서비스 시작 실패: {result.stderr}")
        logger.info("서비스 시작: %s", name)

    def stop(self, name: str) -> None:
        self._run_nssm("stop", name)
        logger.info("서비스 중지: %s", name)

    def is_running(self, name: str) -> bool:
        result = self._run_nssm("status", name)
        return "SERVICE_RUNNING" in result.stdout

    def get_pid(self, name: str) -> int | None:
        result = self._run_nssm("queryservice", name)
        for line in result.stdout.splitlines():
            if "PID" in line:
                parts = line.split(":")
                if len(parts) >= 2:
                    try:
                        return int(parts[1].strip())
                    except ValueError:
                        pass
        return None


class LinuxServiceManager(ServiceManager):
    """systemd 기반 Linux 서비스 관리."""

    _UNIT_TEMPLATE = """\
[Unit]
Description=P4GitSync - {name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exe_path} --config {config_path} run
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier={name}

[Install]
WantedBy=multi-user.target
"""

    def _unit_path(self, name: str) -> Path:
        return Path(f"/etc/systemd/system/{name}.service")

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True)

    def install(self, name: str, exe_path: str, config_path: str) -> None:
        config_abs = str(Path(config_path).resolve())
        unit_content = self._UNIT_TEMPLATE.format(
            name=name,
            exe_path=exe_path,
            config_path=config_abs,
        )
        unit_path = self._unit_path(name)
        unit_path.write_text(unit_content)
        self._systemctl("daemon-reload")
        self._systemctl("enable", name)

        self._registry.add(name, config=config_abs, platform="linux")
        logger.info("systemd 서비스 등록 완료: %s", name)

    def uninstall(self, name: str) -> None:
        self._systemctl("stop", name)
        self._systemctl("disable", name)
        unit_path = self._unit_path(name)
        if unit_path.exists():
            unit_path.unlink()
        self._systemctl("daemon-reload")
        self._registry.remove(name)
        logger.info("systemd 서비스 제거 완료: %s", name)

    def start(self, name: str) -> None:
        result = self._systemctl("start", name)
        if result.returncode != 0:
            raise RuntimeError(f"서비스 시작 실패: {result.stderr}")
        logger.info("서비스 시작: %s", name)

    def stop(self, name: str) -> None:
        self._systemctl("stop", name)
        logger.info("서비스 중지: %s", name)

    def is_running(self, name: str) -> bool:
        result = self._systemctl("is-active", name)
        return result.stdout.strip() == "active"

    def get_pid(self, name: str) -> int | None:
        result = self._systemctl("show", name, "--property=MainPID", "--value")
        try:
            pid = int(result.stdout.strip())
            return pid if pid > 0 else None
        except ValueError:
            return None


def create_service_manager(registry: ServiceRegistry | None = None) -> ServiceManager:
    """플랫폼에 맞는 ServiceManager 생성."""
    if sys.platform == "win32":
        return WindowsServiceManager(registry)
    return LinuxServiceManager(registry)
```

- [ ] **Step 2: 커밋**

```bash
git add p4gitsync/src/p4gitsync/cli/service_manager.py
git commit -m "feat: 서비스 매니저 (Windows NSSM + Linux systemd)"
```

---

### Task 3: Setup Wizard

**Files:**
- Create: `p4gitsync/src/p4gitsync/cli/setup_wizard.py`
- Modify: `p4gitsync/pyproject.toml`

- [ ] **Step 1: tomli_w 의존성 추가**

`p4gitsync/pyproject.toml`의 dependencies에 추가:
```toml
dependencies = [
    "p4python>=2024.1",
    "pygit2>=1.14",
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "slack-sdk>=3.27",
    "redis>=5.0",
    "tomli_w>=1.0",
]
```

Run: `cd p4gitsync && pip install -e ".[dev]"`

- [ ] **Step 2: 구현**

```python
# p4gitsync/src/p4gitsync/cli/setup_wizard.py
from __future__ import annotations

import getpass
import logging
import sys
import tomllib
from collections import OrderedDict
from pathlib import Path

import tomli_w

logger = logging.getLogger("p4gitsync.setup")


def run_setup(config_path: str) -> None:
    """대화형 config.toml 생성/수정."""
    path = Path(config_path)
    if path.exists():
        _edit_mode(path)
    else:
        _create_mode(path)


def _create_mode(path: Path) -> None:
    """신규 config.toml 생성."""
    print(f"\n새 설정 파일을 생성합니다: {path}\n")
    config: dict = {}

    # [1/5] P4 서버
    print("[1/5] P4 서버 설정")
    config["p4"] = _ask_p4_settings()

    # [2/5] Git
    print("\n[2/5] Git 저장소 설정")
    config["git"] = _ask_git_settings()

    # [3/5] 동기화
    print("\n[3/5] 동기화 설정")
    config["sync"], config["stream_policy"] = _ask_sync_settings(config["p4"]["stream"])

    # [4/5] LFS
    print("\n[4/5] LFS 설정")
    config["lfs"] = _ask_lfs_settings(config["p4"])

    # 기본 섹션
    config.setdefault("state", {"db_path": ""})
    config.setdefault("logging", {"level": "INFO", "format": "text"})
    config.setdefault("redis", {"enabled": False})
    config.setdefault("api", {"enabled": False})

    # [5/5] 저장
    print(f"\n[5/5] 설정 저장")
    _save_config(path, config)
    print(f"  {path} 저장 완료")


def _edit_mode(path: Path) -> None:
    """기존 config.toml 수정."""
    with open(path, "rb") as f:
        config = tomllib.load(f)

    print(f"\n기존 설정 파일 감지: {path}")
    while True:
        print("\n변경할 섹션을 선택하세요:")
        print("  1. P4 서버 설정")
        print("  2. Git 저장소 설정")
        print("  3. 동기화 방향/정책")
        print("  4. LFS 설정")
        print("  5. API/알림 설정")
        print("  0. 완료 (저장)")
        choice = input("> ").strip()
        if choice == "0":
            break
        elif choice == "1":
            config["p4"] = _ask_p4_settings(config.get("p4", {}))
        elif choice == "2":
            config["git"] = _ask_git_settings(config.get("git", {}))
        elif choice == "3":
            stream = config.get("p4", {}).get("stream", "")
            config["sync"], config["stream_policy"] = _ask_sync_settings(
                stream, config.get("sync", {}),
            )
        elif choice == "4":
            config["lfs"] = _ask_lfs_settings(config.get("p4", {}), config.get("lfs", {}))
        elif choice == "5":
            config["api"] = _ask_api_settings(config.get("api", {}))

    _save_config(path, config)
    print(f"\n  {path} 저장 완료")


def _ask_p4_settings(defaults: dict | None = None) -> dict:
    d = defaults or {}
    port = input(f"  P4 서버 주소 ({d.get('port', 'ssl:p4server:1666')}): ").strip()
    user = input(f"  P4 계정 ({d.get('user', '')}): ").strip()
    password = getpass.getpass(f"  P4 비밀번호: ")
    stream = input(f"  P4 Stream ({d.get('stream', '//depot/main')}): ").strip()

    result = {
        "port": port or d.get("port", ""),
        "user": user or d.get("user", ""),
        "password": password or d.get("password", ""),
        "stream": stream or d.get("stream", ""),
        "workspace": d.get("workspace", f"{user or d.get('user', 'sync')}_p4gitsync.read"),
        "submit_workspace": d.get("submit_workspace", f"{user or d.get('user', 'sync')}_p4gitsync.sync"),
    }

    # 연결 테스트
    print("\n  연결 테스트 중...", end=" ", flush=True)
    try:
        from p4gitsync.config.sync_config import P4Config
        p4cfg = P4Config(**{k: v for k, v in result.items() if k in P4Config.__dataclass_fields__})
        client = p4cfg.create_client()
        client.connect()
        parent, excludes = client.resolve_virtual_stream(result["stream"])
        if parent != result["stream"]:
            print(f"OK (virtual stream: parent={parent}, excludes={len(excludes)}개)")
        else:
            print("OK")
        client.disconnect()
    except Exception as e:
        print(f"실패: {e}")

    return result


def _ask_git_settings(defaults: dict | None = None) -> dict:
    d = defaults or {}
    repo_path = input(f"  Git repo 경로 ({d.get('repo_path', '')}): ").strip()
    bare_input = input(f"  Bare repo? (Y/n, 현재={d.get('bare', True)}): ").strip().lower()
    bare = bare_input != "n"
    branch = input(f"  기본 브랜치 ({d.get('default_branch', 'main')}): ").strip()
    return {
        "repo_path": repo_path or d.get("repo_path", ""),
        "bare": bare,
        "default_branch": branch or d.get("default_branch", "main"),
        "remote_url": d.get("remote_url", ""),
    }


def _ask_sync_settings(stream: str, defaults: dict | None = None) -> tuple[dict, dict]:
    d = defaults or {}
    direction = input(f"  동기화 방향 (p4_to_git/bidirectional, 현재={d.get('direction', 'p4_to_git')}): ").strip()
    interval = input(f"  폴링 간격 (초, 현재={d.get('polling_interval_seconds', 30)}): ").strip()
    sync = {
        "polling_interval_seconds": int(interval) if interval else d.get("polling_interval_seconds", 30),
        "batch_size": d.get("batch_size", 50),
    }
    direction = direction or "p4_to_git"
    policy = {
        "auto_discover": False,
        "include_patterns": [stream],
        "exclude_types": [],
        "sync_directions": [{"stream": stream, "branch": "main", "direction": direction}],
    }
    return sync, policy


def _ask_lfs_settings(p4_config: dict, defaults: dict | None = None) -> dict:
    d = defaults or {}
    enable_input = input(f"  LFS 활성화? (Y/n, 현재={d.get('enabled', False)}): ").strip().lower()
    enabled = enable_input != "n"
    if not enabled:
        return {"enabled": False}

    extensions = list(d.get("extensions", []))
    if not extensions:
        print("  P4 typemap에서 binary 확장자 자동 감지...", end=" ", flush=True)
        try:
            extensions = _detect_binary_extensions(p4_config)
            print(f"{len(extensions)}개 발견")
            print(f"    {' '.join(extensions)}")
        except Exception as e:
            print(f"실패: {e}")
            extensions = [".png", ".jpg", ".ogg", ".wav", ".dll", ".exe", ".bin"]

    add = input("  추가할 확장자 (쉼표 구분, 없으면 Enter): ").strip()
    if add:
        for ext in add.split(","):
            ext = ext.strip()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if ext and ext not in extensions:
                extensions.append(ext)

    remove = input("  제외할 확장자 (쉼표 구분, 없으면 Enter): ").strip()
    if remove:
        for ext in remove.split(","):
            ext = ext.strip()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if ext in extensions:
                extensions.remove(ext)

    return {"enabled": True, "extensions": extensions}


def _ask_api_settings(defaults: dict | None = None) -> dict:
    d = defaults or {}
    enable = input(f"  API 활성화? (y/N, 현재={d.get('enabled', False)}): ").strip().lower()
    if enable != "y":
        return {"enabled": False}
    port = input(f"  API 포트 ({d.get('port', 8080)}): ").strip()
    return {
        "enabled": True,
        "host": "0.0.0.0",
        "port": int(port) if port else d.get("port", 8080),
    }


def _detect_binary_extensions(p4_config: dict) -> list[str]:
    """P4 typemap + 최근 CL에서 binary 확장자를 감지."""
    from p4gitsync.config.sync_config import P4Config
    from collections import Counter
    import os

    p4cfg = P4Config(**{k: v for k, v in p4_config.items() if k in P4Config.__dataclass_fields__})
    client = p4cfg.create_client()
    client.connect()

    exts: set[str] = set()

    # typemap에서 binary 타입 추출
    try:
        results = client._p4.run_typemap("-o")
        if results:
            for entry in results[0].get("TypeMap", []):
                if "binary" in entry.lower():
                    parts = entry.strip().split()
                    if len(parts) >= 2:
                        pattern = parts[-1]
                        ext = os.path.splitext(pattern)[1]
                        if ext:
                            exts.add(ext.lower())
    except Exception:
        pass

    # 최근 CL에서 binary file_type 확장자 수집
    try:
        stream = p4_config.get("stream", "")
        parent, _ = client.resolve_virtual_stream(stream)
        cls = client.get_changes_after(parent, 0)[-100:]
        if cls:
            infos = client.describe_batch(cls)
            for info in infos:
                for fa in info.files:
                    if "binary" in fa.file_type:
                        ext = os.path.splitext(fa.depot_path)[1].lower()
                        if ext:
                            exts.add(ext)
    except Exception:
        pass

    client.disconnect()
    return sorted(exts)


def _save_config(path: Path, config: dict) -> None:
    """config dict를 TOML 파일로 저장."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(config, f)
```

- [ ] **Step 3: 커밋**

```bash
git add p4gitsync/src/p4gitsync/cli/setup_wizard.py p4gitsync/pyproject.toml
git commit -m "feat: 대화형 setup wizard (config.toml 생성/수정)"
```

---

### Task 4: Status Reporter

**Files:**
- Create: `p4gitsync/src/p4gitsync/cli/status_reporter.py`
- Test: `p4gitsync/tests/test_status_reporter.py`

- [ ] **Step 1: 테스트 작성**

```python
# p4gitsync/tests/test_status_reporter.py
from p4gitsync.cli.status_reporter import format_table, format_duration


class TestFormatDuration:
    def test_seconds(self):
        assert format_duration(45) == "45초"

    def test_minutes(self):
        assert format_duration(3661) == "1시간 1분"

    def test_days(self):
        assert format_duration(90061) == "1일 1시간"


class TestFormatTable:
    def test_basic_table(self):
        headers = ["이름", "상태"]
        rows = [["svc-a", "실행중"], ["svc-b", "중지"]]
        result = format_table(headers, rows)
        assert "svc-a" in result
        assert "svc-b" in result
        assert "이름" in result
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd p4gitsync && pytest tests/test_status_reporter.py -v`
Expected: FAIL

- [ ] **Step 3: 구현**

```python
# p4gitsync/src/p4gitsync/cli/status_reporter.py
from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

from p4gitsync.cli.service_registry import ServiceRegistry
from p4gitsync.cli.service_manager import create_service_manager


def format_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}초"
    if s < 3600:
        return f"{s // 60}분 {s % 60}초"
    if s < 86400:
        return f"{s // 3600}시간 {s % 3600 // 60}분"
    return f"{s // 86400}일 {s % 86400 // 3600}시간"


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """간단한 텍스트 테이블 포맷."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(cells))

    lines = [fmt_row(headers), "  ".join("-" * w for w in col_widths)]
    for row in rows:
        lines.append(fmt_row(row))
    return "\n".join(lines)


def show_status(name: str | None = None) -> None:
    """등록된 서비스 상태 출력."""
    registry = ServiceRegistry()
    manager = create_service_manager(registry)
    services = registry.list_all()

    if not services:
        print("등록된 동기화 서비스가 없습니다.")
        return

    if name:
        entry = registry.get(name)
        if not entry:
            print(f"서비스 '{name}'을 찾을 수 없습니다.")
            return
        _show_detail(name, entry, manager)
    else:
        _show_summary(services, manager)


def _show_summary(services: dict[str, dict], manager) -> None:
    headers = ["이름", "상태", "Stream", "Last CL"]
    rows = []
    for name, entry in services.items():
        running = manager.is_running(name)
        status = "● 실행중" if running else "○ 중지"
        stream, last_cl = _read_config_state(entry["config"])
        rows.append([name, status, stream, str(last_cl)])
    print("\n등록된 동기화 서비스:")
    print(format_table(headers, rows))
    print()


def _show_detail(name: str, entry: dict, manager) -> None:
    config_path = entry["config"]
    running = manager.is_running(name)
    pid = manager.get_pid(name) if running else None
    stream, last_cl = _read_config_state(config_path)

    config = _load_config(config_path)
    direction = "p4_to_git"
    if config:
        dirs = config.get("stream_policy", {}).get("sync_directions", [])
        if dirs:
            direction = dirs[0].get("direction", "p4_to_git")

    lfs_enabled = config.get("lfs", {}).get("enabled", False) if config else False
    lfs_exts = len(config.get("lfs", {}).get("extensions", [])) if config else 0
    repo_path = config.get("git", {}).get("repo_path", "") if config else ""

    status_str = f"● 실행중 (PID {pid})" if running else "○ 중지"

    print(f"\n서비스: {name}")
    print(f"  상태:        {status_str}")
    print(f"  Config:      {config_path}")
    print(f"  Stream:      {stream}")
    print(f"  방향:        {direction}")
    print(f"  Git repo:    {repo_path}")
    print(f"  Last CL:     {last_cl}")
    print(f"  LFS:         {'활성화 (' + str(lfs_exts) + ' 확장자)' if lfs_enabled else '비활성화'}")
    print()


def _load_config(config_path: str) -> dict | None:
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return None


def _read_config_state(config_path: str) -> tuple[str, int]:
    """config.toml에서 stream, state.db에서 last_cl 조회."""
    config = _load_config(config_path)
    if not config:
        return "?", 0
    stream = config.get("p4", {}).get("stream", "?")
    db_path = config.get("state", {}).get("db_path", "")
    last_cl = 0
    if db_path and Path(db_path).exists():
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT last_cl FROM sync_state WHERE stream = ?", (stream,)
            ).fetchone()
            if row:
                last_cl = row[0]
            conn.close()
        except Exception:
            pass
    return stream, last_cl
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd p4gitsync && pytest tests/test_status_reporter.py -v`
Expected: 3 passed

- [ ] **Step 5: 커밋**

```bash
git add p4gitsync/src/p4gitsync/cli/status_reporter.py p4gitsync/tests/test_status_reporter.py
git commit -m "feat: 동기화 상태 조회 (status 명령)"
```

---

### Task 5: __main__.py CLI 통합

**Files:**
- Modify: `p4gitsync/src/p4gitsync/__main__.py`

- [ ] **Step 1: setup, service, status 서브커맨드 추가**

`_build_parser()`에 추가:

```python
    # setup
    setup_parser = subparsers.add_parser("setup", help="대화형 config.toml 생성/수정")

    # service
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

    # status
    status_parser = subparsers.add_parser("status", help="동기화 상태 조회")
    status_parser.add_argument("--name", help="특정 서비스만 조회")
```

`main()`에 분기 추가:

```python
    elif command == "setup":
        from p4gitsync.cli.setup_wizard import run_setup
        run_setup(args.config)
    elif command == "service":
        _run_service(args)
    elif command == "status":
        from p4gitsync.cli.status_reporter import show_status
        show_status(args.name)
```

`_run_service` 함수 추가:

```python
def _run_service(args) -> None:
    from p4gitsync.cli.service_manager import create_service_manager

    manager = create_service_manager()
    subcmd = args.service_command
    name = args.name

    if subcmd == "install":
        import sys
        exe_path = sys.executable
        # PyInstaller frozen인 경우
        if getattr(sys, "frozen", False):
            exe_path = sys.executable
        else:
            exe_path = f"{sys.executable} -m p4gitsync"
        config_path = str(Path(args.config).resolve())
        manager.install(name, exe_path, config_path)
        print(f"서비스 '{name}' 등록 완료.")
        print(f"시작: p4gitsync service start --name {name}")
    elif subcmd == "start":
        manager.start(name)
        print(f"서비스 '{name}' 시작됨.")
    elif subcmd == "stop":
        manager.stop(name)
        print(f"서비스 '{name}' 중지됨.")
    elif subcmd == "uninstall":
        manager.uninstall(name)
        print(f"서비스 '{name}' 제거됨.")
    else:
        print("사용법: p4gitsync service {install|start|stop|uninstall}")
```

- [ ] **Step 2: 전체 테스트 실행**

Run: `cd p4gitsync && pytest tests/ -x -q --ignore=tests/test_commit_metadata.py --ignore=tests/test_git_operator.py`
Expected: all passed

- [ ] **Step 3: CLI 동작 확인**

```bash
python -m p4gitsync setup --help
python -m p4gitsync service --help
python -m p4gitsync status --help
```

- [ ] **Step 4: 커밋**

```bash
git add p4gitsync/src/p4gitsync/__main__.py
git commit -m "feat: setup/service/status CLI 서브커맨드 통합"
```
