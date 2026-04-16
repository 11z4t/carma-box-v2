"""Unit tests for core.night_planner (PLAT-1674)."""

from __future__ import annotations

from datetime import datetime

import pytest

from core.night_planner import (
    NightPlannerConfig,
    NightPlannerInput,
    is_in_window,
    plan_night,
)


# Reference times
WEEKDAY_22_00 = datetime(2026, 4, 16, 22, 0, 0)   # Thursday 22:00


@pytest.fixture
def cfg() -> NightPlannerConfig:
    return NightPlannerConfig()


def _input(
    *,
    bat_soc: float = 50.0,
    bat_cap: float = 20.0,
    ev_soc: float = 50.0,
    ev_target: float = 100.0,
    ev_cap: float = 90.0,
    pv_tomorrow: float = 15.0,
    presence: str | None = None,
    now: datetime = WEEKDAY_22_00,
) -> NightPlannerInput:
    return NightPlannerInput(
        now=now,
        bat_total_soc_pct=bat_soc,
        bat_total_cap_kwh=bat_cap,
        ev_soc_pct=ev_soc,
        ev_target_soc_pct=ev_target,
        ev_battery_kwh=ev_cap,
        pv_forecast_tomorrow_kwh=pv_tomorrow,
        ev_device_tracker_state=presence,
    )


def test_pure_ev_focus_high_pv_weekend(cfg: NightPlannerConfig) -> None:
    """High PV tomorrow + weekend → mer PV till EV imorgon, kortare ev_window inatt.

    Choice A (per 901 QC sweep 22:24): bat_target_soc=100 default → 80% bat
    har 20pp deficit = 4 kWh grid behövs. Detta är KORREKT beteende —
    helg fm prio till EV, bat tar grid efter EV-fönster.
    """
    sat_22 = datetime(2026, 4, 18, 22, 0, 0)  # Saturday 22:00
    inp = _input(bat_soc=80, ev_soc=30, pv_tomorrow=40.0, now=sat_22)
    plan = plan_night(inp, cfg)
    # EV home tomorrow (Sun morning) → some PV will go to EV
    assert plan.pv_to_ev_tomorrow_kwh > 0
    # bat 80% < target 100 → grid behövs efter EV-fönster (korrekt arkitektur)
    assert plan.bat_need_grid_kwh > 0
    # PV-allokering: EV först (helg fm hemma), bat tar grid sluttimmar
    assert plan.pv_to_bat_tomorrow_kwh < plan.pv_to_ev_tomorrow_kwh


def test_pure_bat_focus_low_pv_weekday(cfg: NightPlannerConfig) -> None:
    """Low PV + weekday → ev_window kort, bat_window lång."""
    inp = _input(bat_soc=30, ev_soc=80, pv_tomorrow=5.0)
    plan = plan_night(inp, cfg)
    assert plan.bat_need_grid_kwh > 0  # PV tomorrow won't fill bat
    assert plan.pv_to_ev_tomorrow_kwh == 0  # weekday → no PV to EV


def test_balanced_split(cfg: NightPlannerConfig) -> None:
    """Both EV and BAT need significant charging."""
    inp = _input(bat_soc=50, ev_soc=50, pv_tomorrow=12.0)
    plan = plan_night(inp, cfg)
    assert plan.ev_min_hours > 0
    assert plan.bat_min_hours > 0
    assert plan.ev_window_start_hour == cfg.night_start_hour


def test_overflow_warning_when_both_need_too_long(cfg: NightPlannerConfig) -> None:
    """If EV+BAT need > 8h, planner warns and splits proportionally."""
    inp = _input(bat_soc=10, ev_soc=10, ev_cap=200, pv_tomorrow=0)
    plan = plan_night(inp, cfg)
    assert plan.overflow_warning is True
    assert "OVERFLOW" in plan.reason


def test_pv_to_ev_zero_when_ev_away_tomorrow(cfg: NightPlannerConfig) -> None:
    """Weekday morning → EV away → no PV allocated to EV."""
    inp = _input(pv_tomorrow=20.0, presence="not_home")
    plan = plan_night(inp, cfg)
    assert plan.pv_to_ev_tomorrow_kwh == 0


def test_pv_excess_calc(cfg: NightPlannerConfig) -> None:
    """pv_excess_tomorrow = pv_tomorrow - house_consumption."""
    inp = _input(pv_tomorrow=20.0)
    plan = plan_night(inp, cfg)
    # 20 - 14 (default house) = 6
    assert plan.pv_excess_tomorrow_kwh == pytest.approx(6.0, abs=0.01)


def test_pv_excess_zero_when_pv_too_low(cfg: NightPlannerConfig) -> None:
    """pv_excess = 0 when pv < house consumption."""
    inp = _input(pv_tomorrow=10.0)
    plan = plan_night(inp, cfg)
    assert plan.pv_excess_tomorrow_kwh == 0


def test_ev_need_calc(cfg: NightPlannerConfig) -> None:
    inp = _input(ev_soc=80, ev_target=100, ev_cap=90)
    plan = plan_night(inp, cfg)
    # (100-80)/100 * 90 = 18 kWh
    assert plan.ev_need_kwh == pytest.approx(18.0, abs=0.01)


def test_bat_need_calc(cfg: NightPlannerConfig) -> None:
    inp = _input(bat_soc=50, bat_cap=20, pv_tomorrow=0)
    plan = plan_night(inp, cfg)
    # bat target=100 default, (100-50)/100 * 20 = 10 kWh
    assert plan.bat_need_grid_kwh == pytest.approx(10.0, abs=0.01)


def test_zero_need_no_grid(cfg: NightPlannerConfig) -> None:
    """If both at target → no grid need."""
    inp = _input(bat_soc=100, ev_soc=100)
    plan = plan_night(inp, cfg)
    assert plan.ev_need_kwh == 0
    assert plan.bat_need_grid_kwh == 0


def test_is_in_window_simple() -> None:
    # 22..6 wraps midnight
    assert is_in_window(22, 22, 6) is True
    assert is_in_window(23, 22, 6) is True
    assert is_in_window(2, 22, 6) is True
    assert is_in_window(5, 22, 6) is True
    assert is_in_window(6, 22, 6) is False
    assert is_in_window(15, 22, 6) is False


def test_is_in_window_no_wrap() -> None:
    # 9..17 same day
    assert is_in_window(9, 9, 17) is True
    assert is_in_window(10, 9, 17) is True
    assert is_in_window(17, 9, 17) is False
    assert is_in_window(8, 9, 17) is False


def test_2026_04_16_live_scenario(cfg: NightPlannerConfig) -> None:
    """Regression: replikera live-fakta från 2026-04-16 22:00."""
    inp = NightPlannerInput(
        now=WEEKDAY_22_00,
        bat_total_soc_pct=50.0,    # ~mid på live (kontor 52, forrad 19)
        bat_total_cap_kwh=20.0,
        ev_soc_pct=79.2,
        ev_target_soc_pct=100.0,
        ev_battery_kwh=90.0,
        pv_forecast_tomorrow_kwh=12.9,  # live forecast
        ev_device_tracker_state="home",
    )
    plan = plan_night(inp, cfg)

    # EV need: (100-79.2)/100 * 90 = 18.7 kWh
    assert plan.ev_need_kwh == pytest.approx(18.7, abs=0.5)

    # Bat need (after pv tomorrow): pv_excess = max(0, 12.9-14) = 0
    # bat_need_kwh = (100-50)/100 * 20 = 10 kWh, all grid
    assert plan.bat_need_grid_kwh == pytest.approx(10.0, abs=0.5)

    # PV imorgon räcker ej till EV efter house
    assert plan.pv_to_ev_tomorrow_kwh == 0

    # EV-window startar 22:00
    assert plan.ev_window_start_hour == 22
