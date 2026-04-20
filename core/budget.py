"""Unified Power Budget Allocator — EN dispatcher för ALL energi-routing.

PLAT-1686 (v2.11 S4).

Ersätter de tre oberoende dispatchers (_compute_charge_plan, EVSurplusController,
SurplusDispatch) med EN centraliserad budget-allokering per cykel.

Principer:
  1. EN budget, EN allocator — inga parallella dispatch-paths
  2. Dag 06-22: surplus = max(0, PV - house). ALDRIG grid-import för laddning.
  3. Natt 22-06: surplus = grid-budget (tak - weighted)
  4. Prio FM 06-12: EV → bat → consumers
  5. Prio EM 12-22: bat → EV → consumers
  6. Consumers ALDRIG om bat < 100% (EM)
  7. Grid ±100W hysteres (target 0W)
  8. EV ramp ±1A per cykel baserat på grid-feedback

Pure module — no I/O. Caller (engine) executes returned commands.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from core.grid_tuner import (
    GridRollingState,
    GridTunerConfig,
    tune_grid_delta,
)
from core.models import Command, CommandType, ConsumerState, EMSMode
from core.zero_grid import BatLimits, BatSnapshot, plan_zero_grid

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DAYTIME_START_H: int = 6
_DAYTIME_END_H: int = 22
_FM_END_H: int = 12
_EVENING_DISCHARGE_START_H: int = 17
_EVENING_DISCHARGE_END_H: int = 20

_GRID_TARGET_W: float = 0.0
# PLAT-1715: max allowed grid deviation. User rule: ±100W max, aggressive
# correction above ±50W.
_GRID_TOLERANCE_W: float = 100.0
_GRID_AGGRESSIVE_W: float = 50.0
_PCT_FACTOR: float = 100.0

# EV power per amp (3-phase 230V)
_EV_W_PER_AMP: float = 3.0 * 230.0  # 690W per amp

_RULE_ID: str = "BUDGET"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetConfig:
    """Configuration for the power budget allocator."""

    ev_min_amps: int = 6
    ev_max_amps: int = 16
    # bat_soc_full_pct: battery is physically full (stops "bat discharge support" path).
    # bat_charge_stop_soc_pct: stop charging at this SoC — must match S8 PV_SURPLUS entry
    # in state_machine.surplus_entry_soc_pct, otherwise a dead zone exists where the
    # state machine says "surplus mode" but budget keeps charging → export grows.
    bat_soc_full_pct: float = 100.0
    bat_charge_stop_soc_pct: float = 95.0
    # bat_spread_max_pct: user-invariant for SoC spread across batteries.
    # Above this diff zero-grid uses the aggressive primary/secondary split
    # (full charge to lower-SoC, full discharge from higher-SoC, with
    # overflow spill). 1.0 pp matches the user rule "SoC diff > 1 % = P1".
    # bat_aggressive_spread_pct kept for backward-compat API; both default
    # to the same value so tests keep their boundary semantics clear.
    bat_spread_max_pct: float = 1.0
    bat_aggressive_spread_pct: float = 1.0
    bat_lower_ratio: float = 0.8
    bat_higher_ratio: float = 0.2
    # EV ramp tröghet: ramp UP kräver N konsekutiva export-cykler
    ev_ramp_up_hold_cycles: int = 2  # trög upp (moln kan återkomma)
    ev_ramp_down_hold_cycles: int = 1  # snabb ner (skydda mot import)
    # Bat discharge support for EV
    bat_discharge_support: bool = True
    # Evening cutoff — bat prio after this hour
    evening_cutoff_h: int = 17
    # Bat discharge minimum SoC — absolute floor (GoodWe AC output cut)
    bat_discharge_min_soc_pct: float = 15.0
    # Default bat capacity fallback if caller omits bat_caps
    bat_default_cap_kwh: float = 10.0
    # PLAT-1718: physical rate limits used by the zero-grid controller when
    # the caller does not supply per-battery values. 5 kW matches the GoodWe
    # ET-10 default; override via site.yaml if different hardware is used.
    bat_default_max_charge_w: int = 5000
    bat_default_max_discharge_w: int = 5000
    # PLAT-1715 R7: consumer cascade tunables (no magic numbers).
    # cascade_cooldown_s: minimum seconds between two switches of the same
    #   consumer — prevents flapping when the grid signal is noisy.
    # cascade_sustained_cycles: how many consecutive export cycles must pass
    #   before starting the next consumer in the priority list.
    cascade_cooldown_s: float = 60.0
    cascade_sustained_cycles: int = 2
    # PLAT-1738: a bat counts as "saturated" (for cascade bat-at-max guard)
    # when its allocated charge-limit is within this many W of the physical
    # max OR when SoC >= bat_charge_stop_soc_pct. Keep conservative so
    # transient small allocations don't trigger false-positives.
    bat_at_max_headroom_w: int = 500
    # PLAT-1696 step 1 grid-smoothing window. Median-of-N rejects
    # single-cycle spurious readings from the HA grid sensor (observed
    # live: alternating 12.9 kW ↔ 2.5 kW every 15 s). Median (not mean)
    # so one outlier in N is fully filtered. 3 is a good default — large
    # enough to reject isolated spikes, small enough to react to real
    # load shifts within ~2 cycles.
    grid_smoothing_window: int = 3
    # PLAT-1737: tiered grid-sensor fine-tuner. Applied AFTER zero_grid so
    # zero_grid owns mode/direction (stable) while the tuner nudges the
    # allocated power to kill grid-drift within-cycle. Default disabled —
    # enable via site.yaml once a site has stabilised on zero_grid alone.
    grid_tuner: GridTunerConfig = field(default_factory=GridTunerConfig)


# ---------------------------------------------------------------------------
# Input / Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetInput:
    """All facts needed for one budget cycle."""

    now: datetime
    grid_power_w: float  # positive = import, negative = export
    pv_power_w: float  # total PV production
    house_load_w: float  # house consumption (excl bat/EV)
    ev_connected: bool
    ev_charging: bool
    ev_current_amps: int
    ev_soc_pct: float
    ev_target_soc_pct: float
    bat_socs: dict[str, float]  # battery_id → SoC %
    bat_caps: dict[str, float]  # battery_id → capacity kWh
    bat_powers: dict[str, float]  # battery_id → current power W
    bat_modes: dict[str, str]  # battery_id → current EMS mode
    pv_remaining_kwh: float = 0.0  # Solcast remaining today
    house_remaining_kwh: float = 0.0  # estimated house consumption remaining
    # PLAT-1715: consumer list for unified priority cascade.
    # Empty tuple (default) → no cascade, legacy behaviour.
    consumers: tuple[ConsumerState, ...] = ()
    # PLAT-1724: Nordpool spot + forecast horizon for the arbitrage planner.
    # current_price_ore is the spot at ``now`` hour; future_prices_ore starts
    # at the NEXT hour (index 0 = now+1h). Empty tuple → planner skipped.
    current_price_ore: float = 0.0
    future_prices_ore: tuple[float, ...] = ()


@dataclass
class BudgetState:
    """Mutable state persisted between cycles (caller owns)."""

    consecutive_export_cycles: int = 0
    consecutive_import_cycles: int = 0
    # PLAT-1740: Budget's INTENDED EV state — compared against, not
    # HA-reported ``ev_charging``. HA/Easee can flap ev_charging cycle-
    # by-cycle (plug sensor glitch, integration restart). Comparing to
    # intended state makes START/STOP emission idempotent.
    intended_ev_enabled: bool = False
    # Last amps value Budget wrote (source of truth for SET_EV_CURRENT
    # idempotency). Renamed 2026-04-20 from "last HA value" semantic to
    # "intended" — name kept stable for back-compat with call sites.
    ev_current_amps: int = 0
    # PLAT-1715: cooldown per consumer (monotonic seconds) to prevent flapping.
    # Key = consumer_id, value = monotonic timestamp of last switch.
    consumer_last_switch_ts: dict[str, float] = field(default_factory=dict)
    # PLAT-1696 step 1: rolling window of recent grid-power readings.
    # ``allocate()`` pushes the raw grid_power_w into this deque each
    # cycle, then feeds the MEDIAN of the window to zero_grid so single
    # spurious spikes (HA sensor glitch) are rejected.
    grid_history_w: list[float] = field(default_factory=list)
    # PLAT-1737: 5-min rolling window for grid-tuner anti-flap guard.
    # Updated EVERY cycle (regardless of grid_tuner.enabled) so the data
    # is ready the moment the tuner is enabled without a warm-up wait.
    grid_rolling: GridRollingState = field(default_factory=GridRollingState)


@dataclass(frozen=True)
class BudgetResult:
    """Output of one budget cycle."""

    commands: list[Command]
    ev_target_amps: int
    bat_allocations: dict[str, int]  # battery_id → charge limit W
    bat_discharge_w: int = 0  # total bat discharge for support
    reason: str = ""
    # PLAT-1751: batteries whose SoC < floor — engine calls
    # mode_change_manager.emergency_mode_change() for each.
    emergency_recovery: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_daytime(hour: int) -> bool:
    return _DAYTIME_START_H <= hour < _DAYTIME_END_H


def _is_fm(hour: int) -> bool:
    return _DAYTIME_START_H <= hour < _FM_END_H


def _available_surplus_w(inp: BudgetInput) -> float:
    """Calculate available PV surplus (W). NEVER negative during daytime."""
    surplus = inp.pv_power_w - inp.house_load_w
    if _is_daytime(inp.now.hour):
        # Closed-loop grid-feedback correction (PLAT-1715).
        # grid_power_w < 0 → exporting → bump surplus so bat absorbs more.
        # grid_power_w > 0 → importing → lower surplus so bat limit shrinks.
        if inp.grid_power_w < 0:
            surplus += -inp.grid_power_w
        elif inp.grid_power_w > _GRID_AGGRESSIVE_W:
            surplus -= inp.grid_power_w
        return max(0.0, surplus)
    return surplus


def _all_bat_full(inp: BudgetInput, cfg: BudgetConfig) -> bool:
    return all(s >= cfg.bat_soc_full_pct for s in inp.bat_socs.values())


def _ev_power_at_amps(amps: int) -> float:
    return float(amps) * _EV_W_PER_AMP


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def allocate(
    inp: BudgetInput,
    cfg: BudgetConfig,
    state: BudgetState | None = None,
) -> BudgetResult:
    """Allocate power budget for one cycle.

    Returns commands + target amps + bat allocations.
    Caller executes via CommandExecutor.
    """
    if state is None:
        state = BudgetState()

    cmds: list[Command] = []
    hour = inp.now.hour
    daytime = _is_daytime(hour)
    fm = _is_fm(hour)
    surplus = _available_surplus_w(inp)
    remaining = surplus
    ev_target = 0
    bat_alloc: dict[str, int] = {}
    bat_discharge = 0
    reasons: list[str] = []
    ev_wants_charge = inp.ev_connected and inp.ev_soc_pct < inp.ev_target_soc_pct

    # Update grid feedback counters
    if inp.grid_power_w < -_GRID_TOLERANCE_W:
        state.consecutive_export_cycles += 1
        state.consecutive_import_cycles = 0
    elif inp.grid_power_w > _GRID_TOLERANCE_W:
        state.consecutive_import_cycles += 1
        state.consecutive_export_cycles = 0
    else:
        state.consecutive_export_cycles = 0
        state.consecutive_import_cycles = 0

    # Calculate dynamic bat_min_soc — how low bat can go
    # and still reach 100% from remaining PV
    bat_total_cap = sum(inp.bat_caps.values()) or 1.0
    pv_for_bat = max(0.0, inp.pv_remaining_kwh - inp.house_remaining_kwh)
    bat_min_soc_dynamic = max(
        0.0,
        cfg.bat_soc_full_pct - (pv_for_bat / bat_total_cap * _PCT_FACTOR),
    )
    bat_avg_soc = sum(inp.bat_socs.values()) / len(inp.bat_socs) if inp.bat_socs else 0.0
    bat_can_support_ev = bat_avg_soc > bat_min_soc_dynamic

    # ----- PRIORITY ALLOCATION -----

    # PLAT-1718: The zero-grid controller OWNS bat mode + limit during
    # the whole active daytime window (06:00–20:00). It drives
    # grid → 0 W every cycle using the measured ``grid_power_w`` instead
    # of the pv-house model (which can lag or go stale). The legacy
    # evening_discharge branch was a one-shot ``target = max(0, grid)``
    # that ignored the current bat power — this caused a ±2.8 kW
    # oscillation in the 17-20 window. Zero-grid is closed-loop on the
    # measured bat + grid state, so it converges within the deadband.
    #
    # Outside this window:
    #   20:00–22:00 → bat standby (preserve capacity for morning peak)
    #   22:00–06:00 → night controller takes over in engine
    # User invariant 2026-04-18: grid must stay within ±100 W 24/7.
    # Zero-grid controller therefore OWNS bat mode/limit every cycle,
    # day or night. Night grid-charge decisions still flow through the
    # NightPlanner → PlanExecutor path and can override via explicit
    # commands when price triggers fire (PLAT-1722 candidate in scope).
    zero_grid_active = True
    zero_grid_plan = None
    if zero_grid_active:
        # PLAT-1696 step 1: grid-sensor smoothing. Push raw reading into
        # the rolling window and feed the MEDIAN to zero_grid. One-cycle
        # spikes (e.g. HA sensor glitch) are rejected, real load shifts
        # pass through after window//2 + 1 cycles.
        state.grid_history_w.append(inp.grid_power_w)
        if len(state.grid_history_w) > cfg.grid_smoothing_window:
            state.grid_history_w.pop(0)
        sorted_history = sorted(state.grid_history_w)
        grid_smoothed_w = sorted_history[len(sorted_history) // 2]

        zg_bats = [
            BatSnapshot(
                battery_id=bid,
                power_w=inp.bat_powers.get(bid, 0.0),
                soc_pct=soc,
            )
            for bid, soc in inp.bat_socs.items()
        ]
        zg_limits = {
            bid: BatLimits(
                max_charge_w=cfg.bat_default_max_charge_w,
                max_discharge_w=cfg.bat_default_max_discharge_w,
                soc_min_pct=cfg.bat_discharge_min_soc_pct,
                soc_max_pct=cfg.bat_charge_stop_soc_pct,
            )
            for bid in inp.bat_socs
        }
        # PLAT-1754 verified (no-op): cfg value is ALWAYS forwarded
        # explicitly — plan_zero_grid's parameter default (5.0) is
        # intentionally never reached from this call site.
        # Do NOT remove the keyword argument: that would silently revert
        # to 5.0 and break the user invariant "SoC diff > 1 pp = P1"
        # (BudgetConfig.bat_aggressive_spread_pct defaults to 1.0).
        zero_grid_plan = plan_zero_grid(
            grid_power_w=grid_smoothed_w,
            bats=zg_bats,
            limits_by_id=zg_limits,
            spread_aggressive_pct=cfg.bat_aggressive_spread_pct,
        )
        bat_alloc = dict(zero_grid_plan.limits_w)
        bat_discharge = sum(
            lim
            for bid, lim in zero_grid_plan.limits_w.items()
            if zero_grid_plan.modes[bid] == "discharge_pv"
        )
        reasons.append(zero_grid_plan.reason)

        # PLAT-1737: grid_tuner — fine-tune bat_alloc using RAW grid
        # (not smoothed) for fast response to moln-dip transients.
        # Rolling window is always updated so mode-change guard is ready
        # when enabled. Delta distribution respects each bat's mode:
        #   charge-mode: positive grid delta (import) reduces alloc,
        #                negative (export) increases alloc.
        #   discharge-mode: opposite.
        # All limits clamped to [0, bat_default_max_{charge,discharge}_w].
        state.grid_rolling.add(
            time.monotonic(),
            inp.grid_power_w,
            cfg.grid_tuner.rolling_window_s,
        )
        if cfg.grid_tuner.enabled:
            tune_delta = tune_grid_delta(inp.grid_power_w, cfg.grid_tuner)
            if tune_delta != 0:
                active_bats = [
                    bid
                    for bid, mode in zero_grid_plan.modes.items()
                    if mode in ("charge_battery", "discharge_pv")
                ]
                if active_bats:
                    per_bat_delta = tune_delta // len(active_bats)
                    for bid in active_bats:
                        mode = zero_grid_plan.modes[bid]
                        current = bat_alloc[bid]
                        if mode == "charge_battery":
                            # Charge semantics inverts the sign.
                            new_alloc = current - per_bat_delta
                            cap = cfg.bat_default_max_charge_w
                        else:  # discharge_pv
                            new_alloc = current + per_bat_delta
                            cap = cfg.bat_default_max_discharge_w
                        bat_alloc[bid] = max(0, min(cap, new_alloc))
                    reasons.append(
                        f"grid_tuner: raw={inp.grid_power_w:.0f}W "
                        f"delta={tune_delta:+d}W "
                        f"per_bat={per_bat_delta:+d}W over {len(active_bats)} bats",
                    )
                    # Keep bat_discharge in sync after tuning
                    bat_discharge = sum(
                        alloc
                        for bid, alloc in bat_alloc.items()
                        if zero_grid_plan.modes[bid] == "discharge_pv"
                    )

    if not daytime:
        # Night — defers to night controller
        reasons.append("NIGHT: defers to night controller")

    elif (
        not zero_grid_active
        and _EVENING_DISCHARGE_START_H <= hour < _EVENING_DISCHARGE_END_H
        and bat_avg_soc > cfg.bat_discharge_min_soc_pct
    ):
        # Fallback legacy evening_discharge — only reachable when the
        # zero-grid controller is disabled (feature flag / night branch).
        # Kept as a safety net; emits proportional-by-capacity shares.
        bat_discharge, bat_alloc = _allocate_evening_discharge(
            inp,
            cfg,
            state,
        )
        ev_target = 0
        reasons.append(
            f"EVENING_DISCHARGE(legacy): bat_discharge {bat_discharge}W"
            f" house={inp.house_load_w:.0f}W",
        )

    elif not zero_grid_active and hour >= cfg.evening_cutoff_h:
        # Evening 20-22 when zero-grid is off: bat standby, NO EV
        # (preserve for morning). During zero_grid_active the controller
        # holds the bat at grid=0 on its own — no override here.
        bat_alloc = {bid: 0 for bid in inp.bat_socs}
        ev_target = 0
        reasons.append("EVENING: bat standby, EV off")

    elif fm and ev_wants_charge:
        # FM 06-12: EV wants to charge. Zero-grid already wrote the bat
        # plan; the EV ramper handles its own ±1 A per cycle.
        ev_target, remaining = _allocate_ev_with_ramp(
            inp,
            remaining,
            cfg,
            state,
        )
        reasons.append(f"FM: EV target {ev_target}A")

    else:
        # EM 12-17: zero-grid owns bat; EV runs when it is capable of
        # pulling surplus. Either bat has room to support EV discharge,
        # or the bat is fully charged (in which case EV should consume
        # the remaining PV directly).
        if ev_wants_charge and (bat_can_support_ev or _all_bat_full(inp, cfg)):
            ev_target, remaining = _allocate_ev_with_ramp(
                inp,
                remaining,
                cfg,
                state,
            )
            reasons.append(f"EM: EV {ev_target}A")
        else:
            ev_target = 0
            if ev_wants_charge:
                reasons.append(
                    f"EM: bat {bat_avg_soc:.0f}%≤min" f"{bat_min_soc_dynamic:.0f}% EV off",
                )
            else:
                reasons.append("EM: EV idle")

    # ----- EMIT COMMANDS -----

    # EV commands — ONLY during daytime. At night the NightEVController
    # owns EV (weighted-peak shaving, ramp-hold windows, etc). If Budget
    # also emits here, the two controllers fight: observed live as
    # "stop_ev_charging (BUDGET)" → "Setting EV current 6A (NIGHT_EV)"
    # loop in the 22:46 cycles. Budget staying out of EV at night leaves
    # ev_target=0 but emits no START/STOP/SET commands.
    if daytime:
        # PLAT-1740: compare to Budget's INTENDED state, not HA-reported
        # ev_charging. HA flapping (plug sensor glitch, integration
        # restart) must not cause Budget to re-emit START/STOP in a loop.
        # State is flipped only when Budget actually emits the command.
        want_enabled = ev_target > 0
        if want_enabled and not state.intended_ev_enabled:
            cmds.append(
                Command(
                    command_type=CommandType.START_EV_CHARGING,
                    target_id="ev",
                    value=None,
                    rule_id=_RULE_ID,
                    reason="Budget: EV start",
                )
            )
            state.intended_ev_enabled = True
        if not want_enabled and state.intended_ev_enabled:
            cmds.append(
                Command(
                    command_type=CommandType.STOP_EV_CHARGING,
                    target_id="ev",
                    value=None,
                    rule_id=_RULE_ID,
                    reason="Budget: EV stop",
                )
            )
            state.intended_ev_enabled = False
        # SET_EV_CURRENT: compare to state.ev_current_amps (what Budget
        # last wrote), NOT inp.ev_current_amps (which is HA-reported and
        # can lag/flap). ev_current_amps is updated at end of tick().
        if want_enabled and ev_target != state.ev_current_amps:
            cmds.append(
                Command(
                    command_type=CommandType.SET_EV_CURRENT,
                    target_id="ev",
                    value=ev_target,
                    rule_id=_RULE_ID,
                    reason=f"Budget: EV {state.ev_current_amps}→{ev_target}A",
                )
            )

    # Bat commands — per-battery mode + limit.
    # PLAT-1718: when the zero-grid controller ran (daytime), its plan is
    # the source of truth — each bat may be charging, discharging, or
    # standing by independently. Night paths fall back to the legacy
    # 'all-discharge' / 'all-charge' collective decision.
    bat_modes_target: dict[str, str] = {}
    if zero_grid_plan is not None:
        # zero-grid plan returns: "charge_battery" / "discharge_pv" / "battery_standby"
        bat_modes_target.update(zero_grid_plan.modes)
    else:
        for bid, limit_w in bat_alloc.items():
            if bat_discharge > 0 and limit_w > 0:
                bat_modes_target[bid] = EMSMode.DISCHARGE_PV.value
            elif limit_w > 0:
                bat_modes_target[bid] = EMSMode.CHARGE_BATTERY.value
            else:
                bat_modes_target[bid] = EMSMode.BATTERY_STANDBY.value

    # PLAT-1714/1715: idempotent mode emission — only write SET_EMS_MODE when
    # the inverter is not already in the target mode. The 5-step mode-change
    # protocol clears the power limit during Step 1 PREPARE, so re-emitting
    # the same mode every cycle would keep pulling the limit back to 0.
    emergency_bats: frozenset[str] = (
        zero_grid_plan.emergency_recovery if zero_grid_plan is not None else frozenset()
    )

    for bid, limit_w in bat_alloc.items():
        target_mode = bat_modes_target.get(
            bid,
            EMSMode.BATTERY_STANDBY.value,
        )
        current_mode = inp.bat_modes.get(bid, "")
        # PLAT-1751: Emergency bats are handled by mode_change_manager.emergency_mode_change()
        # in the engine — budget must NOT emit SET_EMS_MODE for them (single-writer).
        if bid not in emergency_bats and current_mode != target_mode:
            cmds.append(
                Command(
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bid,
                    value=target_mode,
                    rule_id=_RULE_ID,
                    reason=(
                        f"Budget: {target_mode} {limit_w}W" if limit_w > 0 else "Budget: standby"
                    ),
                )
            )
        cmds.append(
            Command(
                command_type=CommandType.SET_EMS_POWER_LIMIT,
                target_id=bid,
                value=limit_w,
                rule_id=_RULE_ID,
                reason=(f"Budget: limit {limit_w}W" if limit_w > 0 else "Budget: standby limit=0"),
            )
        )
        # PLAT-1751: Emergency bats get fast_charging=True via emergency_mode_change()
        # (mode_change_manager owns fast_charging for recovery path). Only emit
        # SET_FAST_CHARGING=False for non-emergency bats to keep INV-3 intact.
        if bid not in emergency_bats:
            cmds.append(
                Command(
                    command_type=CommandType.SET_FAST_CHARGING,
                    target_id=bid,
                    value=False,
                    rule_id=_RULE_ID,
                    reason="INV-3: ensure fast_charging OFF",
                )
            )

    # PLAT-1715: unified consumer cascade runs after bat + EV allocation.
    cmds.extend(_cascade_consumers(inp, bat_alloc, state, cfg))

    # Update state
    state.ev_current_amps = ev_target

    reason = (
        " | ".join(reasons)
        + f" | grid={inp.grid_power_w:.0f}W surplus={surplus:.0f}W"
        + f" bat_min_soc={bat_min_soc_dynamic:.0f}%"
    )
    return BudgetResult(
        commands=cmds,
        ev_target_amps=ev_target,
        bat_allocations=bat_alloc,
        bat_discharge_w=bat_discharge,
        reason=reason,
        emergency_recovery=emergency_bats,
    )


# ---------------------------------------------------------------------------
# Allocation functions
# ---------------------------------------------------------------------------


def _allocate_evening_discharge(
    inp: BudgetInput,
    cfg: BudgetConfig,
    state: BudgetState,
) -> tuple[int, dict[str, int]]:
    """Evening discharge: bat covers grid import, targeting grid 0W.

    Pure grid-feedback control:
    - grid importing (positive) → discharge that amount
    - grid exporting (negative) → discharge 0 (bat giving too much)
    - grid ≈ 0 → discharge 0 (balanced)

    This is a simple proportional controller. The grid power already
    reflects the net of all sources (PV, bat, house, EV). If grid
    imports X watts, bat needs to discharge X watts to zero it out.

    Returns (total_discharge_w, per_battery_alloc).
    """
    # Discharge exactly what grid imports — nothing more, nothing less
    target_w = max(0.0, inp.grid_power_w)
    # grid_power_w > 0 = import → discharge that amount
    # grid_power_w <= 0 = export or balanced → no discharge needed

    total_discharge = int(target_w)
    if total_discharge <= 0:
        return 0, {bid: 0 for bid in inp.bat_socs}

    # Proportional shares per battery (simultaneous min_soc convergence)
    shares = _bat_discharge_shares(inp, cfg)
    alloc: dict[str, int] = {}
    for bid, share in shares.items():
        alloc[bid] = int(total_discharge * share)

    return total_discharge, alloc


def _bat_discharge_shares(
    inp: BudgetInput,
    cfg: BudgetConfig,
) -> dict[str, float]:
    """Proportional discharge shares so both bats reach min_soc simultaneously."""
    avail = {}
    for bid, soc in inp.bat_socs.items():
        cap = inp.bat_caps.get(bid, cfg.bat_default_cap_kwh)
        headroom = max(0.0, soc - cfg.bat_discharge_min_soc_pct)
        avail[bid] = headroom / _PCT_FACTOR * cap
    total = sum(avail.values())
    if total <= 0:
        return {bid: 0.0 for bid in inp.bat_socs}
    return {bid: a / total for bid, a in avail.items()}


def _allocate_ev_with_ramp(
    inp: BudgetInput,
    remaining_w: float,
    cfg: BudgetConfig,
    state: BudgetState,
) -> tuple[int, float]:
    """Allocate EV with asymmetric ramp tröghet. Returns (target_amps, remaining).

    Ramp UP: only after N consecutive export cycles (trög — moln kan återkomma)
    Ramp DOWN: immediate on import (snabb — skydda mot grid-import)
    """
    current = state.ev_current_amps

    if current > 0 and inp.ev_charging:
        # Already charging — grid-feedback ramp
        if (
            state.consecutive_import_cycles >= cfg.ev_ramp_down_hold_cycles
            and current > cfg.ev_min_amps
        ):
            target = current - 1
        elif (
            state.consecutive_export_cycles >= cfg.ev_ramp_up_hold_cycles
            and current < cfg.ev_max_amps
        ):
            target = current + 1
        else:
            target = current
    elif remaining_w >= _ev_power_at_amps(cfg.ev_min_amps):
        # Not charging but enough surplus to start
        target = cfg.ev_min_amps
    else:
        target = 0

    target = max(0, min(target, cfg.ev_max_amps))

    # For already-charging EV, only count the CHANGE
    if inp.ev_charging and target > 0:
        used = max(0.0, _ev_power_at_amps(target) - _ev_power_at_amps(current))
    else:
        used = _ev_power_at_amps(target)

    return target, remaining_w - used


def _allocate_ev(
    inp: BudgetInput,
    remaining_w: float,
    cfg: BudgetConfig,
) -> tuple[int, float, float]:
    """Allocate surplus to EV. Returns (target_amps, used_w, remaining_w).

    Ramp ±1A per cycle based on grid feedback:
    - grid < -tolerance → ramp up (absorb export)
    - grid > +tolerance → ramp down (reduce import)
    - else hold

    When EV is ALREADY charging, grid feedback drives the ramp.
    When EV is NOT charging, surplus must cover min_amps to start.
    """
    current = inp.ev_current_amps

    if current > 0 and inp.ev_charging:
        # Already charging — use grid feedback for ramp
        if inp.grid_power_w < -_GRID_TOLERANCE_W and current < cfg.ev_max_amps:
            target = current + 1
        elif inp.grid_power_w > _GRID_TOLERANCE_W and current > cfg.ev_min_amps:
            target = current - 1
        else:
            target = current
    elif remaining_w >= _ev_power_at_amps(cfg.ev_min_amps):
        # Not charging but enough surplus to start
        target = cfg.ev_min_amps
    else:
        target = 0

    # Clamp
    target = max(0, min(target, cfg.ev_max_amps))

    # For already-charging EV, used_w is the CHANGE from current
    # (grid already accounts for current draw)
    if inp.ev_charging and target > 0:
        used = _ev_power_at_amps(target) - _ev_power_at_amps(current)
        used = max(0.0, used)  # only count increase
    else:
        used = _ev_power_at_amps(target)

    return target, used, remaining_w - used


def _bat_at_max(
    bat_alloc: dict[str, int],
    bat_socs: dict[str, float],
    cfg: BudgetConfig,
) -> tuple[bool, str]:
    """PLAT-1738: True if no bat can absorb more charge.

    A bat is "at max" if either:
      - SoC is at or above ``bat_charge_stop_soc_pct`` (firmware stops), or
      - allocated charge-limit is within ``cfg.bat_at_max_headroom_w`` of
        ``cfg.bat_default_max_charge_w`` (fysisk mättnad denna cykel).

    Returns (all_at_max, reason_fragment). reason_fragment describes
    each bat's state for the cascade log — no more "bat at max" lies.
    """
    if not bat_alloc:
        return False, ""
    parts: list[str] = []
    all_at_max = True
    for bid, alloc in bat_alloc.items():
        soc = bat_socs.get(bid, 100.0)
        max_w = cfg.bat_default_max_charge_w
        soc_at_stop = soc >= cfg.bat_charge_stop_soc_pct
        alloc_at_max = alloc >= max_w - cfg.bat_at_max_headroom_w
        at_max = soc_at_stop or alloc_at_max
        if not at_max:
            all_at_max = False
        state_tag = "stop-SoC" if soc_at_stop else ("alloc-max" if alloc_at_max else "headroom")
        parts.append(f"{bid}: SoC={soc:.0f}% alloc={alloc}W ({state_tag})")
    return all_at_max, ", ".join(parts)


def _cascade_consumers(
    inp: BudgetInput,
    bat_alloc: dict[str, int],
    state: BudgetState,
    cfg: BudgetConfig,
) -> list[Command]:
    """PLAT-1715 + PLAT-1738: Unified consumer cascade.

    User rules (2026-04-18 + 2026-04-19):
      - grid export > 100 W AND bat-at-max (PLAT-1738 check) → turn ON
        lowest-priority-number inactive consumer.
      - grid import > 100 W → turn OFF highest priority_shed active consumer.
    One switch per cycle + ``cfg.cascade_cooldown_s`` per-consumer cooldown
    to prevent flapping. Bat-at-max guard prevents cascading onto PV-surplus
    that bat still has headroom to absorb (moln-dip regression, 2026-04-19).

    Pure function: reads inp, writes only to state.consumer_last_switch_ts.
    """
    if not inp.consumers:
        return []

    now_ts = time.monotonic()
    cooldown_s = cfg.cascade_cooldown_s
    grid_w = inp.grid_power_w
    cmds: list[Command] = []

    # Sustained export signal: bat couldn't absorb the surplus in the
    # previous cycle(s) → consumers needed. consecutive_export_cycles is
    # updated in allocate() before the cascade runs.
    sustained_export = state.consecutive_export_cycles >= cfg.cascade_sustained_cycles

    all_at_max, bat_state_str = _bat_at_max(bat_alloc, inp.bat_socs, cfg)

    if grid_w < -_GRID_TOLERANCE_W and sustained_export and all_at_max:
        inactive = sorted(
            (c for c in inp.consumers if not c.active),
            key=lambda c: c.priority,
        )
        for c in inactive:
            last = state.consumer_last_switch_ts.get(c.consumer_id, 0.0)
            if now_ts - last < cooldown_s:
                continue
            cmds.append(
                Command(
                    command_type=CommandType.TURN_ON_CONSUMER,
                    target_id=c.consumer_id,
                    value=None,
                    rule_id=_RULE_ID,
                    reason=(
                        f"Cascade: grid export {-grid_w:.0f}W > "
                        f"{_GRID_TOLERANCE_W:.0f}W sustained, bat SoC: "
                        f"{bat_state_str}"
                    ),
                )
            )
            state.consumer_last_switch_ts[c.consumer_id] = now_ts
            break
    elif grid_w > _GRID_TOLERANCE_W:
        active = sorted(
            (c for c in inp.consumers if c.active),
            key=lambda c: -c.priority_shed,
        )
        for c in active:
            last = state.consumer_last_switch_ts.get(c.consumer_id, 0.0)
            if now_ts - last < cooldown_s:
                continue
            cmds.append(
                Command(
                    command_type=CommandType.TURN_OFF_CONSUMER,
                    target_id=c.consumer_id,
                    value=None,
                    rule_id=_RULE_ID,
                    reason=(f"Cascade: grid import {grid_w:.0f}W > " f"{_GRID_TOLERANCE_W:.0f}W"),
                )
            )
            state.consumer_last_switch_ts[c.consumer_id] = now_ts
            break
    return cmds


def _allocate_bat(
    inp: BudgetInput,
    remaining_w: float,
    cfg: BudgetConfig,
) -> tuple[dict[str, int], float]:
    """Allocate remaining surplus to batteries. Returns (allocations, remaining).

    SoC convergence: lower SoC gets more. Max 1pp spread.
    Uses charge_pv mode — GoodWe absorbs PV naturally.
    """
    if remaining_w <= 0 or not inp.bat_socs:
        return {bid: 0 for bid in inp.bat_socs}, remaining_w

    # PLAT-1695: Stop charging at bat_charge_stop_soc_pct (matches S8 entry).
    # Previously used bat_soc_full_pct (100%) which created a 5pp dead zone where
    # state machine triggered S8 PV_SURPLUS at 95% but budget kept charging up to
    # 100% → grid export grew instead of shrinking at high SoC.
    # NOTE: Allocation only distributed across ACTIVE bats (below threshold);
    # bats at/above threshold get 0 so they standby in downstream command emit.
    active = {bid: soc for bid, soc in inp.bat_socs.items() if soc < cfg.bat_charge_stop_soc_pct}
    if not active:
        return {bid: 0 for bid in inp.bat_socs}, remaining_w

    total_alloc = int(remaining_w)
    # Initialize all bats to 0; then fill active ones below.
    alloc: dict[str, int] = {bid: 0 for bid in inp.bat_socs}
    active_bids = list(active.keys())

    if len(active_bids) == 1:
        alloc[active_bids[0]] = total_alloc
        return alloc, 0.0

    # Multiple active batteries — proportional-by-capacity with SoC-skew
    # when unbalanced (lower SoC gets more to catch up).
    default_cap = cfg.bat_default_cap_kwh
    socs = [active[bid] for bid in active_bids]
    spread = max(socs) - min(socs)

    # PLAT-1715: aggressive rebalance — when spread is large, route ALL surplus
    # to the bottom half so SoC catches up fast. User rule: "1 bat ska ladda max".
    if spread > cfg.bat_aggressive_spread_pct:
        sorted_bids = sorted(active_bids, key=lambda b: active[b])
        mid = max(1, len(sorted_bids) // 2)
        lower_bids = sorted_bids[:mid]
        # Split total_alloc equally across the bottom half; top half = 0.
        share_per = 1.0 / len(lower_bids)
        for bid in lower_bids:
            alloc[bid] = int(total_alloc * share_per)
        return alloc, 0.0

    # Kund-agnostisk: scale to N batteries, not hardcoded to 2.
    if spread > cfg.bat_spread_max_pct:
        # Unbalanced: split bat_lower_ratio across the bottom half,
        # bat_higher_ratio across the top half (by SoC).
        sorted_bids = sorted(active_bids, key=lambda b: active[b])
        mid = max(1, len(sorted_bids) // 2)
        lower_bids = sorted_bids[:mid]
        higher_bids = sorted_bids[mid:]
        lower_share_per = cfg.bat_lower_ratio / max(1, len(lower_bids))
        higher_share_per = cfg.bat_higher_ratio / max(1, len(higher_bids)) if higher_bids else 0.0
        for bid in lower_bids:
            alloc[bid] = int(total_alloc * lower_share_per)
        for bid in higher_bids:
            alloc[bid] = int(total_alloc * higher_share_per)
        return alloc, 0.0

    # Balanced — proportional by capacity (only among active bats)
    active_caps = {bid: inp.bat_caps.get(bid, default_cap) for bid in active_bids}
    total_cap = sum(active_caps.values()) or 1.0
    for bid in active_bids:
        alloc[bid] = int(total_alloc * active_caps[bid] / total_cap)
    return alloc, 0.0
