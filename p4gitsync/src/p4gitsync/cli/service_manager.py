"""서비스 매니저 — Windows(NSSM) / Linux(systemd) 서비스 설치·제어."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.request import urlopen

from p4gitsync.cli.service_registry import ServiceRegistry

logger = logging.getLogger(__name__)


class ServiceManager(ABC):
    """플랫폼별 서비스 관리 추상 인터페이스."""

    def __init__(self, registry: ServiceRegistry | None = None) -> None:
        self.registry = registry or ServiceRegistry()

    @abstractmethod
    def install(self, name: str, exe_path: str, config_path: str) -> None:
        """서비스를 등록한다."""

    @abstractmethod
    def uninstall(self, name: str) -> None:
        """서비스를 제거한다."""

    @abstractmethod
    def start(self, name: str) -> None:
        """서비스를 시작한다."""

    @abstractmethod
    def stop(self, name: str) -> None:
        """서비스를 중지한다."""

    @abstractmethod
    def is_running(self, name: str) -> bool:
        """서비스가 실행 중인지 반환한다."""

    @abstractmethod
    def get_pid(self, name: str) -> int | None:
        """서비스의 PID를 반환한다. 실행 중이 아니면 None."""


# ---------------------------------------------------------------------------
# Windows — NSSM 기반
# ---------------------------------------------------------------------------


class WindowsServiceManager(ServiceManager):
    """NSSM(Non-Sucking Service Manager)을 이용한 Windows 서비스 관리."""

    _NSSM_URL = "https://nssm.cc/release/nssm-2.24.zip"

    def _nssm_path(self) -> Path:
        """NSSM 실행 파일 경로를 반환한다. 없으면 자동 다운로드."""
        dest = Path.home() / ".p4gitsync" / "nssm.exe"
        if not dest.exists():
            self._download_nssm(dest)
        return dest

    def _download_nssm(self, dest: Path) -> None:
        """nssm-2.24.zip 에서 win64/nssm.exe 를 추출한다."""
        logger.info("NSSM 다운로드 중: %s", self._NSSM_URL)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(self._NSSM_URL) as resp:  # noqa: S310
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for entry in zf.namelist():
                if entry.endswith("win64/nssm.exe"):
                    dest.write_bytes(zf.read(entry))
                    logger.info("NSSM 설치 완료: %s", dest)
                    return
        msg = "nssm-2.24.zip 에서 win64/nssm.exe를 찾을 수 없습니다."
        raise FileNotFoundError(msg)

    def _run_nssm(self, *args: str) -> subprocess.CompletedProcess[str]:
        nssm = str(self._nssm_path())
        return subprocess.run(
            [nssm, *args],
            capture_output=True,
            text=True,
            check=False,
        )

    # -- public API --

    def install(self, name: str, exe_path: str, config_path: str) -> None:
        nssm = str(self._nssm_path())
        subprocess.run([nssm, "install", name, exe_path], check=True)

        # 서비스 파라미터 설정
        settings: dict[str, str] = {
            "AppParameters": f"run --config {config_path}",
            "AppStdout": str(Path.home() / ".p4gitsync" / f"{name}.log"),
            "AppStderr": str(Path.home() / ".p4gitsync" / f"{name}.err.log"),
            "AppRotateFiles": "1",
            "Start": "SERVICE_AUTO_START",
            "AppRestartDelay": "5000",
        }
        for key, value in settings.items():
            subprocess.run([nssm, "set", name, key, value], check=True)

        self.registry.add(name, config=config_path, platform="windows")
        logger.info("서비스 설치 완료: %s", name)

    def uninstall(self, name: str) -> None:
        self._run_nssm("stop", name)
        subprocess.run(
            [str(self._nssm_path()), "remove", name, "confirm"],
            check=True,
        )
        self.registry.remove(name)
        logger.info("서비스 제거 완료: %s", name)

    def start(self, name: str) -> None:
        subprocess.run([str(self._nssm_path()), "start", name], check=True)
        logger.info("서비스 시작: %s", name)

    def stop(self, name: str) -> None:
        subprocess.run([str(self._nssm_path()), "stop", name], check=True)
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
                        return int(parts[-1].strip())
                    except ValueError:
                        return None
        return None


# ---------------------------------------------------------------------------
# Linux — systemd 기반
# ---------------------------------------------------------------------------


class LinuxServiceManager(ServiceManager):
    """systemd 유닛을 이용한 Linux 서비스 관리."""

    _UNIT_TEMPLATE = """\
[Unit]
Description=P4GitSync - {name}
After=network.target

[Service]
Type=simple
ExecStart={exe_path} run --config {config_path}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

    @staticmethod
    def _unit_path(name: str) -> Path:
        return Path(f"/etc/systemd/system/{name}.service")

    @staticmethod
    def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    # -- public API --

    def install(self, name: str, exe_path: str, config_path: str) -> None:
        unit = self._UNIT_TEMPLATE.format(
            name=name,
            exe_path=exe_path,
            config_path=config_path,
        )
        unit_path = self._unit_path(name)
        unit_path.write_text(unit, encoding="utf-8")

        self._systemctl("daemon-reload")
        self._systemctl("enable", name)

        self.registry.add(name, config=config_path, platform="linux")
        logger.info("서비스 설치 완료: %s", name)

    def uninstall(self, name: str) -> None:
        self._systemctl("stop", name)
        self._systemctl("disable", name)

        unit_path = self._unit_path(name)
        if unit_path.exists():
            unit_path.unlink()

        self._systemctl("daemon-reload")

        self.registry.remove(name)
        logger.info("서비스 제거 완료: %s", name)

    def start(self, name: str) -> None:
        subprocess.run(["systemctl", "start", name], check=True)
        logger.info("서비스 시작: %s", name)

    def stop(self, name: str) -> None:
        subprocess.run(["systemctl", "stop", name], check=True)
        logger.info("서비스 중지: %s", name)

    def is_running(self, name: str) -> bool:
        result = self._systemctl("is-active", name)
        return result.stdout.strip() == "active"

    def get_pid(self, name: str) -> int | None:
        result = self._systemctl("show", "--property=MainPID", "--value", name)
        try:
            pid = int(result.stdout.strip())
            return pid if pid > 0 else None
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_service_manager(
    registry: ServiceRegistry | None = None,
) -> ServiceManager:
    """플랫폼에 맞는 ServiceManager 인스턴스를 생성한다."""
    if sys.platform == "win32":
        return WindowsServiceManager(registry)
    return LinuxServiceManager(registry)
