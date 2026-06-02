"""IntegrityCircuitBreaker 상태 전환 테스트 (가짜 checker/notifier)."""

from p4gitsync.services.circuit_breaker import CircuitState, IntegrityCircuitBreaker
from p4gitsync.services.integrity_checker import IntegrityResult


class _FakeChecker:
    """run_scheduled_check 가 미리 지정한 결과를 순서대로 반환."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def run_scheduled_check(self):
        if self.calls >= len(self._results):
            return None
        r = self._results[self.calls]
        self.calls += 1
        return r


class _FakeNotifier:
    def __init__(self):
        self.failures = []

    def send_integrity_failure(self, source, details):
        self.failures.append((source, details))


def _passed():
    return IntegrityResult(passed=True, checked_files=5, schedule="daily_sample")


def _failed():
    return IntegrityResult(
        passed=False, checked_files=5, mismatched_files=["a.bin"],
        schedule="weekly_full",
    )


class TestCircuitBreaker:
    def test_starts_closed_and_allows_sync(self):
        cb = IntegrityCircuitBreaker(_FakeChecker([]))
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_sync() is True

    def test_pass_keeps_closed(self):
        cb = IntegrityCircuitBreaker(_FakeChecker([_passed()]))
        result = cb.run_check()
        assert result.passed is True
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_sync() is True

    def test_failure_trips_open_and_blocks_sync(self):
        notifier = _FakeNotifier()
        cb = IntegrityCircuitBreaker(_FakeChecker([_failed()]), notifier=notifier)
        cb.run_check()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_sync() is False
        assert cb.last_failure is not None
        assert len(notifier.failures) == 1   # Slack ERROR 알림 1회

    def test_recovers_on_subsequent_pass(self):
        cb = IntegrityCircuitBreaker(_FakeChecker([_failed(), _passed()]))
        cb.run_check()
        assert cb.state == CircuitState.OPEN
        cb.run_check()
        assert cb.state == CircuitState.CLOSED
        assert cb.last_failure is None

    def test_manual_reset(self):
        cb = IntegrityCircuitBreaker(_FakeChecker([_failed()]))
        cb.run_check()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_sync() is True

    def test_no_schedule_due_returns_none(self):
        cb = IntegrityCircuitBreaker(_FakeChecker([]))   # run_scheduled_check→None
        assert cb.run_check() is None
        assert cb.state == CircuitState.CLOSED
