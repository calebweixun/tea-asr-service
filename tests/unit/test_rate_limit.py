from __future__ import annotations

from tea_asr.rate_limit import AuthRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_a_source_is_not_blocked_before_any_failure() -> None:
    limiter = AuthRateLimiter()
    assert limiter.is_blocked("1.2.3.4") is False


def test_a_source_is_blocked_once_the_failure_budget_is_exhausted() -> None:
    clock = FakeClock()
    limiter = AuthRateLimiter(max_failures=3, window_s=60.0, clock=clock)
    for _ in range(3):
        limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is True


def test_failures_below_the_budget_do_not_block() -> None:
    clock = FakeClock()
    limiter = AuthRateLimiter(max_failures=3, window_s=60.0, clock=clock)
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is False


def test_a_success_clears_the_failure_window() -> None:
    clock = FakeClock()
    limiter = AuthRateLimiter(max_failures=3, window_s=60.0, clock=clock)
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    limiter.record_success("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is False


def test_the_window_expires_and_unblocks_the_source() -> None:
    clock = FakeClock()
    limiter = AuthRateLimiter(max_failures=2, window_s=60.0, clock=clock)
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is True
    clock.advance(61.0)
    assert limiter.is_blocked("1.2.3.4") is False


def test_sources_are_tracked_independently() -> None:
    clock = FakeClock()
    limiter = AuthRateLimiter(max_failures=2, window_s=60.0, clock=clock)
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is True
    assert limiter.is_blocked("5.6.7.8") is False


def test_tracked_sources_are_bounded_and_evict_oldest_first() -> None:
    clock = FakeClock()
    limiter = AuthRateLimiter(max_failures=1, window_s=60.0, max_tracked=2, clock=clock)
    limiter.record_failure("a")
    limiter.record_failure("b")
    limiter.record_failure("c")  # evicts "a"
    assert limiter.is_blocked("a") is False
    assert limiter.is_blocked("b") is True
    assert limiter.is_blocked("c") is True
