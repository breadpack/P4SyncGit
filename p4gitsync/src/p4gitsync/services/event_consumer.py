from __future__ import annotations

import logging
import time

import redis

from p4gitsync.config.sync_config import RedisConfig

logger = logging.getLogger("p4gitsync.event_consumer")


class EventConsumer:
    """Redis Stream 기반 이벤트 소비자.

    P4 Trigger -> API -> Redis Stream -> EventConsumer -> Orchestrator 파이프라인의
    소비 측을 담당한다. 폴링 fallback과 heartbeat 모니터링도 포함.
    """

    def __init__(
        self,
        redis_config: RedisConfig,
        on_changelist: callable,
        fallback_poll: callable | None = None,
    ) -> None:
        self._config = redis_config
        self._on_changelist = on_changelist
        self._fallback_poll = fallback_poll
        self._redis: redis.Redis | None = None
        self._running = False
        self._last_event_time: float = 0.0

    def connect(self) -> None:
        self._redis = redis.Redis.from_url(self._config.url, decode_responses=True)
        self._ensure_consumer_group()
        self._last_event_time = time.monotonic()
        logger.info(
            "Redis 연결 완료: stream=%s, group=%s, consumer=%s",
            self._config.stream_key,
            self._config.group_name,
            self._config.consumer_name,
        )

    def _ensure_consumer_group(self) -> None:
        try:
            self._redis.xgroup_create(
                self._config.stream_key,
                self._config.group_name,
                id="0",
                mkstream=True,
            )
            logger.info("Consumer group 생성: %s", self._config.group_name)
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.debug("Consumer group 이미 존재: %s", self._config.group_name)
            else:
                raise

    def consume(self) -> None:
        """메인 소비 루프. stop() 호출 시 종료."""
        self._running = True
        logger.info("EventConsumer 소비 루프 시작")

        while self._running:
            try:
                self._claim_stale_pending()
                self._read_and_process()
                self._check_heartbeat()
            except redis.ConnectionError:
                logger.error("Redis 연결 끊김. 5초 후 재연결 시도.")
                time.sleep(5)
                try:
                    self.connect()
                except Exception:
                    logger.exception("Redis 재연결 실패")
            except Exception:
                logger.exception("소비 루프 에러")
                time.sleep(1)

    def stop(self) -> None:
        self._running = False

    def _read_and_process(self) -> None:
        messages = self._redis.xreadgroup(
            self._config.group_name,
            self._config.consumer_name,
            {self._config.stream_key: ">"},
            count=self._config.batch_size,
            block=self._config.block_ms,
        )

        if not messages:
            return

        for stream_name, entries in messages:
            for msg_id, data in entries:
                self._process_message(msg_id, data)

    def _process_message(self, msg_id: str, data: dict) -> None:
        changelist = int(data.get("changelist", 0))
        user = data.get("user", "")
        stream = data.get("stream", "")

        if changelist <= 0:
            logger.warning("잘못된 changelist: msg_id=%s, data=%s", msg_id, data)
            self._redis.xack(
                self._config.stream_key, self._config.group_name, msg_id,
            )
            return

        try:
            self._on_changelist(changelist, user, stream)
            self._redis.xack(
                self._config.stream_key, self._config.group_name, msg_id,
            )
            self._last_event_time = time.monotonic()
            logger.debug("이벤트 처리 완료: CL %d, user=%s, stream=%s", changelist, user, stream)
        except Exception:
            logger.exception("이벤트 처리 실패: CL %d", changelist)

    def _check_heartbeat(self) -> None:
        """heartbeat_timeout_minutes 이상 이벤트가 없으면 P4 교차 확인 (fallback poll)."""
        if self._fallback_poll is None:
            return

        timeout_seconds = self._config.heartbeat_timeout_minutes * 60
        elapsed = time.monotonic() - self._last_event_time

        if elapsed >= timeout_seconds:
            logger.warning(
                "Trigger heartbeat 초과 (%.0f분). Fallback 폴링 실행.",
                elapsed / 60,
            )
            try:
                self._fallback_poll()
            except Exception:
                logger.exception("Fallback 폴링 실패")
            self._last_event_time = time.monotonic()

    def _claim_stale_pending(self) -> None:
        """pending_claim_timeout_hours 이상 미처리된 메시지를 현재 consumer로 claim."""
        claim_ms = self._config.pending_claim_timeout_hours * 3600 * 1000

        try:
            result = self._redis.xautoclaim(
                self._config.stream_key,
                self._config.group_name,
                self._config.consumer_name,
                min_idle_time=claim_ms,
                start_id="0-0",
                count=self._config.batch_size,
            )
            # xautoclaim returns (next_start_id, claimed_messages, deleted_ids)
            claimed = result[1] if len(result) > 1 else []
            if claimed:
                logger.info("Stale pending 메시지 %d건 claim", len(claimed))
                for msg_id, data in claimed:
                    self._process_message(msg_id, data)
        except redis.ResponseError as e:
            if "NOGROUP" in str(e):
                self._ensure_consumer_group()
            else:
                logger.warning("xautoclaim 실패: %s", e)

    def publish_event(self, changelist: int, user: str, stream: str = "") -> str:
        """Redis Stream에 이벤트 발행. API 서버에서 호출."""
        fields = {"changelist": str(changelist), "user": user}
        if stream:
            fields["stream"] = stream
        msg_id = self._redis.xadd(
            self._config.stream_key,
            fields,
            maxlen=self._config.max_stream_length,
            approximate=True,
        )
        logger.debug("이벤트 발행: CL %d -> %s", changelist, msg_id)
        return msg_id

    def get_stream_info(self) -> dict:
        """모니터링용 Redis Stream 상태 정보."""
        try:
            info = self._redis.xinfo_stream(self._config.stream_key)
            groups = self._redis.xinfo_groups(self._config.stream_key)
            pending_total = sum(g.get("pending", 0) for g in groups)

            memory_info = self._redis.info("memory")

            return {
                "stream_length": info.get("length", 0),
                "groups": len(groups),
                "pending_messages": pending_total,
                "memory_used_mb": round(
                    memory_info.get("used_memory", 0) / (1024 * 1024), 2,
                ),
            }
        except Exception as e:
            logger.error("Redis Stream 정보 조회 실패: %s", e)
            return {"error": str(e)}
