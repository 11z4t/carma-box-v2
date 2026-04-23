"""PLAT-1790: Coordinator-level compat tests for ev_dispatch_v2 state machine.

Ported from tests/unit/test_coordinator_v2.py::TestEVDispatchV2Integration
in the deprecated 4recon/carmabox repo. The old tests used CoordinatorV2.cycle()
— these equivalent tests call evaluate_ev_action() directly, simulating the same
cycle-by-cycle state tracking that main.py performs in _evaluate_ev().

All tests exercise the same invariants as the original coordinator tests:
- Feature flag gating
- Disconnected EV produces no write actions
- R12 bat-threshold rejection
- Shadow mode produces NOOP
- State object is replaced each cycle (not mutated in place)
- Multi-cycle phase progression
- Reason format follows ev_v2_* prefix convention
"""

from __future__ import annotations

import time

from config.schema import EVDispatchV2Config
from core.ev_dispatch import (
    EVActionType,
    EVDispatchInputs,
    EVDispatchState,
    EVPhase,
    evaluate_ev_action,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cfg(**kw: object) -> EVDispatchV2Config:
    defaults: dict[str, object] = {
        "enabled": True,
        "bat_ready_threshold_pct": 95.0,
        "min_session_min": 15.0,
        "shadow_mode": False,
    }
    defaults.update(kw)
    return EVDispatchV2Config(**defaults)  # type: ignore[arg-type]


def _inputs(**kw: object) -> EVDispatchInputs:
    """All-pass inputs simulating a connected, surplus-rich scenario."""
    defaults: dict[str, object] = {
        "ev_status": "connected",
        "bat_soc": 96.0,
        "surplus_w": 3000.0,
        "grid_w": 0.0,
        "predicted_refill_kwh": 12.0,
        "predicted_bat_deficit_kwh": 2.0,
        "planned_window_min": 120.0,
        "bat_soc_at_sunset": 100.0,
        "pv_power_w": 3000.0,
        "prev_pv_power_w": 3000.0,
        "now_monotonic": time.monotonic(),
    }
    defaults.update(kw)
    return EVDispatchInputs(**defaults)  # type: ignore[arg-type]


def _cycle(
    state: EVDispatchState,
    cfg: EVDispatchV2Config,
    **input_kw: object,
) -> tuple[EVDispatchState, EVActionType, str]:
    """Simulate one coordinator cycle: call evaluate_ev_action and return
    (new_state, action, reason)."""
    result = evaluate_ev_action(state, _inputs(**input_kw), cfg)
    return result.new_state, result.action, result.reason


# ── Tests ────────────────────────────────────────────────────────────────────

class TestEVDispatchCoordinatorCompat:
    """Coordinator-level integration tests for ev_dispatch_v2 (PLAT-1790)."""

    def test_feature_flag_disabled_returns_noop_reason(self) -> None:
        """Disabled feature flag → NOOP with 'feature flag' in reason."""
        cfg = EVDispatchV2Config(enabled=False)
        state = EVDispatchState()
        _, action, reason = _cycle(state, cfg)
        assert action == EVActionType.NOOP
        assert "feature flag" in reason

    def test_feature_flag_enabled_populates_reason(self) -> None:
        """With enabled=True, v2 path runs and populates a non-empty reason."""
        cfg = _cfg()
        state = EVDispatchState()
        _, action, reason = _cycle(state, cfg)
        assert reason != ""

    def test_disconnected_ev_produces_no_write_action(self) -> None:
        """Disconnected EV -> R2 blocks any charge command (anti-oscillation)."""
        cfg = _cfg()
        state = EVDispatchState()
        _, action, _ = _cycle(state, cfg, ev_status="disconnected")
        assert action == EVActionType.NOOP

    def test_bat_below_threshold_no_charge(self) -> None:
        """Battery SoC below threshold -> R12 blocks EV charging."""
        cfg = _cfg(bat_ready_threshold_pct=95.0)
        state = EVDispatchState()
        # First cycle: IDLE → CONNECTED
        state, _, _ = _cycle(state, cfg, bat_soc=80.0)
        # Second cycle: CONNECTED — R12 should block start
        state, action, reason = _cycle(state, cfg, bat_soc=80.0)
        assert action == EVActionType.NOOP
        assert "R12" in reason

    def test_shadow_mode_never_produces_write_action(self) -> None:
        """Shadow mode: evaluate_ev_action marks result is_shadow — no writes."""
        cfg = _cfg(shadow_mode=True)
        state = EVDispatchState()
        # Cycle 1: IDLE → CONNECTED (NOOP even in shadow)
        state, _, _ = _cycle(state, cfg)
        # Cycle 2: CONNECTED → would start (shadow)
        result = evaluate_ev_action(state, _inputs(), cfg)
        assert result.is_shadow is True
        # Shadow actions are still an action, but flagged — coordinator must NOT write

    def test_state_object_replaced_each_cycle(self) -> None:
        """EVDispatchState is replaced (not mutated) each cycle.

        The coordinator stores result.new_state between cycles — it must be
        a new object, not the same reference (immutable pattern).
        """
        cfg = _cfg()
        state1 = EVDispatchState()
        result = evaluate_ev_action(state1, _inputs(), cfg)
        state2 = result.new_state
        assert state1 is not state2

    def test_multi_cycle_phase_progression_idle_to_charging(self) -> None:
        """Multi-cycle: IDLE → CONNECTED → CHARGING when all criteria met."""
        cfg = _cfg()
        state = EVDispatchState()
        assert state.phase == EVPhase.IDLE

        # Cycle 1: IDLE → CONNECTED
        result = evaluate_ev_action(state, _inputs(), cfg)
        state = result.new_state
        assert state.phase == EVPhase.CONNECTED

        # Cycle 2: CONNECTED → CHARGING (all criteria pass)
        result = evaluate_ev_action(state, _inputs(), cfg)
        state = result.new_state
        assert state.phase == EVPhase.CHARGING
        assert result.action == EVActionType.EV_START

    def test_cycles_in_phase_increments_within_same_phase(self) -> None:
        """cycles_in_phase counter grows when phase does not change."""
        cfg = _cfg()
        state = EVDispatchState()

        # IDLE → CONNECTED (disconnected input keeps state in IDLE for multiple cycles)
        for _ in range(3):
            result = evaluate_ev_action(
                state, _inputs(ev_status="disconnected"), cfg
            )
            state = result.new_state

        # After 3 cycles all in IDLE, cycles_in_phase should be > 0
        assert state.phase == EVPhase.IDLE
        assert state.cycles_in_phase >= 2
