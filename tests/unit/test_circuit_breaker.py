"""Tests for adapters.circuit_breaker (PLAT-1707)."""

from __future__ import annotations

from adapters.circuit_breaker import CircuitBreaker, CircuitBreakerConfig


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _cb(failure_threshold: int = 3, cooldown_s: float = 10.0) -> tuple[CircuitBreaker, _FakeClock]:
    clock = _FakeClock()
    cb = CircuitBreaker(
        name="test",
        config=CircuitBreakerConfig(
            failure_threshold=failure_threshold,
            cooldown_s=cooldown_s,
            success_threshold=1,
        ),
        clock=clock,
    )
    return cb, clock


def test_closed_breaker_allows_calls() -> None:
    cb, _ = _cb()
    assert cb.allow() is True


def test_consecutive_failures_trip_breaker() -> None:
    cb, _ = _cb(failure_threshold=3)
    for _ in range(2):
        cb.on_failure()
    assert cb.state == "closed"
    cb.on_failure()
    assert cb.state == "open"
    assert cb.allow() is False


def test_success_resets_failure_count() -> None:
    cb, _ = _cb(failure_threshold=3)
    cb.on_failure()
    cb.on_failure()
    cb.on_success()
    assert cb.consecutive_failures == 0
    # Next two failures must NOT trip — count restarted.
    cb.on_failure()
    cb.on_failure()
    assert cb.state == "closed"


def test_open_transitions_to_half_open_after_cooldown() -> None:
    cb, clock = _cb(failure_threshold=2, cooldown_s=10.0)
    cb.on_failure(); cb.on_failure()
    assert cb.state == "open"
    assert cb.allow() is False

    clock.t = 9.9
    assert cb.allow() is False
    clock.t = 10.0
    assert cb.allow() is True
    assert cb.state == "half_open"


def test_half_open_success_closes_breaker() -> None:
    cb, clock = _cb(failure_threshold=2, cooldown_s=5.0)
    cb.on_failure(); cb.on_failure()
    clock.t = 5.0
    cb.allow()                    # → half_open
    cb.on_success()
    assert cb.state == "closed"
    assert cb.consecutive_failures == 0


def test_half_open_failure_reopens_with_fresh_cooldown() -> None:
    cb, clock = _cb(failure_threshold=2, cooldown_s=5.0)
    cb.on_failure(); cb.on_failure()
    clock.t = 5.0
    cb.allow()
    cb.on_failure()               # probe fails
    assert cb.state == "open"
    assert cb.opened_at == 5.0    # cooldown reset
    # Still blocked until 5.0 + 5.0
    clock.t = 9.0
    assert cb.allow() is False
    clock.t = 10.0
    assert cb.allow() is True


def test_success_threshold_requires_multiple_probes() -> None:
    """Configurable: 2 consecutive successes before closing."""
    clock = _FakeClock()
    cb = CircuitBreaker(
        name="strict",
        config=CircuitBreakerConfig(
            failure_threshold=1, cooldown_s=1.0, success_threshold=2,
        ),
        clock=clock,
    )
    cb.on_failure()
    assert cb.state == "open"
    clock.t = 1.0
    cb.allow()                    # half_open
    cb.on_success()
    assert cb.state == "half_open"
    cb.on_success()
    assert cb.state == "closed"


def test_snapshot_returns_current_state() -> None:
    cb, _ = _cb()
    snap = cb.snapshot()
    assert snap["name"] == "test"
    assert snap["state"] == "closed"
    assert snap["consecutive_failures"] == 0
