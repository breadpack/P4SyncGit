from __future__ import annotations

import logging
import time

from slack_sdk.webhook import WebhookClient

from p4gitsync.notifications.alert_classifier import AlertClassifier, AlertLevel

logger = logging.getLogger("p4gitsync.notifications")

_DEDUP_WINDOW_SECONDS = 24 * 60 * 60  # 24시간


class SlackNotifier:
    """채널별 Slack 알림을 전송한다.

    - alerts: ERROR 레벨 (즉시 대응)
    - warnings: WARN 레벨 (주의 관찰)
    - info: INFO 레벨, READY/NOT READY, 일일 리포트
    """

    def __init__(
        self,
        webhook_url: str,
        channel: str = "",
        *,
        alerts_webhook_url: str = "",
        warnings_webhook_url: str = "",
        info_webhook_url: str = "",
    ) -> None:
        self._classifier = AlertClassifier()
        self._sent_alerts: dict[str, float] = {}

        self._clients: dict[str, WebhookClient | None] = {
            "alerts": _make_client(alerts_webhook_url or webhook_url),
            "warnings": _make_client(warnings_webhook_url or webhook_url),
            "info": _make_client(info_webhook_url or webhook_url),
        }
        # 하위 호환: 채널 미분리 시 단일 클라이언트 사용
        self._fallback_client = _make_client(webhook_url)

    def send_error(self, changelist: int, stream: str, error: str) -> None:
        """ERROR 레벨 알림을 alerts 채널로 전송."""
        dedup_key = f"error:{changelist}:{stream}"
        prefix = self._get_dedup_prefix(dedup_key)

        urgency = self._classifier.classify_error(error)
        icon = ":rotating_light:" if urgency == "immediate" else ":warning:"

        message = (
            f"{icon} {prefix}P4GitSync 에러\n"
            f"CL: {changelist}\n"
            f"Stream: {stream}\n"
            f"긴급도: {urgency}\n"
            f"```{error}```"
        )
        self._send_to_channel("alerts", message, dedup_key)

    def send_warning(self, message: str, dedup_key: str = "") -> None:
        """WARN 레벨 알림을 warnings 채널로 전송."""
        key = dedup_key or f"warn:{message[:50]}"
        prefix = self._get_dedup_prefix(key)
        self._send_to_channel("warnings", f":large_yellow_circle: {prefix}{message}", key)

    def send_info(self, message: str) -> None:
        """INFO 레벨 알림을 info 채널로 전송."""
        self._send_to_channel("info", message)

    def send_readiness(self, ready: bool, details: str = "") -> None:
        """READY/NOT READY 상태 변경을 info 채널로 전송."""
        status = "READY" if ready else "NOT READY"
        icon = ":white_check_mark:" if ready else ":x:"
        message = f"{icon} 컷오버 상태: [{status}]"
        if details:
            message += f"\n{details}"
        self._send_to_channel("info", message)

    def send_daily_report(self, report: str) -> None:
        """일일 리포트를 info 채널로 전송."""
        self._send_to_channel("info", report)

    def send_silence_alert(self, minutes: float) -> None:
        """침묵 장애 경고를 warnings 채널로 전송."""
        self.send_warning(
            f"침묵 장애 감지: {minutes:.0f}분 동안 동기화 없음 (P4 활동 있음)",
            dedup_key="silence_alert",
        )

    def send_sync_delay_warning(self, delay_minutes: float, stream: str) -> None:
        """동기화 지연 경고."""
        self.send_warning(
            f"동기화 지연: {stream} ({delay_minutes:.1f}분 초과)",
            dedup_key=f"sync_delay:{stream}",
        )

    def send_queue_warning(self, queue_size: int) -> None:
        """미처리 큐 경고."""
        self.send_warning(
            f"미처리 큐: {queue_size}건 초과",
            dedup_key="queue_overflow",
        )

    def send_disk_warning(self, usage_percent: float) -> None:
        """디스크 임계값 경고."""
        self.send_warning(
            f"디스크 사용량: {usage_percent:.1f}% (임계값 초과)",
            dedup_key="disk_usage",
        )

    def send_new_stream(self, stream: str) -> None:
        """신규 stream 감지 알림."""
        self.send_info(f":new: 신규 stream 감지: {stream}")

    def send_integrity_failure(self, branch: str, details: str) -> None:
        """무결성 검증 실패를 ERROR로 전송."""
        message = (
            f":rotating_light: 무결성 검증 실패\n"
            f"Branch: {branch}\n"
            f"```{details}```"
        )
        self._send_to_channel("alerts", message, f"integrity:{branch}")

    def send_connection_failure(self, service: str, error: str) -> None:
        """P4/Git 연결 실패를 ERROR로 전송."""
        message = (
            f":rotating_light: {service} 연결 실패\n"
            f"```{error}```"
        )
        self._send_to_channel("alerts", message, f"connection:{service}")

    def send_conflict_alert(
        self,
        branch: str,
        conflict_branch: str,
        conflict_files: list[str],
        p4_changelists: list[int],
        git_commits: list[str],
    ) -> None:
        """양방향 동기화 충돌을 ERROR로 전송."""
        files_str = "\n".join(f"  - {f}" for f in conflict_files[:20])
        if len(conflict_files) > 20:
            files_str += f"\n  ... 외 {len(conflict_files) - 20}개"
        message = (
            f":collision: 양방향 동기화 충돌 감지\n"
            f"Branch: {branch}\n"
            f"충돌 branch: `{conflict_branch}`\n"
            f"P4 CL: {p4_changelists}\n"
            f"Git commits: {[s[:12] for s in git_commits]}\n"
            f"충돌 파일:\n```{files_str}```\n"
            f"해결 방법: Git에서 `{conflict_branch}`를 merge 후 삭제하세요."
        )
        self._send_to_channel("alerts", message, f"conflict:{branch}")

    def _send_to_channel(
        self, channel_key: str, text: str, dedup_key: str = ""
    ) -> None:
        """지정된 채널로 메시지를 전송한다."""
        if dedup_key and self._is_duplicate(dedup_key):
            logger.debug("알림 중복 억제: %s", dedup_key)
            return

        client = self._clients.get(channel_key) or self._fallback_client
        if not client:
            return

        try:
            client.send(text=text)
            if dedup_key:
                self._sent_alerts[dedup_key] = time.time()
        except Exception as e:
            logger.error("Slack 알림 전송 실패 (%s): %s", channel_key, e)

    def _is_duplicate(self, key: str) -> bool:
        """24시간 내 동일 조건 알림이 이미 전송되었는지 확인."""
        last_sent = self._sent_alerts.get(key)
        if last_sent is None:
            return False
        return (time.time() - last_sent) < _DEDUP_WINDOW_SECONDS

    def _get_dedup_prefix(self, key: str) -> str:
        """이미 전송된 알림의 재발송이면 '[지속 중] ' 접두사를 반환."""
        if key in self._sent_alerts:
            # 24시간 경과하여 재전송 가능한 경우
            return "[지속 중] "
        return ""

    def cleanup_expired_alerts(self) -> None:
        """만료된 중복 방지 기록을 정리."""
        now = time.time()
        expired = [
            k for k, v in self._sent_alerts.items()
            if (now - v) >= _DEDUP_WINDOW_SECONDS
        ]
        for k in expired:
            del self._sent_alerts[k]


def _make_client(webhook_url: str) -> WebhookClient | None:
    if not webhook_url:
        return None
    return WebhookClient(webhook_url)
