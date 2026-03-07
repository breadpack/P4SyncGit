from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("p4gitsync.notifications.daily_report")


@dataclass
class DailyStats:
    processed_cls: int = 0
    failed_cls: int = 0
    total_errors: int = 0
    streams_synced: set[str] = field(default_factory=set)
    avg_sync_time_ms: float = 0
    sync_times: list[float] = field(default_factory=list)

    def record_sync(self, stream: str, duration_ms: float) -> None:
        self.processed_cls += 1
        self.streams_synced.add(stream)
        self.sync_times.append(duration_ms)
        self.avg_sync_time_ms = sum(self.sync_times) / len(self.sync_times)

    def record_error(self) -> None:
        self.total_errors += 1
        self.failed_cls += 1


class DailyReporter:
    """매일 지정된 시각에 일일 리포트를 생성한다."""

    def __init__(self, report_hour: int = 9) -> None:
        self._report_hour = report_hour
        self._stats = DailyStats()
        self._last_report_date: str = ""

    @property
    def stats(self) -> DailyStats:
        return self._stats

    def should_send_report(self) -> bool:
        """현재 시각이 리포트 시간이고, 오늘 아직 보내지 않았으면 True."""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if today == self._last_report_date:
            return False
        return now.hour >= self._report_hour

    def generate_report(self, disk_usage_percent: float = 0) -> str:
        """일일 리포트 메시지를 생성하고 통계를 초기화한다."""
        s = self._stats
        today = datetime.now().strftime("%Y-%m-%d")
        streams_str = ", ".join(sorted(s.streams_synced)) if s.streams_synced else "없음"

        report = (
            f":bar_chart: P4GitSync 일일 리포트 ({today})\n"
            f"---\n"
            f"처리된 CL: {s.processed_cls}건\n"
            f"실패한 CL: {s.failed_cls}건\n"
            f"총 에러: {s.total_errors}건\n"
            f"동기화 stream: {streams_str}\n"
            f"평균 동기화 시간: {s.avg_sync_time_ms:.1f}ms\n"
            f"디스크 사용량: {disk_usage_percent:.1f}%"
        )

        self._last_report_date = today
        self._stats = DailyStats()
        return report
