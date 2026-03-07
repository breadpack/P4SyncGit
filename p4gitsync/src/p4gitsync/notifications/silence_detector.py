from __future__ import annotations

import logging
import time

logger = logging.getLogger("p4gitsync.notifications.silence")


class SilenceDetector:
    """P4 활동이 있음에도 동기화가 멈춘 '침묵 장애'를 감지한다."""

    def __init__(self, threshold_minutes: int = 30) -> None:
        self._threshold_seconds = threshold_minutes * 60
        self._last_sync_time: float = time.time()
        self._alerted = False

    def record_sync(self) -> None:
        """동기화 성공 시 호출하여 마지막 동기화 시간을 갱신."""
        self._last_sync_time = time.time()
        self._alerted = False

    def check(self, p4_has_recent_activity: bool) -> bool:
        """침묵 장애 여부를 반환한다.

        P4에 최근 활동이 있는데 동기화가 threshold 이상 없으면 True.
        한 번 알림 후 다음 동기화가 되기 전까지 재알림하지 않는다.
        """
        if self._alerted:
            return False

        if not p4_has_recent_activity:
            return False

        elapsed = time.time() - self._last_sync_time
        if elapsed >= self._threshold_seconds:
            self._alerted = True
            logger.warning(
                "침묵 장애 감지: %.1f분 동안 동기화 없음 (P4 활동 있음)",
                elapsed / 60,
            )
            return True

        return False

    @property
    def minutes_since_last_sync(self) -> float:
        return (time.time() - self._last_sync_time) / 60
