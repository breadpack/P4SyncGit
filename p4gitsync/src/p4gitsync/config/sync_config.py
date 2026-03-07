from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field

from p4gitsync.config.lfs_config import LfsConfig


_KNOWN_SECTIONS = [
    "INITIAL_IMPORT",
    "STREAM_POLICY",
    "LOGGING",
    "SLACK",
    "STATE",
    "REDIS",
    "SYNC",
    "API",
    "GIT",
    "LFS",
    "P4",
]


def _coerce_value(raw: str) -> str | int | float | bool:
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def apply_env_overrides(config: dict) -> dict:
    """P4GITSYNC_{SECTION}_{KEY} 환경변수로 설정값을 오버라이드한다.

    예시:
        P4GITSYNC_P4_PORT=ssl:server:1666       -> [p4] port
        P4GITSYNC_SLACK_WEBHOOK_URL=https://...  -> [slack] webhook_url
        P4GITSYNC_INITIAL_IMPORT_BATCH_SIZE=200  -> [initial_import] batch_size
        P4GITSYNC_SYNC_POLLING_INTERVAL_SECONDS=60 -> [sync] polling_interval_seconds
    """
    prefix = "P4GITSYNC_"
    for env_key, raw_value in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        remainder = env_key[len(prefix):]
        for section in _KNOWN_SECTIONS:
            if remainder.startswith(section + "_"):
                key = remainder[len(section) + 1:].lower()
                if not key:
                    break
                section_lower = section.lower()
                if section_lower not in config:
                    config[section_lower] = {}
                config[section_lower][key] = _coerce_value(raw_value)
                break
    return config


@dataclass
class P4Config:
    port: str = ""
    user: str = ""
    workspace: str = ""
    stream: str = ""
    filelog_batch_size: int = 200


@dataclass
class GitConfig:
    repo_path: str = ""
    remote_url: str = ""
    default_branch: str = "main"
    backend: str = "pygit2"  # "pygit2" or "cli"


@dataclass
class StateConfig:
    db_path: str = ""


@dataclass
class SyncConfig:
    polling_interval_seconds: int = 30
    batch_size: int = 50
    push_after_every_commit: bool = False
    file_extraction_mode: str = "print"
    print_to_sync_threshold: int = 50
    git_gc_interval: int = 5000
    error_retry_threshold: int = 3
    push_batch_size: int = 10
    push_interval_seconds: int = 60


@dataclass
class InitialImportConfig:
    mode: str = "full_history"
    start_changelist: int = 1
    batch_size: int = 100
    resume_on_restart: bool = True
    checkpoint_interval: int = 1000
    use_fast_import: bool = True
    replica_port: str = ""


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"
    file: str = ""


@dataclass
class ApiConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    trigger_secret: str = ""


@dataclass
class RedisConfig:
    enabled: bool = False
    url: str = "redis://localhost:6379/0"
    stream_key: str = "p4sync:events"
    group_name: str = "p4sync-workers"
    consumer_name: str = "worker-1"
    max_stream_length: int = 10000
    block_ms: int = 5000
    batch_size: int = 10
    heartbeat_timeout_minutes: int = 30
    pending_claim_timeout_hours: int = 24


@dataclass
class SlackConfig:
    webhook_url: str = ""
    channel: str = "#p4sync-alerts"
    alerts_webhook_url: str = ""
    warnings_webhook_url: str = ""
    info_webhook_url: str = ""
    alerts_channel: str = "#p4gitsync-alerts"
    warnings_channel: str = "#p4gitsync-warnings"
    info_channel: str = "#p4gitsync-info"
    silence_threshold_minutes: int = 30
    daily_report_hour: int = 9


@dataclass
class StreamPolicy:
    """Stream 자동 감지 필터링 정책."""

    auto_discover: bool = True
    include_patterns: list[str] = field(default_factory=list)
    exclude_types: list[str] = field(default_factory=list)
    exclude_streams: list[str] = field(default_factory=list)
    task_stream_policy: str = "ignore"  # "ignore" | "include"

    def should_include(self, stream: str, stream_type: str) -> bool:
        """stream이 필터링 정책에 의해 포함되어야 하는지 판단."""
        if not self.auto_discover:
            return False

        if stream_type == "task" and self.task_stream_policy == "ignore":
            return False

        if stream_type in self.exclude_types:
            return False

        for pattern in self.exclude_streams:
            if fnmatch.fnmatch(stream, pattern):
                return False

        if self.include_patterns:
            return any(fnmatch.fnmatch(stream, p) for p in self.include_patterns)

        return True


@dataclass
class AppConfig:
    p4: P4Config
    git: GitConfig
    state: StateConfig
    sync: SyncConfig = field(default_factory=SyncConfig)
    initial_import: InitialImportConfig = field(default_factory=InitialImportConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    lfs: LfsConfig = field(default_factory=LfsConfig)
    stream_policy: StreamPolicy = field(default_factory=StreamPolicy)
    redis: RedisConfig = field(default_factory=RedisConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        return cls(
            p4=P4Config(**data.get("p4", {})),
            git=GitConfig(**data.get("git", {})),
            state=StateConfig(**data.get("state", {})),
            sync=SyncConfig(**data.get("sync", {})),
            initial_import=InitialImportConfig(**data.get("initial_import", {})),
            logging=LoggingConfig(**data.get("logging", {})),
            slack=SlackConfig(**data.get("slack", {})),
            api=ApiConfig(**data.get("api", {})),
            lfs=LfsConfig(**data.get("lfs", {})),
            stream_policy=StreamPolicy(**data.get("stream_policy", {})),
            redis=RedisConfig(**data.get("redis", {})),
        )
