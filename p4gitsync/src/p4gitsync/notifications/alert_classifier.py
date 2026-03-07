from __future__ import annotations

import enum


class AlertLevel(enum.Enum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


class AlertClassifier:
    """에러 유형을 판단하고 채널을 라우팅한다."""

    _IMMEDIATE_KEYWORDS = (
        "ENOSPC",
        "No space left on device",
        "MemoryError",
        "Out of memory",
        "OOM",
        "Cannot allocate memory",
    )

    _CONNECTION_KEYWORDS = (
        "Connection refused",
        "Connection timed out",
        "ConnectionError",
        "ConnectionResetError",
        "P4Exception",
    )

    def classify_error(self, error: str) -> str:
        """에러 문자열을 분석하여 'immediate' 또는 'standard'를 반환."""
        for keyword in self._IMMEDIATE_KEYWORDS:
            if keyword in error:
                return "immediate"
        for keyword in self._CONNECTION_KEYWORDS:
            if keyword in error:
                return "immediate"
        return "standard"

    def classify_level(
        self,
        *,
        consecutive_failures: int = 0,
        error: str | None = None,
        is_integrity_failure: bool = False,
        sync_delay_minutes: float = 0,
        pending_queue_size: int = 0,
        disk_usage_percent: float = 0,
    ) -> AlertLevel:
        """조건에 따라 AlertLevel을 결정한다."""
        if is_integrity_failure:
            return AlertLevel.ERROR
        if error and self.classify_error(error) == "immediate":
            return AlertLevel.ERROR
        if consecutive_failures >= 3:
            return AlertLevel.ERROR

        if sync_delay_minutes > 5:
            return AlertLevel.WARN
        if pending_queue_size > 100:
            return AlertLevel.WARN
        if disk_usage_percent > 85:
            return AlertLevel.WARN

        return AlertLevel.INFO

    def get_channel_key(self, level: AlertLevel) -> str:
        """AlertLevel에 대응하는 채널 키를 반환."""
        return {
            AlertLevel.ERROR: "alerts",
            AlertLevel.WARN: "warnings",
            AlertLevel.INFO: "info",
        }[level]
