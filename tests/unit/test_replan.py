"""Unit tests for ReplanTrigger — PLAT-1625.

Tests PV change, EV connect, SoC deviation, cooldown.
All thresholds via named constants.
"""

from __future__ import annotations

from unittest.mock import patch

from core.day_plan import BatMode, DayPlan, HourSlot, HourlyForecast
from core.replan import ReplanConfig, ReplanTrigger

# ---------------------------------------------------------------------------
# Named test constants
# ---------------------------------------------------------------------------

_PV_CHANGE_THRESHOLD: float = 0.20
_SOC_DEVIATION_PCT: float = 10.0
_COOLDOWN_S: int = 900

_HOUR_10: int = 10
_SOC_60: float = 60.0
_SOC_75: float = 75.0
_SOC_50: float = 50.0

_PV_NORMAL: float = 3.0
_PV_DROP_30PCT: float = 2.0  # 33% drop from 3.0
_PV_SLIGHT_CHANGE: float = 2.7  # 10% drop — below threshold

_HOUSE_LOAD_W: float = 2500.0
_BAT_ALLOC_W: float = 500.0
_ZERO_W: float = 0.0


def _cfg() -> ReplanConfig:
    return ReplanConfig(
        pv_change_threshold=_PV_CHANGE_THRESHOLD,
        soc_deviation_pct=_SOC_DEVIATION_PCT,
        cooldown_s=_COOLDOWN_S,
    )


def _pv(kwh_per_hour: float) -> dict[int, HourlyForecast]:
    return {
        h: HourlyForecast(
            p10_kwh=kwh_per_hour * 0.7,
            p50_kwh=kwh_per_hour,
            p90_kwh=kwh_per_hour * 1.3,
        )
        for h in range(6, 22)
    }


def _plan(projected_soc: float = _SOC_60) -> DayPlan:
    """Create a minimal DayPlan with a slot at _HOUR_10."""
    slot = HourSlot(
        hour=_HOUR_10,
        pv_forecast_w=_HOUSE_LOAD_W + _BAT_ALLOC_W,
        house_load_w=_HOUSE_LOAD_W,
        bat_alloc_w=_BAT_ALLOC_W,
        ev_alloc_w=_ZERO_W,
        dispatch_alloc_w=_ZERO_W,
        expected_export_w=_ZERO_W,
        bat_mode=BatMode.CHARGE,
        projected_bat_soc_pct=projected_soc,
    )
    total_export = slot.expected_export_w / 1000.0
    return DayPlan(
        slots={_HOUR_10: slot},
        total_expected_export_kwh=total_export,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReplanTrigger:
    """Tests for ReplanTrigger."""

    def test_no_plan_triggers_replan(self) -> None:
        """No existing plan → always replan."""
        trigger = ReplanTrigger(_cfg())
        should, reason = trigger.should_replan(
            current_plan=None,
            pv_hourly=_pv(_PV_NORMAL),
            ev_connected=False,
            bat_soc_pct=_SOC_60,
            current_hour=_HOUR_10,
        )
        assert should is True
        assert "No existing plan" in reason

    def test_pv_change_triggers_replan(self) -> None:
        """PV drop > 20% → replan."""
        trigger = ReplanTrigger(_cfg())
        plan = _plan()

        # First call establishes baseline
        trigger.update_tracking(_pv(_PV_NORMAL), ev_connected=False)

        # Simulate time passing (past cooldown)
        with patch("time.monotonic", return_value=_COOLDOWN_S + 1.0):
            trigger._last_replan_time = 0.0
            should, reason = trigger.should_replan(
                current_plan=plan,
                pv_hourly=_pv(_PV_DROP_30PCT),
                ev_connected=False,
                bat_soc_pct=_SOC_60,
                current_hour=_HOUR_10,
            )

        assert should is True
        assert "PV change" in reason

    def test_small_pv_change_no_replan(self) -> None:
        """PV change < 20% → no replan."""
        trigger = ReplanTrigger(_cfg())
        plan = _plan()

        trigger.update_tracking(_pv(_PV_NORMAL), ev_connected=False)

        with patch("time.monotonic", return_value=_COOLDOWN_S + 1.0):
            trigger._last_replan_time = 0.0
            should, _ = trigger.should_replan(
                current_plan=plan,
                pv_hourly=_pv(_PV_SLIGHT_CHANGE),
                ev_connected=False,
                bat_soc_pct=_SOC_60,
                current_hour=_HOUR_10,
            )

        assert should is False

    def test_ev_connect_triggers_replan(self) -> None:
        """EV connects → replan."""
        trigger = ReplanTrigger(_cfg())
        plan = _plan()

        trigger.update_tracking(_pv(_PV_NORMAL), ev_connected=False)

        with patch("time.monotonic", return_value=_COOLDOWN_S + 1.0):
            trigger._last_replan_time = 0.0
            should, reason = trigger.should_replan(
                current_plan=plan,
                pv_hourly=_pv(_PV_NORMAL),
                ev_connected=True,  # Changed!
                bat_soc_pct=_SOC_60,
                current_hour=_HOUR_10,
            )

        assert should is True
        assert "EV connected" in reason

    def test_soc_drift_triggers_replan(self) -> None:
        """SoC deviates > 10% from plan → replan."""
        trigger = ReplanTrigger(_cfg())
        plan = _plan(projected_soc=_SOC_75)

        trigger.update_tracking(_pv(_PV_NORMAL), ev_connected=False)

        with patch("time.monotonic", return_value=_COOLDOWN_S + 1.0):
            trigger._last_replan_time = 0.0
            should, reason = trigger.should_replan(
                current_plan=plan,
                pv_hourly=_pv(_PV_NORMAL),
                ev_connected=False,
                bat_soc_pct=_SOC_50,  # 25% off from 75%
                current_hour=_HOUR_10,
            )

        assert should is True
        assert "SoC deviation" in reason

    def test_cooldown_prevents_rapid_replan(self) -> None:
        """Two triggers within cooldown → only first fires."""
        trigger = ReplanTrigger(_cfg())

        # First replan (no plan)
        should, _ = trigger.should_replan(
            current_plan=None,
            pv_hourly=_pv(_PV_NORMAL),
            ev_connected=False,
            bat_soc_pct=_SOC_60,
            current_hour=_HOUR_10,
        )
        assert should is True

        plan = _plan()
        trigger.update_tracking(_pv(_PV_NORMAL), ev_connected=False)

        # Second trigger immediately — blocked by cooldown
        should, _ = trigger.should_replan(
            current_plan=plan,
            pv_hourly=_pv(_PV_DROP_30PCT),
            ev_connected=True,
            bat_soc_pct=_SOC_50,
            current_hour=_HOUR_10,
        )
        assert should is False
