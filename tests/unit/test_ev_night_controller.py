"""Unit tests for core.ev_night_controller (PLAT-1674).

Test names from 901 ARCH RESPONSE Q5/N3:
  - test_in_night_window — wraps-midnight edge cases
  - test_at_or_above_target — soc edge cases
  - test_evaluate_outside_window — STOP if charging
  - test_evaluate_start_sequence — first cycle: START + SET(6)
  - test_evaluate_ramp_up / ramp_down / hold_interval
  - test_evaluate_at_target_stop
"""

from __future__ import annotations

from datetime import datetime

import pytest

from core.ev_night_controller import (
    NightEVConfig,
    NightEVState,
    _at_or_above_target,
    _ev_ready_to_start,
    _in_night_window,
    evaluate,
)
from core.models import CommandType, EVState


# Fixed reference points (Wed = weekday)
T_22_00 = datetime(2026, 4, 15, 22, 0, 0)
T_22_30 = datetime(2026, 4, 15, 22, 30, 0)
T_03_00 = datetime(2026, 4, 16, 3, 0, 0)
T_06_00 = datetime(2026, 4, 16, 6, 0, 0)
T_15_00 = datetime(2026, 4, 16, 15, 0, 0)


@pytest.fixture
def cfg() -> NightEVConfig:
    return NightEVConfig()


def _ev(*, soc: float = 50.0, charging: bool = False, connected: bool = True) -> EVState:
    return EVState(
        soc_pct=soc, connected=connected, charging=charging,
        power_w=0.0, current_a=0.0, charger_status="awaiting_start",
    )


def _state(amps: int = 0, last_ramp_offset_s: int = 0, ref: datetime = T_22_30) -> NightEVState:
    return NightEVState(
        current_amps=amps,
        last_ramp_ts=ref.timestamp() - last_ramp_offset_s,
    )


# -------------------------------------------------------------------
# _in_night_window — wraps midnight
# -------------------------------------------------------------------


def test_in_night_window_22_inside(cfg: NightEVConfig) -> None:
    assert _in_night_window(datetime(2026, 4, 15, 22, 0, 0), cfg) is True


def test_in_night_window_03_inside(cfg: NightEVConfig) -> None:
    assert _in_night_window(datetime(2026, 4, 16, 3, 0, 0), cfg) is True


def test_in_night_window_05_59_inside(cfg: NightEVConfig) -> None:
    assert _in_night_window(datetime(2026, 4, 16, 5, 59, 0), cfg) is True


def test_in_night_window_06_outside(cfg: NightEVConfig) -> None:
    assert _in_night_window(datetime(2026, 4, 16, 6, 0, 0), cfg) is False


def test_in_night_window_15_outside(cfg: NightEVConfig) -> None:
    assert _in_night_window(datetime(2026, 4, 16, 15, 0, 0), cfg) is False


def test_in_night_window_21_59_outside(cfg: NightEVConfig) -> None:
    assert _in_night_window(datetime(2026, 4, 15, 21, 59, 0), cfg) is False


# -------------------------------------------------------------------
# _at_or_above_target — soc edge cases (margin 0.5)
# -------------------------------------------------------------------


def test_at_or_above_target_above() -> None:
    assert _at_or_above_target(99.6, 100.0) is True


def test_at_or_above_target_at_margin() -> None:
    assert _at_or_above_target(99.5, 100.0) is True


def test_at_or_above_target_under_margin() -> None:
    assert _at_or_above_target(99.4, 100.0) is False


def test_at_or_above_target_75() -> None:
    assert _at_or_above_target(74.5, 75.0) is True
    assert _at_or_above_target(74.4, 75.0) is False


# -------------------------------------------------------------------
# _ev_ready_to_start
# -------------------------------------------------------------------


def test_ev_ready_when_plugged_below_target() -> None:
    ev = _ev(soc=80, connected=True)
    assert _ev_ready_to_start(ev, target_soc_pct=100) is True


def test_ev_not_ready_unplugged() -> None:
    ev = _ev(soc=50, connected=False)
    assert _ev_ready_to_start(ev, target_soc_pct=100) is False


def test_ev_not_ready_at_target() -> None:
    ev = _ev(soc=100, connected=True)
    assert _ev_ready_to_start(ev, target_soc_pct=100) is False


# -------------------------------------------------------------------
# evaluate — main entry
# -------------------------------------------------------------------


def test_evaluate_outside_window_idle(cfg: NightEVConfig) -> None:
    """Daytime + idle → no commands."""
    res = evaluate(
        T_15_00, _ev(charging=False), grid_weighted_kw=1.0,
        target_soc_pct=100, state=_state(amps=0), cfg=cfg,
    )
    assert res.commands == []
    assert res.new_amps == 0
    assert "OUTSIDE_NIGHT_WINDOW" in res.reason


def test_evaluate_outside_window_was_charging_stops(cfg: NightEVConfig) -> None:
    """Daytime + was charging → emit STOP."""
    res = evaluate(
        T_15_00, _ev(charging=True), grid_weighted_kw=1.0,
        target_soc_pct=100, state=_state(amps=10), cfg=cfg,
    )
    assert any(c.command_type == CommandType.STOP_EV_CHARGING for c in res.commands)


def test_evaluate_start_sequence_at_22(cfg: NightEVConfig) -> None:
    """22:00 + plugged + below target → START + SET_CURRENT(6)."""
    res = evaluate(
        T_22_00, _ev(soc=80, charging=False), grid_weighted_kw=1.0,
        target_soc_pct=100, state=_state(amps=0), cfg=cfg,
    )
    cmd_types = [c.command_type for c in res.commands]
    assert CommandType.START_EV_CHARGING in cmd_types
    assert CommandType.SET_EV_CURRENT in cmd_types
    set_cmd = next(c for c in res.commands if c.command_type == CommandType.SET_EV_CURRENT)
    assert set_cmd.value == cfg.start_amps  # 6
    assert res.new_amps == cfg.start_amps


def test_evaluate_no_start_when_unplugged(cfg: NightEVConfig) -> None:
    res = evaluate(
        T_22_00, _ev(connected=False), grid_weighted_kw=1.0,
        target_soc_pct=100, state=_state(amps=0), cfg=cfg,
    )
    assert all(c.command_type != CommandType.START_EV_CHARGING for c in res.commands)


def test_evaluate_at_target_stops(cfg: NightEVConfig) -> None:
    """Charging + SoC reaches target → STOP."""
    res = evaluate(
        T_22_30, _ev(soc=100, charging=True), grid_weighted_kw=1.0,
        target_soc_pct=100, state=_state(amps=10), cfg=cfg,
    )
    assert any(c.command_type == CommandType.STOP_EV_CHARGING for c in res.commands)
    assert "AT_TARGET" in res.reason


def test_evaluate_ramp_up_low_grid(cfg: NightEVConfig) -> None:
    """Charging at 6A, weighted < threshold_up → ramp to 7A."""
    res = evaluate(
        T_22_30, _ev(soc=80, charging=True), grid_weighted_kw=1.0,
        target_soc_pct=100,
        state=_state(amps=6, last_ramp_offset_s=120),  # past interval
        cfg=cfg,
    )
    assert res.new_amps == 7
    assert any(
        c.command_type == CommandType.SET_EV_CURRENT and c.value == 7
        for c in res.commands
    )


def test_evaluate_ramp_down_high_grid(cfg: NightEVConfig) -> None:
    """Charging at 10A, weighted > threshold_down → ramp to 9A."""
    res = evaluate(
        T_22_30, _ev(soc=80, charging=True),
        grid_weighted_kw=cfg.tak_weighted_kw,  # well above threshold_down
        target_soc_pct=100,
        state=_state(amps=10, last_ramp_offset_s=120),
        cfg=cfg,
    )
    assert res.new_amps == 9


def test_evaluate_hold_within_band(cfg: NightEVConfig) -> None:
    """Charging at 8A, weighted in band → HOLD."""
    res = evaluate(
        T_22_30, _ev(soc=80, charging=True),
        grid_weighted_kw=2.8,  # between thresholds (between 2.7 and 2.85)
        target_soc_pct=100,
        state=_state(amps=8, last_ramp_offset_s=120),
        cfg=cfg,
    )
    assert res.new_amps == 8
    assert "HOLD" in res.reason


def test_evaluate_hold_when_interval_not_elapsed(cfg: NightEVConfig) -> None:
    """Last ramp 30s ago → still hold (interval 60s)."""
    res = evaluate(
        T_22_30, _ev(soc=80, charging=True), grid_weighted_kw=1.0,
        target_soc_pct=100,
        state=_state(amps=8, last_ramp_offset_s=30),
        cfg=cfg,
    )
    assert res.new_amps == 8
    assert "HOLD" in res.reason


def test_evaluate_ramp_up_capped_at_max(cfg: NightEVConfig) -> None:
    """At max amps → no ramp up even with very low grid."""
    res = evaluate(
        T_22_30, _ev(soc=80, charging=True), grid_weighted_kw=0.1,
        target_soc_pct=100,
        state=_state(amps=cfg.max_amps, last_ramp_offset_s=120),
        cfg=cfg,
    )
    assert res.new_amps == cfg.max_amps  # stays


def test_evaluate_ramp_down_floored_at_min(cfg: NightEVConfig) -> None:
    """At min amps → no ramp down even with high grid."""
    res = evaluate(
        T_22_30, _ev(soc=80, charging=True), grid_weighted_kw=10.0,
        target_soc_pct=100,
        state=_state(amps=cfg.min_amps, last_ramp_offset_s=120),
        cfg=cfg,
    )
    assert res.new_amps == cfg.min_amps  # stays


def test_evaluate_ev_disconnect_mid_charge(cfg: NightEVConfig) -> None:
    """EV unplugged mid-night → STOP."""
    res = evaluate(
        T_22_30, _ev(connected=False, charging=True), grid_weighted_kw=1.0,
        target_soc_pct=100, state=_state(amps=8), cfg=cfg,
    )
    assert any(c.command_type == CommandType.STOP_EV_CHARGING for c in res.commands)
    assert "EV_DISCONNECTED" in res.reason


# -------------------------------------------------------------------
# Regression — 2026-04-16 live incident
# -------------------------------------------------------------------


def test_2026_04_16_live_22_00_starts(cfg: NightEVConfig) -> None:
    """Replikera live: kl 22:00, EV plugged 79.2%, target 100, idle.
    Förväntat: START + SET_CURRENT(6) emitteras direkt.
    """
    ev = EVState(
        soc_pct=79.2, connected=True, charging=False,
        power_w=0.0, current_a=0.0, charger_status="awaiting_start",
        reason_for_no_current="charger_disabled",
    )
    res = evaluate(
        T_22_00, ev, grid_weighted_kw=0.5,
        target_soc_pct=100, state=NightEVState(current_amps=0),
        cfg=cfg,
    )
    cmd_types = [c.command_type for c in res.commands]
    assert CommandType.START_EV_CHARGING in cmd_types
    assert CommandType.SET_EV_CURRENT in cmd_types
    assert res.new_amps == 6


def test_2026_04_16_disk_load_ramp_down() -> None:
    """Replikera: EV @ 10A + diskmaskin → grid_weighted hög → ramp down till 9A.

    Live var: weighted 5.62 kW raw (translates to weighted ~2.8 with night_weight 0.5)
    → over threshold_down → ramp down.
    """
    cfg = NightEVConfig(tak_weighted_kw=3.0)
    ev = EVState(
        soc_pct=80.0, connected=True, charging=True,
        power_w=2300.0, current_a=10.0, charger_status="charging",
    )
    state = NightEVState(current_amps=10, last_ramp_ts=T_22_30.timestamp() - 120)
    res = evaluate(
        T_22_30, ev,
        grid_weighted_kw=2.9,  # > threshold_down (2.85)
        target_soc_pct=100, state=state, cfg=cfg,
    )
    assert res.new_amps == 9


def test_command_target_is_ev() -> None:
    """All EV commands should target 'ev' (used by executor)."""
    res = evaluate(
        T_22_00, _ev(soc=80), grid_weighted_kw=1.0,
        target_soc_pct=100, state=NightEVState(), cfg=NightEVConfig(),
    )
    for c in res.commands:
        assert c.target_id == "ev"
        assert c.rule_id == "NIGHT_EV"


def test_decision_reason_always_set() -> None:
    """Reason should never be empty — used for sensor.carma_v2_ev_decision_reason."""
    res = evaluate(
        T_15_00, _ev(), grid_weighted_kw=1.0,
        target_soc_pct=100, state=NightEVState(), cfg=NightEVConfig(),
    )
    assert res.reason != ""


def test_no_hardcoded_amps_in_commands() -> None:
    """B2 from PLAT-1673 — commands use config values, not hardcoded.

    Verifiera att SET_EV_CURRENT använder cfg.start_amps (default 6),
    inte hardcoded 6 direkt i logiken.
    """
    cfg = NightEVConfig(start_amps=8)  # use non-default
    res = evaluate(
        T_22_00, _ev(soc=80), grid_weighted_kw=1.0,
        target_soc_pct=100, state=NightEVState(), cfg=cfg,
    )
    set_cmd = next(c for c in res.commands if c.command_type == CommandType.SET_EV_CURRENT)
    assert set_cmd.value == 8  # cfg-driven, not hardcoded 6
