import logging

from slack_sdk.webhook import WebhookClient

logger = logging.getLogger("p4gitsync.notifications")


class SlackNotifier:
    def __init__(self, webhook_url: str, channel: str = "") -> None:
        self._webhook_url = webhook_url
        self._channel = channel
        self._client: WebhookClient | None = None
        if webhook_url:
            self._client = WebhookClient(webhook_url)

    def send_error(self, changelist: int, stream: str, error: str) -> None:
        if not self._client:
            return
        try:
            self._client.send(
                text=(
                    f":warning: P4GitSync 에러\n"
                    f"CL: {changelist}\n"
                    f"Stream: {stream}\n"
                    f"```{error}```"
                )
            )
        except Exception as e:
            logger.error("Slack 알림 전송 실패: %s", e)

    def send_info(self, message: str) -> None:
        if not self._client:
            return
        try:
            self._client.send(text=message)
        except Exception as e:
            logger.error("Slack 알림 전송 실패: %s", e)
