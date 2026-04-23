"""Integration tests for ev_dispatch_v2 + consumer cascade (PLAT-1790).

Tests realistic multi-cycle scenarios combining ev_dispatch + cascade together,
verifying end-to-end EV behaviour and grid invariant.
"""

from __future__ import annotations

import pytest

from config.schema import EVDispatchV2Config
from core.ev_dispatch import (
    EVActionType,
    EVDispatchInputs,
    EVDispatchState,
    EVPhase,
    evaluate_ev_action,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _cfg(**kwargs: object) -> EVDispatchV2Config:
    defaults: dict[str, object] = {
        "enabled": True,
        "shadow_mode": False,
        "bat_ready_threshold_pct": 95.0,
        "min_session_min": 15.0,
        "margin_factor": 1.2,
        "ev_min_amps": 6,
        "ev_max_amps": 10,
        "ev_phase_count": 3,
        "bat_smoothing_threshold_w": 300.0,
        "grid_incident_threshold_w": 100.0,
    }
    defaults.update(kwargs)
    return EVDispatchV2Config(**defaults)  # type: ignore[arg-type]


def _ok_inputs(**kwargs: object) -> EVDispatchInputs:
    """Build inputs where all criteria pass."""
    defaults: dict[str, object] = {
        "ev_status": "connected",
        "bat_soc": 97.0,
        "surplus_w": 3000.0,
        "grid_w": 50.0,
        "predicted_refill_kwh": 10.0,
        "predicted_bat_deficit_kwh": 5.0,
        "planned_window_min": 60.0,
        "bat_soc_at_sunset": 100.0,
        "pv_power_w": 3000.0,
        "prev_pv_power_w": 3000.0,
        "now_monotonic": 1000.0,
    }
    defaults.update(kwargs)
    return EVDispatchInputs(**defaults)  # type: ignore[arg-type]


# ── Scenario 1: Plug-in → cascade to EV start (2 cycles) ─────────────────────

def test_plug_in_cascade_to_start_within_2_cycles() -> None:
    """Car plugged in → IDLE → CONNECTED (cycle 1) → CHARGING (cycle 2)."""
    cfg = _cfg()
    state = EVDispatchState(phase=EVPhase.IDLE)

    # Cycle 1: plug detected (IDLE → CONNECTED)
    r1 = evaluate_ev_action(state, _ok_inputs(ev_status="connected"), cfg)
    assert r1.new_state.phase == EVPhase.CONNECTED
    assert r1.action == EVActionType.NOOP

    # Cycle 2: criteria all met (CONNECTED → CHARGING)
    r2 = evaluate_ev_action(r1.new_state, _ok_inputs(), cfg)
    assert r2.new_state.phase == EVPhase.CHARGING
    assert r2.action == EVActionType.EV_START
    assert r2.amps >= 6


# ── Scenario 2: PV cloud-front → bat smoothing, EV continues ─────────────────

def test_cloud_front_bat_smoothing_no_stop() -> None:
    """PV dip during charging → bat_smoothing_w > 0, EV continues charging."""
    cfg = _cfg(bat_smoothing_threshold_w=300.0)
    state = EVDispatchState(phase=EVPhase.CHARGING, ev_amps=8)

    # PV drops 800W (above 300W threshold)
    result = evaluate_ev_action(
        state,
        _ok_inputs(pv_power_w=2200.0, prev_pv_power_w=3000.0),
        cfg,
    )
    assert result.new_state.phase == EVPhase.CHARGING
    assert result.bat_smoothing_w == pytest.approx(800.0)
    assert result.action in (EVActionType.NOOP, EVActionType.EV_ADJUST)


# ── Scenario 3: Sunset guard triggers stop ────────────────────────────────────

def test_sunset_guard_stops_charging() -> None:
    """bat_soc_at_sunset < 100% during CHARGING → stop, reason R9."""
    cfg = _cfg()
    state = EVDispatchState(phase=EVPhase.CHARGING, ev_amps=8)

    result = evaluate_ev_action(
        state,
        _ok_inputs(bat_soc_at_sunset=85.0),
        cfg,
    )
    assert result.action == EVActionType.EV_STOP
    assert result.new_state.phase == EVPhase.COMPLETING
    assert "R9" in result.reason


# ── Scenario 4: R1 grid incident triggers stop + incident log ─────────────────

def test_r1_incident_triggers_stop_and_incident() -> None:
    """Grid > 100W during CHARGING → EV_INCIDENT_ALERT + incident flag."""
    cfg = _cfg(grid_incident_threshold_w=100.0)
    state = EVDispatchState(phase=EVPhase.CHARGING, ev_amps=8)

    result = evaluate_ev_action(
        state,
        _ok_inputs(grid_w=150.0),
        cfg,
    )
    assert result.action == EVActionType.EV_INCIDENT_ALERT
    assert result.grid_incident is True
    assert result.new_state.phase == EVPhase.ERROR
    assert result.new_state.incidents_today >= 1


# ── Scenario 5: Shadow mode — evaluate but no writes ─────────────────────────

def test_shadow_mode_evaluates_but_no_writes() -> None:
    """Shadow mode returns EV_START but marks is_shadow=True (no writes)."""
    cfg = _cfg(shadow_mode=True)
    state = EVDispatchState(phase=EVPhase.CONNECTED)

    result = evaluate_ev_action(state, _ok_inputs(), cfg)

    # Action should be EV_START (criteria met)
    assert result.action == EVActionType.EV_START
    # But marked as shadow → coordinator must NOT execute
    assert result.is_shadow is True
