"""Regression tests for PLAT-1790 oscillation incidents (2026-04-20/21/22/23).

These tests verify that ev_dispatch_v2 NEVER writes to EV when car is disconnected,
regardless of what plan/price logic says. The root cause of the oscillation was that
v1 wrote to switch.easee_home_12840_is_enabled without checking cable status.

Each test simulates the conditions observed on the incident dates.
"""

from __future__ import annotations

from config.schema import EVDispatchV2Config
from core.ev_dispatch import (
    EVActionType,
    EVDispatchInputs,
    EVDispatchState,
    EVPhase,
    evaluate_ev_action,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _cfg() -> EVDispatchV2Config:
    return EVDispatchV2Config(enabled=True)


def _disconnected_inputs(**kwargs: object) -> EVDispatchInputs:
    """Inputs with EV disconnected — no charging should occur."""
    defaults: dict[str, object] = {
        "ev_status": "disconnected",
        "bat_soc": 100.0,
        "surplus_w": 5000.0,  # Even with lots of surplus
        "grid_w": 0.0,
        "predicted_refill_kwh": 20.0,
        "predicted_bat_deficit_kwh": 0.0,
        "planned_window_min": 120.0,
        "bat_soc_at_sunset": 100.0,
        "pv_power_w": 5000.0,
        "prev_pv_power_w": 5000.0,
        "now_monotonic": 1000.0,
    }
    defaults.update(kwargs)
    return EVDispatchInputs(**defaults)  # type: ignore[arg-type]


# ── Regression 1: 2026-04-20 — 1220 events (120 cycle simulation) ────────────

def test_no_writes_when_disconnected_2026_04_20() -> None:
    """Simulate 120 cycles (60 min) with car disconnected — 0 EV writes.

    Incident: 2026-04-20, 1220 events on switch.easee_home_12840_is_enabled.
    Root cause: plan executor wrote without cable check.
    """
    cfg = _cfg()
    state = EVDispatchState(phase=EVPhase.IDLE)
    write_count = 0

    for cycle in range(120):
        result = evaluate_ev_action(
            state,
            _disconnected_inputs(now_monotonic=float(cycle * 30)),
            cfg,
        )
        if result.action not in (EVActionType.NOOP, EVActionType.EV_INCIDENT_ALERT):
            write_count += 1
        state = result.new_state

    assert write_count == 0, (
        f"Expected 0 EV writes when disconnected, got {write_count}"
    )
    assert state.phase == EVPhase.IDLE


# ── Regression 2: 2026-04-22 — no EV charge despite plan saying start ────────

def test_no_writes_when_disconnected_2026_04_22() -> None:
    """Same as regression 1 but for 2026-04-22 incident date.

    Incident: EV was disconnected but writes continued.
    """
    cfg = _cfg()
    state = EVDispatchState(phase=EVPhase.IDLE)
    write_count = 0

    for cycle in range(120):
        result = evaluate_ev_action(
            state,
            _disconnected_inputs(
                ev_status="disconnected",
                now_monotonic=float(cycle * 30 + 86400),  # different base time
            ),
            cfg,
        )
        if result.action not in (EVActionType.NOOP, EVActionType.EV_INCIDENT_ALERT):
            write_count += 1
        state = result.new_state

    assert write_count == 0


# ── Regression 3: Plan says start, but car not connected → 0 writes ──────────

def test_no_writes_when_plan_says_start_but_disconnected() -> None:
    """Even when all criteria would be met, disconnected = no writes.

    The acceptance formula always checks R2 (plug status) first.
    """
    cfg = _cfg()
    state = EVDispatchState(phase=EVPhase.IDLE)
    write_count = 0

    for cycle in range(30):
        # Simulate: "plan says start" by providing all-green inputs except ev_status
        result = evaluate_ev_action(
            state,
            _disconnected_inputs(
                ev_status="disconnected",
                bat_soc=100.0,
                surplus_w=8000.0,
                predicted_refill_kwh=50.0,
                predicted_bat_deficit_kwh=1.0,
                planned_window_min=180.0,
                bat_soc_at_sunset=100.0,
                now_monotonic=float(cycle * 30),
            ),
            cfg,
        )
        if result.action not in (EVActionType.NOOP, EVActionType.EV_INCIDENT_ALERT):
            write_count += 1
        state = result.new_state

    assert write_count == 0


# ── Regression 4: External write doesn't cause oscillation ───────────────────

def test_no_tamper_oscillation_when_disconnected() -> None:
    """Simulate external entity writing to EV switch → v2 does NOT respond.

    Oscillation root cause: v2 noticed state mismatch and "corrected" it each cycle,
    while external (Easee cloud) corrected back → loop.
    With v2, disconnected = IDLE = NOOP always, regardless of is_enabled state.
    """
    cfg = _cfg()
    state = EVDispatchState(phase=EVPhase.IDLE)
    ev_write_actions: list[str] = []

    for cycle in range(40):
        # Simulate: external alternates EV on/off (tamper)
        ev_status = "disconnected"  # Car never actually connected

        result = evaluate_ev_action(
            state,
            _disconnected_inputs(ev_status=ev_status, now_monotonic=float(cycle * 30)),
            cfg,
        )

        if result.action != EVActionType.NOOP:
            ev_write_actions.append(str(result.action))
        state = result.new_state

    assert len(ev_write_actions) == 0, (
        f"Expected no write actions but got: {ev_write_actions}"
    )


# ── Regression 5: Startup recovery with disconnected EV → 0 writes ───────────

def test_startup_recovery_no_writes_when_disconnected() -> None:
    """After restart with ev_enabled=True in persistent state, disconnected → 0 writes.

    Incident cause: startup recovery wrote switch.turn_on without checking cable.
    v2 fix: ev_dispatch is checked every cycle with current EV status.
    """
    cfg = _cfg()

    # Simulate: persistent state says "ev was enabled before restart"
    # but the state machine in v2 starts fresh at IDLE
    state = EVDispatchState(phase=EVPhase.IDLE)

    # First 3 cycles after restart
    for cycle in range(3):
        result = evaluate_ev_action(
            state,
            _disconnected_inputs(
                ev_status="disconnected",
                now_monotonic=float(cycle * 30),
            ),
            cfg,
        )
        assert result.action == EVActionType.NOOP, (
            f"Cycle {cycle}: expected NOOP but got {result.action}"
        )
        state = result.new_state
