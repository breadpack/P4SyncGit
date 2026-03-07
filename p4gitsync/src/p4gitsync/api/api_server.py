from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from p4gitsync.config.sync_config import RedisConfig
from p4gitsync.services.circuit_breaker import IntegrityCircuitBreaker
from p4gitsync.services.event_consumer import EventConsumer
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.api")


class TriggerPayload(BaseModel):
    changelist: int
    user: str


class HealthResponse(BaseModel):
    status: str
    last_trigger_time: str | None


class StatusResponse(BaseModel):
    streams: list[dict]
    redis: dict | None
    process: dict


class ErrorEntry(BaseModel):
    changelist: int
    stream: str
    error_msg: str
    retry_count: int
    created_at: str


class CutoverReadinessResponse(BaseModel):
    ready: bool
    total_lag: int
    unresolved_errors: int
    integrity_passed: bool
    last_sync_seconds_ago: float | None
    blockers: list[str]


class ApiServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        trigger_secret: str = "",
        trigger_event: asyncio.Event | None = None,
        redis_config: RedisConfig | None = None,
        state_store: StateStore | None = None,
        event_consumer: EventConsumer | None = None,
        circuit_breaker: IntegrityCircuitBreaker | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._trigger_secret = trigger_secret
        self._trigger_event = trigger_event
        self._redis_config = redis_config
        self._state_store = state_store
        self._event_consumer = event_consumer
        self._circuit_breaker = circuit_breaker
        self._last_trigger_time: datetime | None = None
        self._last_sync_time: datetime | None = None
        self._thread: threading.Thread | None = None
        self._trigger_count: int = 0

        self.app = FastAPI(title="P4GitSync API")
        self._register_routes()

    def _register_routes(self) -> None:
        self._register_trigger_route()
        self._register_health_route()
        self._register_status_route()
        self._register_errors_route()
        self._register_cutover_readiness_route()
        self._register_retry_route()

    def _register_trigger_route(self) -> None:
        @self.app.post("/api/trigger", status_code=202)
        async def trigger(
            payload: TriggerPayload,
            x_trigger_secret: str = Header(default=""),
        ) -> dict:
            if self._trigger_secret and x_trigger_secret != self._trigger_secret:
                raise HTTPException(status_code=403, detail="Invalid trigger secret")

            self._last_trigger_time = datetime.now()
            self._trigger_count += 1
            logger.info("트리거 수신: CL %d, user=%s", payload.changelist, payload.user)

            # Redis 사용 시 Stream에 발행
            if self._event_consumer is not None:
                try:
                    msg_id = self._event_consumer.publish_event(
                        payload.changelist, payload.user,
                    )
                    return {
                        "status": "accepted",
                        "changelist": payload.changelist,
                        "redis_msg_id": msg_id,
                    }
                except Exception as e:
                    logger.error("Redis 발행 실패, fallback 사용: %s", e)

            # Fallback: asyncio.Event 기반 (폴링 트리거)
            if self._trigger_event is not None:
                self._trigger_event.set()

            return {"status": "accepted", "changelist": payload.changelist}

    def _register_health_route(self) -> None:
        @self.app.get("/api/health")
        async def health() -> HealthResponse:
            return HealthResponse(
                status="ok",
                last_trigger_time=(
                    self._last_trigger_time.isoformat()
                    if self._last_trigger_time
                    else None
                ),
            )

    def _register_status_route(self) -> None:
        @self.app.get("/api/status")
        async def status() -> StatusResponse:
            streams = self._get_stream_status()
            redis_info = self._get_redis_info()
            process_info = self._get_process_info()
            return StatusResponse(
                streams=streams, redis=redis_info, process=process_info,
            )

    def _register_errors_route(self) -> None:
        @self.app.get("/api/errors")
        async def errors() -> list[ErrorEntry]:
            if self._state_store is None:
                return []
            raw = self._state_store.get_unresolved_errors()
            return [
                ErrorEntry(
                    changelist=e["changelist"],
                    stream=e["stream"],
                    error_msg=e.get("error_msg", ""),
                    retry_count=e.get("retry_count", 0),
                    created_at=e.get("created_at", ""),
                )
                for e in raw
            ]

    def record_sync_completed(self) -> None:
        """동기화 완료 시점을 기록 (SyncOrchestrator에서 호출)."""
        self._last_sync_time = datetime.now()

    def _register_cutover_readiness_route(self) -> None:
        @self.app.get("/api/cutover-readiness")
        async def cutover_readiness() -> CutoverReadinessResponse:
            blockers, metrics = self._check_cutover_blockers_detailed()
            return CutoverReadinessResponse(
                ready=len(blockers) == 0,
                total_lag=metrics["total_lag"],
                unresolved_errors=metrics["unresolved_errors"],
                integrity_passed=metrics["integrity_passed"],
                last_sync_seconds_ago=metrics["last_sync_seconds_ago"],
                blockers=blockers,
            )

    def _register_retry_route(self) -> None:
        @self.app.post("/api/retry/{changelist}")
        async def retry(changelist: int) -> dict:
            if self._event_consumer is not None:
                try:
                    msg_id = self._event_consumer.publish_event(changelist, "manual-retry")
                    return {"status": "queued", "changelist": changelist, "redis_msg_id": msg_id}
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"Redis 발행 실패: {e}")

            if self._trigger_event is not None:
                self._trigger_event.set()
                return {"status": "triggered", "changelist": changelist}

            raise HTTPException(status_code=503, detail="이벤트 시스템 미설정")

    def _get_stream_status(self) -> list[dict]:
        if self._state_store is None:
            return []
        mappings = self._state_store.get_all_registered_streams()
        result = []
        for m in mappings:
            last_cl = self._state_store.get_last_synced_cl(m.stream)
            result.append({
                "stream": m.stream,
                "branch": m.branch,
                "last_synced_cl": last_cl,
                "parent_stream": m.parent_stream,
            })
        return result

    def _get_redis_info(self) -> dict | None:
        if self._event_consumer is None:
            return None
        return self._event_consumer.get_stream_info()

    def _get_process_info(self) -> dict:
        import os
        import time

        return {
            "pid": os.getpid(),
            "trigger_count": self._trigger_count,
            "last_trigger_time": (
                self._last_trigger_time.isoformat()
                if self._last_trigger_time
                else None
            ),
            "uptime_info": "running",
        }

    def _check_cutover_blockers_detailed(self) -> tuple[list[str], dict]:
        blockers: list[str] = []
        metrics = {
            "total_lag": 0,
            "unresolved_errors": 0,
            "integrity_passed": True,
            "last_sync_seconds_ago": None,
        }

        if self._state_store is None:
            blockers.append("StateStore 미연결")
            return blockers, metrics

        errors = self._state_store.get_unresolved_errors()
        metrics["unresolved_errors"] = len(errors)
        if errors:
            blockers.append(f"미해결 에러 {len(errors)}건 존재")

        pending = self._state_store.get_pending_pushes()
        total_lag = len(pending)
        metrics["total_lag"] = total_lag
        if total_lag > 0:
            blockers.append(f"미완료 push {total_lag}건 (total_lag={total_lag})")

        if self._event_consumer is not None:
            redis_info = self._event_consumer.get_stream_info()
            pending_msgs = redis_info.get("pending_messages", 0)
            if pending_msgs > 0:
                metrics["total_lag"] += pending_msgs
                blockers.append(f"Redis pending 메시지 {pending_msgs}건")

        if self._circuit_breaker is not None:
            if not self._circuit_breaker.allow_sync():
                metrics["integrity_passed"] = False
                failure = self._circuit_breaker.last_failure
                detail = ""
                if failure:
                    detail = f" ({len(failure.mismatched_files)}개 파일 불일치)"
                blockers.append(f"무결성 검증 실패{detail}")
        # circuit_breaker 미설정 시 integrity_passed=True 유지

        if self._last_sync_time is not None:
            elapsed = (datetime.now() - self._last_sync_time).total_seconds()
            metrics["last_sync_seconds_ago"] = elapsed
            if elapsed > 300:  # 5분
                blockers.append(
                    f"마지막 동기화가 {elapsed:.0f}초 전 (5분 초과)"
                )
        else:
            blockers.append("동기화 기록 없음")

        return blockers, metrics

    def start_in_thread(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            self.app, host=self._host, port=self._port, log_level="warning",
        )
        server = uvicorn.Server(config)
        self._thread = threading.Thread(target=server.run, daemon=True)
        self._thread.start()
        logger.info("API 서버 시작: %s:%d", self._host, self._port)
