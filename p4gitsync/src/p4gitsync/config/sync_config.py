from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from p4gitsync.config.lfs_config import LfsConfig


@dataclass
class P4Config:
    port: str
    user: str
    workspace: str
    stream: str
    filelog_batch_size: int = 200


@dataclass
class GitConfig:
    repo_path: str
    remote_url: str
    default_branch: str = "main"
    backend: str = "pygit2"  # "pygit2" or "cli"


@dataclass
class StateConfig:
    db_path: str


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
