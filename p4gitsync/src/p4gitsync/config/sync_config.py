from dataclasses import dataclass, field


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
class SlackConfig:
    webhook_url: str = ""
    channel: str = "#p4sync-alerts"


@dataclass
class AppConfig:
    p4: P4Config
    git: GitConfig
    state: StateConfig
    sync: SyncConfig = field(default_factory=SyncConfig)
    initial_import: InitialImportConfig = field(default_factory=InitialImportConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)

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
        )
