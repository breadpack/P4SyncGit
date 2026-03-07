from __future__ import annotations

import logging
import time
from enum import Enum

from p4gitsync.notifications.notifier import SlackNotifier
from p4gitsync.services.integrity_checker import IntegrityChecker, IntegrityResult

logger = logging.getLogger("p4gitsync.circuit_breaker")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"


class IntegrityCircuitBreaker:
    """무결성 검증 실패 시 동기화를 자동 중단하는 circuit breaker.

    - CLOSED: 정상 상태, 동기화 허용
    - OPEN: 무결성 실패 감지, 동기화 중단 + Slack ERROR 알림
    """

    def __init__(
        self,
        integrity_checker: IntegrityChecker,
        notifier: SlackNotifier | None = None,
    ) -> None:
        self._checker = integrity_checker
        self._notifier = notifier
        self._state = CircuitState.CLOSED
        self._last_failure_result: IntegrityResult | None = None
        self._opened_at: float = 0.0

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def last_failure(self) -> IntegrityResult | None:
        return self._last_failure_result

    def allow_sync(self) -> bool:
        """동기화를 허용해도 되는지 반환. OPEN이면 False."""
        return self._state == CircuitState.CLOSED

    def run_check(self) -> IntegrityResult | None:
        """스케줄된 무결성 검증을 실행하고 결과에 따라 상태를 전환한다."""
        result = self._checker.run_scheduled_check()
        if result is None:
            return None

        if result.passed:
            if self._state == CircuitState.OPEN:
                logger.info("무결성 검증 통과, circuit breaker CLOSED로 복구")
                self._state = CircuitState.CLOSED
                self._last_failure_result = None
        else:
            self._trip(result)

        return result

    def reset(self) -> None:
        """수동으로 circuit breaker를 CLOSED 상태로 복구한다."""
        if self._state == CircuitState.OPEN:
            logger.info("Circuit breaker 수동 리셋")
            self._state = CircuitState.CLOSED
            self._last_failure_result = None

    def _trip(self, result: IntegrityResult) -> None:
        """무결성 검증 실패 시 OPEN으로 전환."""
        self._state = CircuitState.OPEN
        self._last_failure_result = result
        self._opened_at = time.time()

        details = (
            f"스케줄: {result.schedule}, "
            f"검사 파일: {result.checked_files}개, "
            f"불일치: {len(result.mismatched_files)}개\n"
            f"불일치 파일: {', '.join(result.mismatched_files[:10])}"
        )
        logger.error("Circuit breaker OPEN: %s", details)

        if self._notifier:
            self._notifier.send_integrity_failure("sync", details)
