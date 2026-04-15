"""Unit tests for DayPlan models — PLAT-1629.

Tests energy balance invariant, frozen immutability, BatMode enum,
and DayPlan aggregate validation.
"""

from __future__ import annotations

import pytest

from core.day_plan import (
    BatMode,
    DayPlan,
    HourSlot,
    HourlyForecast,
    ZERO_HOURLY_FORECAST,
    _ENERGY_BALANCE_TOLERANCE_W,
    _W_TO_KW,
)

# ---------------------------------------------------------------------------
# Named test constants — no naked literals
# ---------------------------------------------------------------------------

_TEST_HOUR: int = 10
_PV_FORECAST_W: float = 5000.0
_HOUSE_LOAD_W: float = 800.0
_BAT_ALLOC_W: float = 2000.0
_EV_ALLOC_W: float = 1500.0
_DISPATCH_ALLOC_W: float = 500.0
_EXPORT_W: float = 200.0
_EV_AMPS: int = 6
_SOC_PCT: float = 65.0

_WINDOW_START_H: int = 6
_WINDOW_END_H: int = 22
_WINDOW_HOURS: int = _WINDOW_END_H - _WINDOW_START_H

_P10_KWH: float = 0.3
_P50_KWH: float = 0.5
_P90_KWH: float = 0.8

_BAD_P10_KWH: float = 0.8
_BAD_P50_KWH: float = 0.5

_BAT_TARGET_SOC: float = 100.0
_EV_TARGET_SOC: float = 75.0


def _make_slot(
    hour: int = _TEST_HOUR,
    pv_w: float = _PV_FORECAST_W,
    house_w: float = _HOUSE_LOAD_W,
    bat_w: float = _BAT_ALLOC_W,
    ev_w: float = _EV_ALLOC_W,
    dispatch_w: float = _DISPATCH_ALLOC_W,
    export_w: float = _EXPORT_W,
    bat_mode: BatMode = BatMode.CHARGE,
    ev_amps: int = _EV_AMPS,
    soc_pct: float = _SOC_PCT,
    dispatch_devices: tuple[str, ...] = (),
) -> HourSlot:
    """Helper to create a balanced HourSlot."""
    return HourSlot(
        hour=hour,
        pv_forecast_w=pv_w,
        house_load_w=house_w,
        bat_alloc_w=bat_w,
        ev_alloc_w=ev_w,
        dispatch_alloc_w=dispatch_w,
        expected_export_w=export_w,
        bat_mode=bat_mode,
        ev_amps=ev_amps,
        dispatch_devices=dispatch_devices,
        projected_bat_soc_pct=soc_pct,
    )


# ---------------------------------------------------------------------------
# HourlyForecast tests
# ---------------------------------------------------------------------------


class TestHourlyForecast:
    """Tests for HourlyForecast dataclass."""

    def test_normal_data_preserved(self) -> None:
        """p10 <= p50 <= p90 → values kept as-is."""
        f = HourlyForecast(p10_kwh=_P10_KWH, p50_kwh=_P50_KWH, p90_kwh=_P90_KWH)
        assert f.p10_kwh == _P10_KWH
        assert f.p50_kwh == _P50_KWH
        assert f.p90_kwh == _P90_KWH

    def test_p10_clamped_when_bad_data(self) -> None:
        """p10 > p50 → p10 clamped to p50."""
        f = HourlyForecast(p10_kwh=_BAD_P10_KWH, p50_kwh=_BAD_P50_KWH, p90_kwh=_P90_KWH)
        assert f.p10_kwh == _BAD_P50_KWH
        assert f.p50_kwh == _BAD_P50_KWH

    def test_p90_clamped_when_bad_data(self) -> None:
        """p90 < p50 → p90 raised to p50."""
        _LOW_P90: float = 0.3
        f = HourlyForecast(p10_kwh=_P10_KWH, p50_kwh=_P50_KWH, p90_kwh=_LOW_P90)
        assert f.p90_kwh == _P50_KWH

    def test_zero_forecast_sentinel(self) -> None:
        """ZERO_HOURLY_FORECAST has all zeros."""
        assert ZERO_HOURLY_FORECAST.p10_kwh == 0.0
        assert ZERO_HOURLY_FORECAST.p50_kwh == 0.0
        assert ZERO_HOURLY_FORECAST.p90_kwh == 0.0

    def test_frozen(self) -> None:
        """HourlyForecast is immutable."""
        f = HourlyForecast(p10_kwh=_P10_KWH, p50_kwh=_P50_KWH, p90_kwh=_P90_KWH)
        with pytest.raises(AttributeError):
            f.p50_kwh = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BatMode tests
# ---------------------------------------------------------------------------


class TestBatMode:
    """Tests for BatMode enum."""

    def test_charge_value(self) -> None:
        assert BatMode.CHARGE.value == "charge"

    def test_discharge_value(self) -> None:
        assert BatMode.DISCHARGE.value == "discharge"

    def test_standby_value(self) -> None:
        assert BatMode.STANDBY.value == "standby"

    def test_string_serializable(self) -> None:
        """BatMode inherits from str for JSON serialization."""
        assert isinstance(BatMode.CHARGE, str)


# ---------------------------------------------------------------------------
# HourSlot tests
# ---------------------------------------------------------------------------


class TestHourSlot:
    """Tests for HourSlot energy balance invariant."""

    def test_energy_balance_valid(self) -> None:
        """Balanced slot passes validation."""
        slot = _make_slot()
        assert slot.hour == _TEST_HOUR
        assert slot.bat_mode == BatMode.CHARGE

    def test_energy_balance_violation_raises(self) -> None:
        """Unbalanced slot raises ValueError."""
        _BAD_EXPORT_W: float = 210.0
        with pytest.raises(ValueError, match="energy balance violated"):
            _make_slot(export_w=_BAD_EXPORT_W)

    def test_energy_balance_within_tolerance(self) -> None:
        """Slot within tolerance passes."""
        _TOLERANCE_EXPORT_W: float = _EXPORT_W + _ENERGY_BALANCE_TOLERANCE_W
        # This should pass — exactly at tolerance boundary
        slot = _make_slot(export_w=_TOLERANCE_EXPORT_W)
        assert slot.expected_export_w == _TOLERANCE_EXPORT_W

    def test_frozen_immutable(self) -> None:
        """HourSlot cannot be modified after creation."""
        slot = _make_slot()
        with pytest.raises(AttributeError):
            slot.bat_alloc_w = 999.0  # type: ignore[misc]

    def test_to_dict(self) -> None:
        """to_dict returns all fields."""
        slot = _make_slot(dispatch_devices=("miner",))
        d = slot.to_dict()
        assert d["hour"] == _TEST_HOUR
        assert d["bat_mode"] == "charge"
        assert d["dispatch_devices"] == ["miner"]
        assert d["ev_amps"] == _EV_AMPS

    def test_zero_pv_zero_alloc(self) -> None:
        """Zero PV hour with zero allocations passes balance."""
        _ZERO: float = 0.0
        slot = HourSlot(
            hour=_TEST_HOUR,
            pv_forecast_w=_ZERO,
            house_load_w=_ZERO,
            bat_alloc_w=_ZERO,
            ev_alloc_w=_ZERO,
            dispatch_alloc_w=_ZERO,
            expected_export_w=_ZERO,
            bat_mode=BatMode.STANDBY,
        )
        assert slot.pv_forecast_w == _ZERO


# ---------------------------------------------------------------------------
# DayPlan tests
# ---------------------------------------------------------------------------


def _make_day_plan(
    hours: range | None = None,
    export_per_hour_w: float = _EXPORT_W,
) -> DayPlan:
    """Helper to create a DayPlan covering the standard window."""
    if hours is None:
        hours = range(_WINDOW_START_H, _WINDOW_END_H)
    slots: dict[int, HourSlot] = {}
    for h in hours:
        slots[h] = _make_slot(hour=h, export_w=export_per_hour_w)
    total_export = sum(s.expected_export_w for s in slots.values()) / _W_TO_KW
    return DayPlan(
        slots=slots,
        bat_target_soc_pct=_BAT_TARGET_SOC,
        ev_target_soc_pct=_EV_TARGET_SOC,
        total_expected_export_kwh=total_export,
    )


class TestDayPlan:
    """Tests for DayPlan aggregate."""

    def test_covers_all_hours(self) -> None:
        """DayPlan covers the full 06-22 window."""
        plan = _make_day_plan()
        assert len(plan.slots) == _WINDOW_HOURS
        assert plan.window_hours == list(range(_WINDOW_START_H, _WINDOW_END_H))

    def test_total_export_matches_sum(self) -> None:
        """total_expected_export_kwh matches sum of slot exports."""
        plan = _make_day_plan()
        expected = _EXPORT_W * _WINDOW_HOURS / _W_TO_KW
        assert abs(plan.total_expected_export_kwh - expected) < 0.001

    def test_total_export_mismatch_raises(self) -> None:
        """Mismatched total_expected_export_kwh raises ValueError."""
        slots = {_TEST_HOUR: _make_slot()}
        _BAD_TOTAL: float = 999.0
        with pytest.raises(ValueError, match="total_expected_export_kwh mismatch"):
            DayPlan(slots=slots, total_expected_export_kwh=_BAD_TOTAL)

    def test_get_slot_returns_correct_hour(self) -> None:
        """get_slot returns the right HourSlot."""
        plan = _make_day_plan()
        slot = plan.get_slot(_WINDOW_START_H)
        assert slot is not None
        assert slot.hour == _WINDOW_START_H

    def test_get_slot_outside_window_returns_none(self) -> None:
        """get_slot returns None for hours outside the plan window."""
        plan = _make_day_plan()
        _OUTSIDE_HOUR: int = 3
        assert plan.get_slot(_OUTSIDE_HOUR) is None

    def test_frozen_immutable(self) -> None:
        """DayPlan cannot be modified after creation."""
        plan = _make_day_plan()
        with pytest.raises(AttributeError):
            plan.can_discharge_fm = True  # type: ignore[misc]

    def test_to_dict(self) -> None:
        """to_dict returns serializable structure."""
        plan = _make_day_plan()
        d = plan.to_dict()
        assert len(d["slots"]) == _WINDOW_HOURS
        assert d["bat_target_soc_pct"] == _BAT_TARGET_SOC
        assert isinstance(d["created_at"], str)

    def test_empty_plan_valid(self) -> None:
        """Empty DayPlan (no slots) is valid with 0 export."""
        _ZERO_EXPORT: float = 0.0
        plan = DayPlan(slots={}, total_expected_export_kwh=_ZERO_EXPORT)
        assert len(plan.slots) == 0
        assert plan.total_expected_export_kwh == _ZERO_EXPORT
