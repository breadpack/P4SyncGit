"""실행/서비스 계열 CLI 핸들러.

run(동기화 루프) / service 명령을 처리한다.
"""

from __future__ import annotations

import signal
import sys

from p4gitsync.config.sync_config import AppConfig
from p4gitsync.services.sync_orchestrator import SyncOrchestrator


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


def _run_service(args) -> None:
    from pathlib import Path

    from p4gitsync.cli.service_manager import create_service_manager

    manager = create_service_manager()
    subcmd = args.service_command
    name = getattr(args, "name", "p4gitsync")

    if subcmd == "install":
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
