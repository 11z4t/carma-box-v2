"""Tests for core/health.py — SLO-aware HealthModel (PLAT-1601).

All numeric literals in this file are named constants (nolltolerans magic numbers).
"""

from __future__ import annotations

import logging

import pytest

from core.health import (
    CYCLE_SUCCESS_RATE_TARGET,
    HEALTH_LOG_INTERVAL_S,
    MAX_DEGRADED_SECONDS_PER_HOUR,
    MIN_CYCLES_FOR_SLO,
    SECONDS_PER_HOUR,
    HealthModel,
)

# ── Test-local named constants (nolltolerans magic numbers) ───────────────────

_SMALL_BATCH: int = MIN_CYCLES_FOR_SLO          # exactly the SLO evaluation threshold
_ONE_FAILURE: int = 1
_ZERO_FAILURES: int = 0
_ZERO_ISSUED: int = 0
_ONE_CMD: int = 1
_HALF_HOUR: float = SECONDS_PER_HOUR / 2
_TWO_HOURS: float = SECONDS_PER_HOUR * 2
_TINY_DELTA: float = 0.001
_PAST_INTERVAL: float = HEALTH_LOG_INTERVAL_S + _TINY_DELTA
_BEFORE_INTERVAL: float = HEALTH_LOG_INTERVAL_S - _TINY_DELTA
_ONE_SECOND: float = 1.0
_HALF_SECOND: float = 0.5


# ── SLO constant tests ────────────────────────────────────────────────────────


class TestSloConstants:
    def test_cycle_success_rate_target_value(self) -> None:
        """CYCLE_SUCCESS_RATE_TARGET must be 0.99."""
        assert CYCLE_SUCCESS_RATE_TARGET == 0.99  # noqa: PLR2004

    def test_cycle_success_rate_target_is_float(self) -> None:
        assert isinstance(CYCLE_SUCCESS_RATE_TARGET, float)

    def test_max_degraded_seconds_per_hour_value(self) -> None:
        """MAX_DEGRADED_SECONDS_PER_HOUR must be 120."""
        assert MAX_DEGRADED_SECONDS_PER_HOUR == 120  # noqa: PLR2004

    def test_max_degraded_seconds_per_hour_is_int(self) -> None:
        assert isinstance(MAX_DEGRADED_SECONDS_PER_HOUR, int)

    def test_health_log_interval_is_900(self) -> None:
        """HEALTH_LOG_INTERVAL_S must be 900 (15 min)."""
        assert HEALTH_LOG_INTERVAL_S == 900  # noqa: PLR2004

    def test_seconds_per_hour_is_3600(self) -> None:
        assert SECONDS_PER_HOUR == 3_600  # noqa: PLR2004


# ── record_cycle ──────────────────────────────────────────────────────────────


class TestRecordCycle:
    def test_increments_cycles_total(self) -> None:
        m = HealthModel()
        m.record_cycle()
        assert m.cycles_total == _ONE_FAILURE

    def test_failed_increments_cycles_failed(self) -> None:
        m = HealthModel()
        m.record_cycle(failed=True)
        assert m.cycles_failed == _ONE_FAILURE
        assert m.cycles_total == _ONE_FAILURE

    def test_overrun_increments_cycles_overrun(self) -> None:
        m = HealthModel()
        m.record_cycle(overrun=True)
        assert m.cycles_overrun == _ONE_FAILURE

    def test_success_does_not_increment_failed(self) -> None:
        m = HealthModel()
        m.record_cycle(failed=False)
        assert m.cycles_failed == _ZERO_FAILURES

    def test_multiple_cycles_accumulate(self) -> None:
        m = HealthModel()
        for _ in range(_SMALL_BATCH):
            m.record_cycle()
        assert m.cycles_total == _SMALL_BATCH


# ── record_commands ───────────────────────────────────────────────────────────


class TestRecordCommands:
    def test_increments_commands_issued(self) -> None:
        m = HealthModel()
        m.record_commands(issued=_ONE_CMD, failed=_ZERO_FAILURES)
        assert m.commands_issued == _ONE_CMD

    def test_increments_commands_failed(self) -> None:
        m = HealthModel()
        m.record_commands(issued=_ONE_CMD, failed=_ONE_FAILURE)
        assert m.commands_failed == _ONE_FAILURE

    def test_accumulates_across_calls(self) -> None:
        m = HealthModel()
        m.record_commands(issued=_ONE_CMD, failed=_ZERO_FAILURES)
        m.record_commands(issued=_ONE_CMD, failed=_ONE_FAILURE)
        assert m.commands_issued == _ONE_CMD + _ONE_CMD
        assert m.commands_failed == _ONE_FAILURE


# ── add_degraded_seconds ──────────────────────────────────────────────────────


class TestAddDegradedSeconds:
    def test_accumulates(self) -> None:
        m = HealthModel()
        m.add_degraded_seconds(_ONE_SECOND)
        m.add_degraded_seconds(_HALF_SECOND)
        assert m.degraded_mode_seconds == pytest.approx(_ONE_SECOND + _HALF_SECOND)


# ── is_healthy ────────────────────────────────────────────────────────────────


class TestIsHealthy:
    def test_fresh_model_is_healthy(self) -> None:
        """A new model with no data is healthy (below MIN_CYCLES_FOR_SLO)."""
        m = HealthModel()
        healthy, reason = m.is_healthy()
        assert healthy is True
        assert reason == "healthy"

    def test_below_min_cycles_skips_rate_check(self) -> None:
        """Under MIN_CYCLES_FOR_SLO cycles, success-rate SLO is not evaluated."""
        m = HealthModel()
        for _ in range(MIN_CYCLES_FOR_SLO - _ONE_FAILURE):
            m.record_cycle(failed=True)
        healthy, _ = m.is_healthy()
        assert healthy is True

    def test_cycle_failure_rate_too_high_is_unhealthy(self) -> None:
        """Failing more than (1 - TARGET) of cycles triggers unhealthy."""
        m = HealthModel()
        for _ in range(_SMALL_BATCH):
            m.record_cycle(failed=True)
        healthy, reason = m.is_healthy()
        assert healthy is False
        assert "success rate" in reason

    def test_exactly_at_target_is_healthy(self) -> None:
        """Exactly CYCLE_SUCCESS_RATE_TARGET fraction succeeding is healthy."""
        m = HealthModel()
        # 99 successes + 1 failure = 99% = exactly at target
        _SUCCESS_COUNT: int = 99
        _FAIL_COUNT: int = 1
        for _ in range(_SUCCESS_COUNT):
            m.record_cycle(failed=False)
        for _ in range(_FAIL_COUNT):
            m.record_cycle(failed=True)
        healthy, _ = m.is_healthy()
        assert healthy is True

    def test_degraded_within_limit_is_healthy(self) -> None:
        """Degraded seconds within the hourly limit: healthy."""
        m = HealthModel()
        m._session_start = m._session_start - _TWO_HOURS
        m.add_degraded_seconds(float(MAX_DEGRADED_SECONDS_PER_HOUR - _ONE_FAILURE))
        healthy, _ = m.is_healthy()
        assert healthy is True

    def test_degraded_exceeds_limit_is_unhealthy(self) -> None:
        """Degraded seconds exceeding the hourly limit: unhealthy."""
        m = HealthModel()
        # Simulate 2h uptime with > MAX_DEGRADED_SECONDS_PER_HOUR * 2 degraded seconds
        m._session_start = m._session_start - _TWO_HOURS
        _EXCESS_DEGRADED: float = float(MAX_DEGRADED_SECONDS_PER_HOUR) * _TWO_HOURS
        m.add_degraded_seconds(_EXCESS_DEGRADED)
        healthy, reason = m.is_healthy()
        assert healthy is False
        assert "degraded mode" in reason

    def test_returns_tuple_bool_str(self) -> None:
        """is_healthy() must return (bool, str)."""
        m = HealthModel()
        result = m.is_healthy()
        assert isinstance(result, tuple)
        assert len(result) == 2  # noqa: PLR2004
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)


# ── maybe_log_summary ─────────────────────────────────────────────────────────


class TestMaybeLogSummary:
    def test_does_not_log_before_interval(self) -> None:
        """No log emitted before HEALTH_LOG_INTERVAL_S seconds have elapsed."""
        m = HealthModel()
        now = m._last_log_time + _BEFORE_INTERVAL
        result = m.maybe_log_summary(now=now)
        assert result is False

    def test_logs_after_interval(self, caplog: pytest.LogCaptureFixture) -> None:
        """Log entry emitted once HEALTH_LOG_INTERVAL_S seconds have elapsed."""
        m = HealthModel()
        now = m._last_log_time + _PAST_INTERVAL
        with caplog.at_level(logging.INFO, logger="core.health"):
            result = m.maybe_log_summary(now=now)
        assert result is True
        assert "[health]" in caplog.text

    def test_updates_last_log_time(self) -> None:
        """After logging, _last_log_time is updated to prevent repeated logging."""
        m = HealthModel()
        now = m._last_log_time + _PAST_INTERVAL
        m.maybe_log_summary(now=now)
        assert m._last_log_time == now

    def test_does_not_log_twice_in_quick_succession(self) -> None:
        """Second call immediately after first does not log again."""
        m = HealthModel()
        now = m._last_log_time + _PAST_INTERVAL
        m.maybe_log_summary(now=now)
        result = m.maybe_log_summary(now=now + _TINY_DELTA)
        assert result is False
