from __future__ import annotations

import logging

from p4gitsync.config.sync_config import AppConfig
from p4gitsync.notifications.daily_report import DailyReporter
from p4gitsync.notifications.notifier import SlackNotifier
from p4gitsync.notifications.silence_detector import SilenceDetector
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.services.circuit_breaker import IntegrityCircuitBreaker
from p4gitsync.services.db_backup import DatabaseBackup
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.maintenance")


class SyncMaintenanceRunner:
    """주기적 유지보수, 알림, 침묵 장애 감지를 담당."""

    def __init__(
        self,
        config: AppConfig,
        state_store: StateStore,
        p4_client: P4Client,
        db_backup: DatabaseBackup | None,
        circuit_breaker: IntegrityCircuitBreaker | None,
        notifier: SlackNotifier | None,
        silence_detector: SilenceDetector | None,
        daily_reporter: DailyReporter | None,
    ) -> None:
        self._config = config
        self._state = state_store
        self._p4 = p4_client
        self._db_backup = db_backup
        self._circuit_breaker = circuit_breaker
        self._notifier = notifier
        self._silence_detector = silence_detector
        self._daily_reporter = daily_reporter

    def run(self) -> None:
        """주기적 유지보수: DB 백업, 오래된 에러 정리, 무결성 검증."""
        try:
            if self._db_backup:
                self._db_backup.maybe_backup()
        except Exception:
            logger.exception("DB 백업 실패")

        try:
            self._state.cleanup_resolved_errors()
        except Exception:
            logger.exception("sync_errors 정리 실패")

        try:
            self._state.archive_old_commit_maps()
        except Exception:
            logger.exception("cl_commit_map 아카이브 실패")

        try:
            if self._circuit_breaker:
                self._circuit_breaker.run_check()
        except Exception:
            logger.exception("무결성 검증 실패")

        self._check_periodic_notifications()

    def _check_periodic_notifications(self) -> None:
        """침묵 장애 감지 및 일일 리포트 전송."""
        if not self._notifier:
            return

        if self._silence_detector:
            p4_active = self._check_p4_activity()
            if self._silence_detector.check(p4_active):
                self._notifier.send_silence_alert(
                    self._silence_detector.minutes_since_last_sync
                )

        if self._daily_reporter and self._daily_reporter.should_send_report():
            report = self._daily_reporter.generate_report()
            self._notifier.send_daily_report(report)

        self._notifier.cleanup_expired_alerts()

    def _check_p4_activity(self) -> bool:
        """P4에 최근 활동이 있는지 확인."""
        try:
            stream = self._config.p4.stream
            last_cl = self._state.get_last_synced_cl(stream)
            changes = self._p4.get_changes_after(stream, last_cl)
            return len(changes) > 0
        except Exception:
            return False
