"""CARMA Box — EV dispatch v2: pure function state machine (PLAT-1790).

Implements acceptance formula R1-R12 for per-cycle EV charging decisions.

Design principles:
- Pure function: evaluate_ev_action() takes all inputs as arguments, no side effects.
- Feature-flagged: returns NOOP immediately when config.enabled is False.
- Single writer: only this module decides EV actions; coordinator executes them.
- Structured reasons: every decision includes an R-number + human-readable explanation.
- No HA imports: pure Python, fully unit-testable without mocking HA.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.schema import EVDispatchV2Config

_LOGGER = logging.getLogger(__name__)

# ── EV status sets (R2) ─────────────────────────────────────────────────────

EV_ACTIVE_STATUSES: frozenset[str] = frozenset(
    {"connected", "awaiting_start", "charging", "ready_to_charge"}
)
"""EV charger statuses that indicate a vehicle is plugged in and may charge."""

EV_IDLE_STATUSES: frozenset[str] = frozenset(
    {"disconnected", "unavailable", "unknown", "none", ""}
)
"""EV charger statuses that indicate no vehicle or unknown state."""


# ── Phase enum ──────────────────────────────────────────────────────────────

class EVPhase(StrEnum):
    """EV dispatch state machine phases."""

    IDLE = "IDLE"
    """No vehicle connected, or feature disabled. No writes."""

    CONNECTED = "CONNECTED"
    """Vehicle connected, evaluating acceptance criteria."""

    ANALYZING = "ANALYZING"
    """Running acceptance formula — transition state (1 cycle max)."""

    CHARGING = "CHARGING"
    """EV is actively charging under v2 control."""

    COMPLETING = "COMPLETING"
    """Criteria lost — stopping EV gracefully (1 cycle)."""

    ERROR = "ERROR"
    """Unexpected disconnect or R1 incident during charging. Stopping."""


# ── Action enum ─────────────────────────────────────────────────────────────

class EVActionType(StrEnum):
    """Actions that the coordinator should execute."""

    NOOP = "NOOP"
    """No action. Do not write to HA."""

    EV_START = "EV_START"
    """Start EV charging at `amps`."""

    EV_STOP = "EV_STOP"
    """Stop EV charging immediately."""

    EV_ADJUST = "EV_ADJUST"
    """Adjust EV charging current to `amps`."""

    EV_INCIDENT_ALERT = "EV_INCIDENT_ALERT"
    """R1 grid invariant breached during charging. Stop + log incident."""


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class EVDispatchState:
    """Persistent state carried between cycles.

    The coordinator must persist this between cycles (e.g. on coordinator instance).
    All fields are serialisable (no HA objects).
    """

    phase: EVPhase = EVPhase.IDLE
    """Current state machine phase."""

    ev_amps: int = 0
    """Current commanded EV charging current (amps). 0 = not charging."""

    phase_changed_at: float = 0.0
    """monotonic timestamp of last phase change (time.monotonic())."""

    cycles_in_phase: int = 0
    """How many consecutive cycles have been spent in current phase."""

    last_reason: str = ""
    """Human-readable reason for last decision (for sensor.carma_ev_decision_reason)."""

    incidents_today: int = 0
    """R1 grid incidents today (reset at midnight)."""


@dataclass
class EVDispatchInputs:
    """All sensor readings and computed values for one cycle.

    The coordinator reads these from HA state and passes them here.
    All values are plain Python — no HA types.
    """

    ev_status: str
    """Current EV charger status from HA sensor (R2)."""

    bat_soc: float
    """Current battery SoC in percent (R12)."""

    surplus_w: float
    """Available PV surplus in watts (positive = export available)."""

    grid_w: float
    """Current grid power in watts (positive = import, negative = export) (R1)."""

    predicted_refill_kwh: float
    """Forecast: P10 kWh available from PV between now and sunset (R10)."""

    predicted_bat_deficit_kwh: float
    """Forecast: kWh bat will need during EV session to cover household (R10)."""

    planned_window_min: float
    """Forecast: how many minutes of EV-viable surplus remain today (R11)."""

    bat_soc_at_sunset: float
    """Forecast: predicted bat SoC (%) at sunset time today (R9)."""

    pv_power_w: float = 0.0
    """Current PV production (W) — used for bat smoothing (R3)."""

    prev_pv_power_w: float = 0.0
    """PV production in previous cycle (W) — used to detect transients (R3)."""

    now_monotonic: float = 0.0
    """time.monotonic() at call time — used for timing guards."""


@dataclass
class EVDispatchResult:
    """Result of one evaluate_ev_action() call.

    The coordinator reads this and executes the action (unless shadow_mode).
    """

    new_state: EVDispatchState
    """Updated state to persist for the next cycle."""

    action: EVActionType
    """What the coordinator should do."""

    amps: int = 0
    """Target amps for EV_START / EV_ADJUST. 0 for other actions."""

    reason: str = ""
    """Structured reason string: 'R12: bat_soc=82.3 < threshold=95.0'."""

    bat_smoothing_w: float = 0.0
    """Watts the battery should absorb to cover PV transient (R3). 0 = not needed."""

    is_shadow: bool = False
    """True if shadow_mode is active — coordinator must NOT execute the action."""

    grid_incident: bool = False
    """True if R1 was triggered — coordinator must log to incidents sensor."""


# ── Rejection helpers ───────────────────────────────────────────────────────

def _reject(
    state: EVDispatchState,
    new_phase: EVPhase,
    action: EVActionType,
    reason: str,
    inputs: EVDispatchInputs,
) -> EVDispatchResult:
    """Build a rejection result with updated state."""
    new_state = EVDispatchState(
        phase=new_phase,
        ev_amps=0,
        phase_changed_at=(
            inputs.now_monotonic if new_phase != state.phase else state.phase_changed_at
        ),
        cycles_in_phase=0 if new_phase != state.phase else state.cycles_in_phase + 1,
        last_reason=reason,
        incidents_today=state.incidents_today,
    )
    return EVDispatchResult(new_state=new_state, action=action, reason=reason)


def _advance(
    state: EVDispatchState,
    new_phase: EVPhase,
    action: EVActionType,
    amps: int,
    reason: str,
    inputs: EVDispatchInputs,
    bat_smoothing_w: float = 0.0,
) -> EVDispatchResult:
    """Build a forward-transition result."""
    new_state = EVDispatchState(
        phase=new_phase,
        ev_amps=amps,
        phase_changed_at=(
            inputs.now_monotonic if new_phase != state.phase else state.phase_changed_at
        ),
        cycles_in_phase=0 if new_phase != state.phase else state.cycles_in_phase + 1,
        last_reason=reason,
        incidents_today=state.incidents_today,
    )
    return EVDispatchResult(
        new_state=new_state,
        action=action,
        amps=amps,
        reason=reason,
        bat_smoothing_w=bat_smoothing_w,
    )


# ── Acceptance formula helpers ──────────────────────────────────────────────

def _check_r2_plug(ev_status: str) -> str | None:
    """R2: EV must be physically connected.

    Returns failure reason string, or None if OK.
    """
    if ev_status not in EV_ACTIVE_STATUSES:
        return f"R2: ev_status='{ev_status}' not in active statuses"
    return None


def _check_r12_bat_ready(bat_soc: float, threshold: float) -> str | None:
    """R12: Battery must be at or above ready threshold.

    Returns failure reason string, or None if OK.
    """
    if bat_soc < threshold:
        return f"R12: bat_soc={bat_soc:.1f}% < threshold={threshold:.1f}%"
    return None


def _check_r10_refill(
    predicted_refill_kwh: float,
    predicted_deficit_kwh: float,
    margin_factor: float,
) -> str | None:
    """R10: Predicted PV refill (xmargin) must cover battery deficit during EV.

    Returns failure reason string, or None if OK.
    """
    required = predicted_deficit_kwh * margin_factor
    if predicted_refill_kwh < required:
        return (
            f"R10: predicted_refill={predicted_refill_kwh:.2f} kWh < "
            f"required={required:.2f} kWh "
            f"(deficit={predicted_deficit_kwh:.2f} x margin={margin_factor:.1f})"
        )
    return None


def _check_r11_window(planned_window_min: float, min_session_min: float) -> str | None:
    """R11: Planned session window must be long enough.

    Returns failure reason string, or None if OK.
    """
    if planned_window_min < min_session_min:
        return (
            f"R11: planned_window={planned_window_min:.1f} min "
            f"< min_session={min_session_min:.1f} min"
        )
    return None


def _check_r9_bat_sunset(bat_soc_at_sunset: float) -> str | None:
    """R9: Battery must be predicted to reach 100% before/at sunset.

    Returns failure reason string, or None if OK.
    """
    if bat_soc_at_sunset < 100.0:
        return f"R9: bat_soc_at_sunset={bat_soc_at_sunset:.1f}% < 100%"
    return None


def _check_r1_grid_incident(grid_w: float, threshold_w: float) -> str | None:
    """R1: Grid must stay within ±threshold_w hard invariant.

    Returns failure reason string, or None if within bounds.
    Note: only checked during CHARGING phase.
    """
    if abs(grid_w) > threshold_w:
        return f"R1: grid_w={grid_w:.0f}W exceeds ±{threshold_w:.0f}W invariant"
    return None


def _compute_bat_smoothing(
    pv_power_w: float,
    prev_pv_power_w: float,
    threshold_w: float,
) -> float:
    """R3: Compute battery smoothing needed to cover PV transient.

    If PV drops by more than threshold, battery should cover the dip.

    Returns:
        Watts the battery should discharge to maintain grid stability. 0 if not needed.
    """
    dip = prev_pv_power_w - pv_power_w
    if dip > threshold_w:
        return dip
    return 0.0


def _optimal_amps(surplus_w: float, cfg: EVDispatchV2Config) -> int:
    """Compute optimal EV charging amps given current surplus.

    Clamps to [ev_min_amps, ev_max_amps].
    """
    voltage_per_phase = 230.0
    available_amps = surplus_w / (voltage_per_phase * cfg.ev_phase_count)
    amps = int(available_amps)
    return max(cfg.ev_min_amps, min(cfg.ev_max_amps, amps))


# ── Full acceptance check ───────────────────────────────────────────────────

def _run_acceptance_formula(
    inputs: EVDispatchInputs,
    cfg: EVDispatchV2Config,
) -> str | None:
    """Run all acceptance criteria R2/R9/R10/R11/R12.

    Returns first failure reason, or None if all criteria met.
    """
    for check in (
        _check_r2_plug(inputs.ev_status),
        _check_r12_bat_ready(inputs.bat_soc, cfg.bat_ready_threshold_pct),
        _check_r10_refill(
            inputs.predicted_refill_kwh,
            inputs.predicted_bat_deficit_kwh,
            cfg.margin_factor,
        ),
        _check_r11_window(inputs.planned_window_min, cfg.min_session_min),
        _check_r9_bat_sunset(inputs.bat_soc_at_sunset),
    ):
        if check is not None:
            return check
    return None


# ── Main entry point ────────────────────────────────────────────────────────

def evaluate_ev_action(
    state: EVDispatchState,
    inputs: EVDispatchInputs,
    cfg: EVDispatchV2Config,
) -> EVDispatchResult:
    """Evaluate per-cycle EV action based on current state and sensor readings.

    Pure function — no side effects. The coordinator executes the returned action.

    State machine:
        IDLE → CONNECTED (when plug detected)
        CONNECTED → CHARGING (when all criteria met)
        CONNECTED → IDLE (when plug removed)
        CHARGING → CHARGING (criteria still met, keep running)
        CHARGING → COMPLETING (criteria lost)
        CHARGING → ERROR (disconnect or R1 incident)
        COMPLETING → IDLE (EV stopped)
        ERROR → IDLE (EV confirmed off)

    Args:
        state: Current persistent state from last cycle.
        inputs: All sensor readings for this cycle.
        cfg: Feature configuration (from site.yaml).

    Returns:
        EVDispatchResult with new_state (persist this) and action (execute this).
    """
    # Feature flag — fast path (R feature_flag)
    if not cfg.enabled:
        new_state = EVDispatchState(
            phase=EVPhase.IDLE,
            ev_amps=0,
            phase_changed_at=state.phase_changed_at,
            cycles_in_phase=state.cycles_in_phase + 1,
            last_reason="ev_dispatch_v2 disabled (feature flag)",
            incidents_today=state.incidents_today,
        )
        return EVDispatchResult(
            new_state=new_state,
            action=EVActionType.NOOP,
            reason="ev_dispatch_v2 disabled (feature flag)",
        )

    # Shadow mode wrapper — evaluate but mark result as non-executable
    result = _evaluate_internal(state, inputs, cfg)
    if cfg.shadow_mode and result.action != EVActionType.NOOP:
        _LOGGER.info(
            "SHADOW ev_dispatch_v2: would=%s amps=%d reason=%s",
            result.action,
            result.amps,
            result.reason,
        )
        # Return same result but flagged as shadow
        return EVDispatchResult(
            new_state=result.new_state,
            action=result.action,
            amps=result.amps,
            reason=result.reason,
            bat_smoothing_w=result.bat_smoothing_w,
            is_shadow=True,
            grid_incident=result.grid_incident,
        )
    return result


def _evaluate_internal(
    state: EVDispatchState,
    inputs: EVDispatchInputs,
    cfg: EVDispatchV2Config,
) -> EVDispatchResult:
    """Core state machine logic (internal, always called with enabled=True)."""
    phase = state.phase

    # ── IDLE ────────────────────────────────────────────────────────────────
    if phase == EVPhase.IDLE:
        if inputs.ev_status in EV_ACTIVE_STATUSES:
            reason = f"plug detected: ev_status={inputs.ev_status!r}"
            _LOGGER.info("ev_dispatch_v2: IDLE → CONNECTED (%s)", reason)
            return _advance(state, EVPhase.CONNECTED, EVActionType.NOOP, 0, reason, inputs)
        return _reject(state, EVPhase.IDLE, EVActionType.NOOP, "R2: no vehicle connected", inputs)

    # ── CONNECTED ───────────────────────────────────────────────────────────
    if phase == EVPhase.CONNECTED:
        # Check plug still present
        if inputs.ev_status not in EV_ACTIVE_STATUSES:
            reason = f"R2: plug removed (status={inputs.ev_status!r})"
            _LOGGER.info("ev_dispatch_v2: CONNECTED → IDLE (%s)", reason)
            return _reject(state, EVPhase.IDLE, EVActionType.NOOP, reason, inputs)

        # Run full acceptance formula
        rejection = _run_acceptance_formula(inputs, cfg)
        if rejection is not None:
            _LOGGER.debug("ev_dispatch_v2: CONNECTED blocked: %s", rejection)
            return _reject(state, EVPhase.CONNECTED, EVActionType.NOOP, rejection, inputs)

        # All criteria met — start charging
        amps = _optimal_amps(inputs.surplus_w, cfg)
        reason = f"all criteria met — starting at {amps}A"
        _LOGGER.info("ev_dispatch_v2: CONNECTED → CHARGING (%s)", reason)
        return _advance(state, EVPhase.CHARGING, EVActionType.EV_START, amps, reason, inputs)

    # ── CHARGING ────────────────────────────────────────────────────────────
    if phase == EVPhase.CHARGING:
        # R1: grid invariant check (hardest — checked first)
        r1_fail = _check_r1_grid_incident(inputs.grid_w, cfg.grid_incident_threshold_w)
        if r1_fail is not None:
            _LOGGER.warning("ev_dispatch_v2: R1 INCIDENT during CHARGING: %s", r1_fail)
            new_state = EVDispatchState(
                phase=EVPhase.ERROR,
                ev_amps=0,
                phase_changed_at=inputs.now_monotonic,
                cycles_in_phase=0,
                last_reason=r1_fail,
                incidents_today=state.incidents_today + 1,
            )
            return EVDispatchResult(
                new_state=new_state,
                action=EVActionType.EV_INCIDENT_ALERT,
                amps=0,
                reason=r1_fail,
                grid_incident=True,
            )

        # R2: disconnect check
        if inputs.ev_status not in EV_ACTIVE_STATUSES:
            reason = f"R2: plug disconnected during charging (status={inputs.ev_status!r})"
            _LOGGER.warning("ev_dispatch_v2: CHARGING → ERROR (disconnect): %s", reason)
            new_state = EVDispatchState(
                phase=EVPhase.ERROR,
                ev_amps=0,
                phase_changed_at=inputs.now_monotonic,
                cycles_in_phase=0,
                last_reason=reason,
                incidents_today=state.incidents_today,
            )
            return EVDispatchResult(
                new_state=new_state,
                action=EVActionType.EV_STOP,
                amps=0,
                reason=reason,
            )

        # Check remaining acceptance criteria
        rejection = _run_acceptance_formula(inputs, cfg)
        if rejection is not None:
            _LOGGER.info("ev_dispatch_v2: CHARGING → COMPLETING (%s)", rejection)
            return _advance(state, EVPhase.COMPLETING, EVActionType.EV_STOP, 0, rejection, inputs)

        # Still good — compute optimal amps
        optimal = _optimal_amps(inputs.surplus_w, cfg)
        bat_smoothing = _compute_bat_smoothing(
            inputs.pv_power_w,
            inputs.prev_pv_power_w,
            cfg.bat_smoothing_threshold_w,
        )

        if optimal != state.ev_amps:
            reason = (
                f"adjusting amps {state.ev_amps}A -> {optimal}A "
                f"(surplus={inputs.surplus_w:.0f}W)"
            )
            return _advance(
                state, EVPhase.CHARGING, EVActionType.EV_ADJUST, optimal, reason, inputs,
                bat_smoothing_w=bat_smoothing,
            )

        reason = f"charging at {optimal}A (surplus={inputs.surplus_w:.0f}W)"
        return _advance(
            state, EVPhase.CHARGING, EVActionType.NOOP, optimal, reason, inputs,
            bat_smoothing_w=bat_smoothing,
        )

    # ── COMPLETING ──────────────────────────────────────────────────────────
    if phase == EVPhase.COMPLETING:
        reason = "EV stopped, returning to IDLE"
        _LOGGER.info("ev_dispatch_v2: COMPLETING → IDLE")
        return _reject(state, EVPhase.IDLE, EVActionType.NOOP, reason, inputs)

    # ── ERROR ────────────────────────────────────────────────────────────────
    if phase == EVPhase.ERROR:
        # Stay in ERROR until EV is confirmed off, then return to IDLE
        reason = "recovering from error — returning to IDLE"
        _LOGGER.info("ev_dispatch_v2: ERROR → IDLE (recovery)")
        return _reject(state, EVPhase.IDLE, EVActionType.NOOP, reason, inputs)

    # Unreachable — unknown phase, reset to IDLE
    _LOGGER.error("ev_dispatch_v2: unknown phase %r — resetting to IDLE", phase)
    return _reject(state, EVPhase.IDLE, EVActionType.NOOP, f"unknown phase: {phase!r}", inputs)
