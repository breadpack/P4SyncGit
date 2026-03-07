import asyncio
import json as _json
import logging
import signal
import sys
import tomllib
from pathlib import Path

from p4gitsync.services.sync_orchestrator import SyncOrchestrator

logger = logging.getLogger("p4gitsync")


def load_config(path: str = "config.toml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        print(f"설정 파일을 찾을 수 없습니다: {path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path, "rb") as f:
        return tomllib.load(f)


async def run(config: dict) -> None:
    orchestrator = SyncOrchestrator(config)
    await orchestrator.start()


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    config = load_config(config_path)
    setup_logging(config.get("logging", {}))

    loop = asyncio.new_event_loop()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, loop.stop)
    else:
        def _win_signal_handler(signum: int, frame: object) -> None:
            loop.call_soon_threadsafe(loop.stop)

        signal.signal(signal.SIGINT, _win_signal_handler)
        signal.signal(signal.SIGTERM, _win_signal_handler)

    try:
        loop.run_until_complete(run(config))
    finally:
        loop.close()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _json.dumps({
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        })


def setup_logging(log_config: dict) -> None:
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    handler = logging.StreamHandler()
    if log_config.get("format") == "json":
        handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler])


if __name__ == "__main__":
    main()
