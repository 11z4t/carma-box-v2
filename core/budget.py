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
from dataclasses import dataclass, field
from datetime import datetime

from core.models import Command, CommandType, ConsumerState, EMSMode

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
    # bat_spread_max_pct: above this SoC diff (pp), use 80/20 unbalanced split.
    # bat_aggressive_spread_pct (PLAT-1715 user rule): above this diff, lower bat
    # charges 100% and higher stays in standby — fast SoC convergence.
    bat_spread_max_pct: float = 1.0
    bat_aggressive_spread_pct: float = 5.0
    bat_lower_ratio: float = 0.8
    bat_higher_ratio: float = 0.2
    # EV ramp tröghet: ramp UP kräver N konsekutiva export-cykler
    ev_ramp_up_hold_cycles: int = 2   # trög upp (moln kan återkomma)
    ev_ramp_down_hold_cycles: int = 1  # snabb ner (skydda mot import)
    # Bat discharge support for EV
    bat_discharge_support: bool = True
    # Evening cutoff — bat prio after this hour
    evening_cutoff_h: int = 17
    # Bat discharge minimum SoC — absolute floor (GoodWe AC output cut)
    bat_discharge_min_soc_pct: float = 15.0
    # Default bat capacity fallback if caller omits bat_caps
    bat_default_cap_kwh: float = 10.0


# ---------------------------------------------------------------------------
# Input / Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetInput:
    """All facts needed for one budget cycle."""

    now: datetime
    grid_power_w: float          # positive = import, negative = export
    pv_power_w: float            # total PV production
    house_load_w: float          # house consumption (excl bat/EV)
    ev_connected: bool
    ev_charging: bool
    ev_current_amps: int
    ev_soc_pct: float
    ev_target_soc_pct: float
    bat_socs: dict[str, float]   # battery_id → SoC %
    bat_caps: dict[str, float]   # battery_id → capacity kWh
    bat_powers: dict[str, float]  # battery_id → current power W
    bat_modes: dict[str, str]    # battery_id → current EMS mode
    pv_remaining_kwh: float = 0.0  # Solcast remaining today
    house_remaining_kwh: float = 0.0  # estimated house consumption remaining
    # PLAT-1715: consumer list for unified priority cascade.
    # Empty tuple (default) → no cascade, legacy behaviour.
    consumers: tuple[ConsumerState, ...] = ()


@dataclass
class BudgetState:
    """Mutable state persisted between cycles (caller owns)."""

    consecutive_export_cycles: int = 0
    consecutive_import_cycles: int = 0
    ev_current_amps: int = 0
    # PLAT-1715: cooldown per consumer (monotonic seconds) to prevent flapping.
    # Key = consumer_id, value = monotonic timestamp of last switch.
    consumer_last_switch_ts: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class BudgetResult:
    """Output of one budget cycle."""

    commands: list[Command]
    ev_target_amps: int
    bat_allocations: dict[str, int]  # battery_id → charge limit W
    bat_discharge_w: int = 0        # total bat discharge for support
    reason: str = ""


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
    ev_wants_charge = (
        inp.ev_connected
        and inp.ev_soc_pct < inp.ev_target_soc_pct
    )

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
        0.0, cfg.bat_soc_full_pct - (pv_for_bat / bat_total_cap * _PCT_FACTOR),
    )
    bat_avg_soc = (
        sum(inp.bat_socs.values()) / len(inp.bat_socs)
        if inp.bat_socs else 0.0
    )
    bat_can_support_ev = bat_avg_soc > bat_min_soc_dynamic

    # ----- PRIORITY ALLOCATION -----

    if not daytime:
        # Night — defers to night controller
        reasons.append("NIGHT: defers to night controller")

    elif (
        _EVENING_DISCHARGE_START_H <= hour < _EVENING_DISCHARGE_END_H
        and bat_avg_soc > cfg.bat_discharge_min_soc_pct
    ):
        # Evening discharge 17-20: bat covers house load, grid target 0W
        # Bat FULL or partial — discharge to cover house load either way
        bat_discharge, bat_alloc = _allocate_evening_discharge(
            inp, cfg, state,
        )
        ev_target = 0
        reasons.append(
            f"EVENING_DISCHARGE: bat_discharge {bat_discharge}W"
            f" house={inp.house_load_w:.0f}W"
        )

    elif hour >= cfg.evening_cutoff_h:
        # Evening 20-22: bat standby, NO EV (preserve for morning)
        bat_alloc, remaining = _allocate_bat(inp, remaining, cfg)
        ev_target = 0
        reasons.append(f"EVENING: bat {sum(bat_alloc.values())}W, EV off")

    elif fm and ev_wants_charge:
        # FM 06-12: EV first + bat-discharge-support if needed
        ev_target, remaining = _allocate_ev_with_ramp(
            inp, remaining, cfg, state,
        )
        # Remaining PV → bat
        bat_charge_alloc, remaining = _allocate_bat(inp, remaining, cfg)
        # Bat discharge support if EV needs more than PV provides
        if ev_target > 0 and bat_can_support_ev and cfg.bat_discharge_support:
            ev_power = _ev_power_at_amps(ev_target)
            gap = ev_power - surplus
            if gap > 0:
                bat_discharge = int(gap)
                # Discharge overrides charge — set per-bat discharge limits
                shares = _bat_discharge_shares(inp, cfg)
                bat_alloc = {
                    bid: int(bat_discharge * share)
                    for bid, share in shares.items()
                }
            else:
                bat_alloc = bat_charge_alloc
        else:
            bat_alloc = bat_charge_alloc
        reasons.append(
            f"FM: EV {ev_target}A bat_charge {sum(bat_charge_alloc.values())}W"
            f" bat_discharge {bat_discharge}W"
        )

    else:
        # EM 12-17: bat first, EV with remainder or bat-support
        bat_charge_alloc, remaining = _allocate_bat(inp, remaining, cfg)
        bat_alloc = bat_charge_alloc

        if ev_wants_charge:
            if _all_bat_full(inp, cfg):
                # Bat full → EV gets surplus + bat-discharge-support
                ev_target, remaining = _allocate_ev_with_ramp(
                    inp, remaining, cfg, state,
                )
                if ev_target > 0 and cfg.bat_discharge_support:
                    ev_power = _ev_power_at_amps(ev_target)
                    gap = ev_power - surplus
                    if gap > 0:
                        bat_discharge = int(gap)
                        shares = _bat_discharge_shares(inp, cfg)
                        bat_alloc = {
                            bid: int(bat_discharge * share)
                            for bid, share in shares.items()
                        }
                reasons.append(f"EM: bat full, EV {ev_target}A discharge {bat_discharge}W")
            elif bat_can_support_ev:
                # Bat not full but has margin → EV from PV surplus
                ev_target, remaining = _allocate_ev_with_ramp(
                    inp, remaining, cfg, state,
                )
                reasons.append(
                    f"EM: bat {bat_avg_soc:.0f}%>min"
                    f"{bat_min_soc_dynamic:.0f}% EV {ev_target}A"
                )
            else:
                ev_target = 0
                reasons.append(f"EM: bat {bat_avg_soc:.0f}%≤min{bat_min_soc_dynamic:.0f}% EV off")
        else:
            ev_target = 0
            reasons.append(f"EM: bat {sum(bat_charge_alloc.values())}W")

    # ----- EMIT COMMANDS -----

    # EV commands
    if ev_target > 0 and not inp.ev_charging:
        cmds.append(Command(
            command_type=CommandType.START_EV_CHARGING,
            target_id="ev", value=None,
            rule_id=_RULE_ID, reason="Budget: EV start",
        ))
    if ev_target == 0 and inp.ev_charging:
        cmds.append(Command(
            command_type=CommandType.STOP_EV_CHARGING,
            target_id="ev", value=None,
            rule_id=_RULE_ID, reason="Budget: EV stop",
        ))
    if ev_target > 0 and ev_target != inp.ev_current_amps:
        cmds.append(Command(
            command_type=CommandType.SET_EV_CURRENT,
            target_id="ev", value=ev_target,
            rule_id=_RULE_ID,
            reason=f"Budget: EV {inp.ev_current_amps}→{ev_target}A",
        ))

    # Bat commands — charge or discharge
    if bat_discharge > 0:
        # Discharge mode — either EV support or evening discharge
        # bat_alloc already has per-battery discharge limits
        for bid, discharge_w in bat_alloc.items():
            if discharge_w > 0:
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bid,
                    value=EMSMode.DISCHARGE_PV.value,
                    rule_id=_RULE_ID,
                    reason=f"Budget: discharge_pv {discharge_w}W",
                ))
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_POWER_LIMIT,
                    target_id=bid, value=discharge_w,
                    rule_id=_RULE_ID,
                    reason=f"Budget: limit {discharge_w}W",
                ))
    else:
        # PLAT-1714: Charge via charge_battery (mode 11) which RESPECTS
        # ems_power_limit. Previous charge_pv is UNCONTROLLABLE in peak_shaving
        # firmware mode → caused 800W grid import during PV absorption.
        #
        # PLAT-1715: Only emit SET_EMS_MODE when the current mode differs from
        # the target. Each SET_EMS_MODE request runs the 5-step mode-change
        # protocol whose Step 1 PREPARE clears ems_power_limit to 0 —
        # if we re-emit the same mode every 30s cycle the limit resets to 0
        # and the inverter stops charging (PLAT-1715 live root cause: 4.9 kW
        # export because limit was repeatedly nulled).
        for bid, limit_w in bat_alloc.items():
            current_mode = inp.bat_modes.get(bid, "")
            target_mode = (
                EMSMode.CHARGE_BATTERY.value
                if limit_w > 0
                else EMSMode.BATTERY_STANDBY.value
            )
            if current_mode != target_mode:
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bid,
                    value=target_mode,
                    rule_id=_RULE_ID,
                    reason=(
                        f"Budget: {target_mode} {limit_w}W"
                        if limit_w > 0
                        else "Budget: standby"
                    ),
                ))
            # Always emit the power limit — idempotent writes keep the
            # inverter converged on the Budget's intended value. Truthy-trap
            # defense (B9) already guarantees 0 is actually written.
            cmds.append(Command(
                command_type=CommandType.SET_EMS_POWER_LIMIT,
                target_id=bid, value=limit_w,
                rule_id=_RULE_ID,
                reason=(
                    f"Budget: limit {limit_w}W"
                    if limit_w > 0
                    else "Budget: standby limit=0"
                ),
            ))

    # PLAT-1715: unified consumer cascade runs after bat + EV allocation.
    cmds.extend(_cascade_consumers(inp, bat_alloc, state))

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


def _cascade_consumers(
    inp: BudgetInput,
    bat_alloc: dict[str, int],
    state: BudgetState,
) -> list[Command]:
    """PLAT-1715: Unified consumer cascade.

    User rules (2026-04-18):
      - grid export > 100 W AND bat at max → turn ON lowest-priority-number
        inactive consumer.
      - grid import > 100 W → turn OFF highest priority_shed active consumer.
    One switch per cycle + 60 s per-consumer cooldown to prevent flapping.

    Pure function: reads inp, writes only to state.consumer_last_switch_ts.
    """
    if not inp.consumers:
        return []
    import time as _time  # noqa: PLC0415

    now_ts = _time.monotonic()
    cooldown_s = 60.0
    grid_w = inp.grid_power_w
    cmds: list[Command] = []

    # Bat at max if every active bat is charging within 50 W of its limit.
    bat_at_max = False
    if bat_alloc:
        bat_at_max = all(
            abs(inp.bat_powers.get(bid, 0.0)) >= max(0.0, float(limit_w) - 50.0)
            for bid, limit_w in bat_alloc.items() if limit_w > 0
        )

    if grid_w < -_GRID_TOLERANCE_W and bat_at_max:
        inactive = sorted(
            (c for c in inp.consumers if not c.active),
            key=lambda c: c.priority,
        )
        for c in inactive:
            last = state.consumer_last_switch_ts.get(c.consumer_id, 0.0)
            if now_ts - last < cooldown_s:
                continue
            cmds.append(Command(
                command_type=CommandType.TURN_ON_CONSUMER,
                target_id=c.consumer_id,
                value=None,
                rule_id=_RULE_ID,
                reason=(
                    f"Cascade: grid export {-grid_w:.0f}W > "
                    f"{_GRID_TOLERANCE_W:.0f}W, bat at max"
                ),
            ))
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
            cmds.append(Command(
                command_type=CommandType.TURN_OFF_CONSUMER,
                target_id=c.consumer_id,
                value=None,
                rule_id=_RULE_ID,
                reason=(
                    f"Cascade: grid import {grid_w:.0f}W > "
                    f"{_GRID_TOLERANCE_W:.0f}W"
                ),
            ))
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
    active = {bid: soc for bid, soc in inp.bat_socs.items()
              if soc < cfg.bat_charge_stop_soc_pct}
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
        higher_share_per = cfg.bat_higher_ratio / max(1, len(higher_bids)) \
            if higher_bids else 0.0
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
