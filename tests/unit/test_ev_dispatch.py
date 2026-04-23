"""Tests for core/ev_dispatch.py — EV dispatch v2 state machine (PLAT-1790).

Tests cover acceptance formula R1-R12, state transitions, bat smoothing,
shadow mode, and feature flag behaviour.
"""

from __future__ import annotations

import pytest

from config.schema import EVDispatchV2Config
from core.ev_dispatch import (
    EVActionType,
    EVDispatchInputs,
    EVDispatchState,
    EVPhase,
    _check_r1_grid_incident,
    _check_r2_plug,
    _check_r9_bat_sunset,
    _check_r10_refill,
    _check_r11_window,
    _check_r12_bat_ready,
    _compute_bat_smoothing,
    _optimal_amps,
    evaluate_ev_action,
)

# ── Fixtures ────────────────────────────────────────────────────────────────

def _cfg(**kwargs: object) -> EVDispatchV2Config:
    """Build test config with feature enabled and all permissive defaults."""
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


def _inputs(**kwargs: object) -> EVDispatchInputs:
    """Build test inputs where all acceptance criteria pass by default."""
    defaults: dict[str, object] = {
        "ev_status": "connected",
        "bat_soc": 97.0,           # > 95% threshold (R12 OK)
        "surplus_w": 3000.0,        # enough surplus
        "grid_w": 50.0,             # within ±100W (R1 OK)
        "predicted_refill_kwh": 10.0,  # > deficit*1.2 (R10 OK)
        "predicted_bat_deficit_kwh": 5.0,  # 5 x 1.2 = 6.0, refill=10 → OK
        "planned_window_min": 60.0,    # > 15 min (R11 OK)
        "bat_soc_at_sunset": 100.0,    # (R9 OK)
        "pv_power_w": 3000.0,
        "prev_pv_power_w": 3000.0,
        "now_monotonic": 1000.0,
    }
    defaults.update(kwargs)
    return EVDispatchInputs(**defaults)  # type: ignore[arg-type]


def _idle_state() -> EVDispatchState:
    return EVDispatchState(phase=EVPhase.IDLE)


def _connected_state() -> EVDispatchState:
    return EVDispatchState(phase=EVPhase.CONNECTED)


def _charging_state(amps: int = 8) -> EVDispatchState:
    return EVDispatchState(phase=EVPhase.CHARGING, ev_amps=amps)


# ── Test 1: feature flag OFF → always NOOP + IDLE ───────────────────────────

def test_idle_when_feature_flag_off() -> None:
    """When enabled=False, evaluate_ev_action always returns NOOP in IDLE."""
    cfg = _cfg(enabled=False)
    for phase in EVPhase:
        state = EVDispatchState(phase=phase)
        result = evaluate_ev_action(state, _inputs(), cfg)
        assert result.action == EVActionType.NOOP
        assert result.new_state.phase == EVPhase.IDLE
        assert "disabled" in result.reason


# ── Test 2: IDLE + disconnected → stays IDLE ────────────────────────────────

def test_idle_when_disconnected() -> None:
    """Disconnected EV stays IDLE, no writes."""
    result = evaluate_ev_action(_idle_state(), _inputs(ev_status="disconnected"), _cfg())
    assert result.action == EVActionType.NOOP
    assert result.new_state.phase == EVPhase.IDLE


# ── Test 3: IDLE + connected → transition to CONNECTED ──────────────────────

def test_connected_when_plugged_in() -> None:
    """IDLE state transitions to CONNECTED when plug detected."""
    result = evaluate_ev_action(_idle_state(), _inputs(ev_status="connected"), _cfg())
    assert result.new_state.phase == EVPhase.CONNECTED
    assert result.action == EVActionType.NOOP  # just transition, not yet start


# ── Test 4: All active statuses accepted (R2) ────────────────────────────────

@pytest.mark.parametrize("status", ["connected", "awaiting_start", "charging", "ready_to_charge"])
def test_r2_all_active_statuses_reach_connected(status: str) -> None:
    """All active EV statuses transition from IDLE → CONNECTED."""
    result = evaluate_ev_action(_idle_state(), _inputs(ev_status=status), _cfg())
    assert result.new_state.phase == EVPhase.CONNECTED, f"expected CONNECTED for status={status!r}"


# ── Test 5: R12 — bat not ready blocks start ─────────────────────────────────

def test_r12_bat_not_ready_blocks_start() -> None:
    """bat_soc below threshold keeps state in CONNECTED, reason contains R12."""
    result = evaluate_ev_action(
        _connected_state(),
        _inputs(bat_soc=80.0, ev_status="connected"),
        _cfg(bat_ready_threshold_pct=95.0),
    )
    assert result.action == EVActionType.NOOP
    assert result.new_state.phase == EVPhase.CONNECTED
    assert "R12" in result.reason


# ── Test 6: R10 — refill insufficient blocks start ───────────────────────────

def test_r10_refill_insufficient_blocks_start() -> None:
    """Insufficient refill forecast keeps state CONNECTED, reason contains R10."""
    result = evaluate_ev_action(
        _connected_state(),
        _inputs(
            predicted_refill_kwh=3.0,
            predicted_bat_deficit_kwh=5.0,  # need 5*1.2=6.0, have 3.0 → fail
        ),
        _cfg(),
    )
    assert result.action == EVActionType.NOOP
    assert "R10" in result.reason


# ── Test 7: R11 — window too short blocks start ──────────────────────────────

def test_r11_window_too_short_blocks_start() -> None:
    """Planned window < min_session blocks start, reason contains R11."""
    result = evaluate_ev_action(
        _connected_state(),
        _inputs(planned_window_min=5.0),
        _cfg(min_session_min=15.0),
    )
    assert result.action == EVActionType.NOOP
    assert "R11" in result.reason


# ── Test 8: R9 — bat not full at sunset blocks start ─────────────────────────

def test_r9_bat_not_full_at_sunset_blocks_start() -> None:
    """bat_soc_at_sunset < 100 blocks start, reason contains R9."""
    result = evaluate_ev_action(
        _connected_state(),
        _inputs(bat_soc_at_sunset=85.0),
        _cfg(),
    )
    assert result.action == EVActionType.NOOP
    assert "R9" in result.reason


# ── Test 9: all criteria met → EV_START ──────────────────────────────────────

def test_all_criteria_met_starts_ev() -> None:
    """When all R9+R10+R11+R12 pass from CONNECTED, EV_START is returned."""
    result = evaluate_ev_action(_connected_state(), _inputs(), _cfg())
    assert result.action == EVActionType.EV_START
    assert result.new_state.phase == EVPhase.CHARGING
    assert result.amps >= 6


# ── Test 10: CHARGING continues when criteria met ────────────────────────────

def test_charging_continues_when_criteria_met() -> None:
    """In CHARGING phase, when criteria still met, NOOP keeps charging."""
    result = evaluate_ev_action(_charging_state(amps=8), _inputs(), _cfg())
    assert result.new_state.phase == EVPhase.CHARGING
    assert result.action in (EVActionType.NOOP, EVActionType.EV_ADJUST)


# ── Test 11: CHARGING stops when criteria lost (R12) ─────────────────────────

def test_charging_stops_when_r12_lost() -> None:
    """In CHARGING, if bat drops below threshold, transition to COMPLETING."""
    result = evaluate_ev_action(
        _charging_state(),
        _inputs(bat_soc=80.0),
        _cfg(bat_ready_threshold_pct=95.0),
    )
    assert result.action == EVActionType.EV_STOP
    assert result.new_state.phase == EVPhase.COMPLETING


# ── Test 12: R1 incident during CHARGING ─────────────────────────────────────

def test_r1_incident_during_charging_triggers_alert() -> None:
    """Grid > 100W during CHARGING → EV_INCIDENT_ALERT + ERROR phase."""
    result = evaluate_ev_action(
        _charging_state(),
        _inputs(grid_w=200.0),
        _cfg(grid_incident_threshold_w=100.0),
    )
    assert result.action == EVActionType.EV_INCIDENT_ALERT
    assert result.new_state.phase == EVPhase.ERROR
    assert result.grid_incident is True
    assert result.new_state.incidents_today == 1
    assert "R1" in result.reason


# ── Test 13: R1 does NOT trigger when idle ───────────────────────────────────

def test_r1_no_incident_when_idle() -> None:
    """Grid > 100W in IDLE phase does NOT trigger R1 incident."""
    result = evaluate_ev_action(_idle_state(), _inputs(grid_w=200.0), _cfg())
    assert result.grid_incident is False
    assert result.new_state.incidents_today == 0


# ── Test 14: Disconnect during CHARGING → ERROR ──────────────────────────────

def test_disconnect_during_charging() -> None:
    """EV disconnect during CHARGING → EV_STOP + ERROR phase."""
    result = evaluate_ev_action(
        _charging_state(),
        _inputs(ev_status="disconnected"),
        _cfg(),
    )
    assert result.action == EVActionType.EV_STOP
    assert result.new_state.phase == EVPhase.ERROR


# ── Test 15: ERROR → IDLE ────────────────────────────────────────────────────

def test_error_transitions_to_idle() -> None:
    """ERROR phase always transitions to IDLE next cycle."""
    state = EVDispatchState(phase=EVPhase.ERROR)
    result = evaluate_ev_action(state, _inputs(), _cfg())
    assert result.new_state.phase == EVPhase.IDLE
    assert result.action == EVActionType.NOOP


# ── Test 16: COMPLETING → IDLE ───────────────────────────────────────────────

def test_completing_transitions_to_idle() -> None:
    """COMPLETING phase transitions to IDLE next cycle."""
    state = EVDispatchState(phase=EVPhase.COMPLETING)
    result = evaluate_ev_action(state, _inputs(), _cfg())
    assert result.new_state.phase == EVPhase.IDLE
    assert result.action == EVActionType.NOOP


# ── Test 17: Shadow mode — evaluate but mark as shadow ───────────────────────

def test_shadow_mode_marks_result_as_shadow() -> None:
    """Shadow mode evaluates and returns action but marks is_shadow=True."""
    cfg = _cfg(shadow_mode=True)
    result = evaluate_ev_action(_connected_state(), _inputs(), cfg)
    # Should have computed EV_START
    assert result.action == EVActionType.EV_START
    assert result.is_shadow is True


def test_shadow_mode_noop_not_flagged() -> None:
    """NOOP results in shadow mode are not specially flagged (already inert)."""
    cfg = _cfg(shadow_mode=True)
    result = evaluate_ev_action(_idle_state(), _inputs(ev_status="disconnected"), cfg)
    assert result.action == EVActionType.NOOP
    # is_shadow is False for NOOP (no wrapper needed)
    assert result.is_shadow is False


# ── Test 18: Battery smoothing computed for PV transient ─────────────────────

def test_bat_smoothing_triggered_by_pv_dip() -> None:
    """PV dip > threshold during CHARGING triggers bat_smoothing_w > 0."""
    result = evaluate_ev_action(
        _charging_state(),
        _inputs(pv_power_w=2000.0, prev_pv_power_w=3000.0),  # dip = 1000W > 300W threshold
        _cfg(bat_smoothing_threshold_w=300.0),
    )
    assert result.bat_smoothing_w > 0.0
    assert result.bat_smoothing_w == pytest.approx(1000.0)


# ── Test 19: No bat smoothing when PV stable ─────────────────────────────────

def test_bat_smoothing_not_triggered_when_stable() -> None:
    """Stable PV during CHARGING → bat_smoothing_w = 0."""
    result = evaluate_ev_action(
        _charging_state(),
        _inputs(pv_power_w=3000.0, prev_pv_power_w=3000.0),
        _cfg(),
    )
    assert result.bat_smoothing_w == 0.0


# ── Test 20: EV_ADJUST when surplus changes ───────────────────────────────────

def test_ev_adjust_when_surplus_increases() -> None:
    """Surplus increase during CHARGING → EV_ADJUST to new optimal amps."""
    # At 3000W surplus and 3 phases: 3000/(230*3) ≈ 4.35A → clamped to 6A (min)
    # At 7000W surplus: 7000/(230*3) ≈ 10.1A → clamped to 10A (max)
    result = evaluate_ev_action(
        _charging_state(amps=6),
        _inputs(surplus_w=7000.0),
        _cfg(ev_max_amps=10),
    )
    assert result.action == EVActionType.EV_ADJUST
    assert result.amps == 10


# ── Test 21: NOOP when amps already optimal ───────────────────────────────────

def test_noop_when_amps_already_optimal() -> None:
    """No adjust if current amps == optimal for current surplus."""
    # surplus=1400W: 1400/(230*3)=2.03A → clamped to 6A
    result = evaluate_ev_action(
        _charging_state(amps=6),
        _inputs(surplus_w=1400.0),  # → 6A optimal (clamped to min)
        _cfg(ev_min_amps=6),
    )
    assert result.action == EVActionType.NOOP
    assert result.new_state.ev_amps == 6


# ── Test 22: Rejection reason always contains R-number ───────────────────────

@pytest.mark.parametrize(
    ("test_inputs", "expected_r"),
    [
        ({"bat_soc": 80.0}, "R12"),
        ({"predicted_refill_kwh": 1.0, "predicted_bat_deficit_kwh": 5.0}, "R10"),
        ({"planned_window_min": 5.0}, "R11"),
        ({"bat_soc_at_sunset": 80.0}, "R9"),
    ],
)
def test_rejection_reason_contains_r_number(test_inputs: dict, expected_r: str) -> None:
    """Every rejection reason string must contain the corresponding R-number."""
    result = evaluate_ev_action(
        _connected_state(),
        _inputs(**test_inputs),
        _cfg(),
    )
    assert expected_r in result.reason, f"Expected {expected_r} in reason: {result.reason!r}"


# ── Unit tests for helpers ───────────────────────────────────────────────────

def test_check_r2_plug_accepts_active_statuses() -> None:
    for status in ("connected", "awaiting_start", "charging", "ready_to_charge"):
        assert _check_r2_plug(status) is None


def test_check_r2_plug_rejects_idle_statuses() -> None:
    for status in ("disconnected", "unavailable", "unknown", "none", ""):
        result = _check_r2_plug(status)
        assert result is not None
        assert "R2" in result


def test_check_r12_bat_ready_boundary() -> None:
    cfg = _cfg(bat_ready_threshold_pct=95.0)
    assert _check_r12_bat_ready(95.0, cfg.bat_ready_threshold_pct) is None
    assert _check_r12_bat_ready(94.9, cfg.bat_ready_threshold_pct) is not None


def test_check_r10_refill_boundary() -> None:
    # refill=12, deficit=10, margin=1.2 → need 12.0, have 12.0 → OK
    assert _check_r10_refill(12.0, 10.0, 1.2) is None
    # refill=11.9, deficit=10, margin=1.2 → need 12.0, have 11.9 → FAIL
    assert _check_r10_refill(11.9, 10.0, 1.2) is not None


def test_check_r11_window_boundary() -> None:
    assert _check_r11_window(15.0, 15.0) is None
    assert _check_r11_window(14.9, 15.0) is not None


def test_check_r9_sunset_boundary() -> None:
    assert _check_r9_bat_sunset(100.0) is None
    assert _check_r9_bat_sunset(99.9) is not None


def test_check_r1_grid_boundary() -> None:
    assert _check_r1_grid_incident(100.0, 100.0) is None  # exactly at limit = OK
    assert _check_r1_grid_incident(100.1, 100.0) is not None
    assert _check_r1_grid_incident(-100.1, 100.0) is not None  # negative also checked


def test_compute_bat_smoothing_values() -> None:
    # dip = 1000W > 300W threshold → smoothing = 1000
    assert _compute_bat_smoothing(2000.0, 3000.0, 300.0) == pytest.approx(1000.0)
    # dip = 200W < 300W threshold → no smoothing
    assert _compute_bat_smoothing(2800.0, 3000.0, 300.0) == 0.0
    # PV increased (no dip) → no smoothing
    assert _compute_bat_smoothing(4000.0, 3000.0, 300.0) == 0.0


def test_optimal_amps_clamps_to_range() -> None:
    cfg = _cfg(ev_min_amps=6, ev_max_amps=10, ev_phase_count=3)
    # Very low surplus → clamped to min
    assert _optimal_amps(500.0, cfg) == 6
    # Very high surplus → clamped to max
    assert _optimal_amps(100_000.0, cfg) == 10
    # In range: 5520W / (230*3) = 8.0A
    assert _optimal_amps(5520.0, cfg) == 8


def test_incidents_counter_accumulates() -> None:
    """Multiple R1 incidents accumulate in state.incidents_today."""
    state = EVDispatchState(phase=EVPhase.CHARGING, ev_amps=8, incidents_today=2)
    result = evaluate_ev_action(state, _inputs(grid_w=200.0), _cfg())
    assert result.new_state.incidents_today == 3


def test_connected_plug_removed_returns_to_idle() -> None:
    """In CONNECTED, if plug is removed, immediately returns to IDLE."""
    result = evaluate_ev_action(
        _connected_state(),
        _inputs(ev_status="disconnected"),
        _cfg(),
    )
    assert result.new_state.phase == EVPhase.IDLE
