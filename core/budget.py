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
from dataclasses import dataclass
from datetime import datetime

from core.models import Command, CommandType, EMSMode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DAYTIME_START_H: int = 6
_DAYTIME_END_H: int = 22
_FM_END_H: int = 12

_GRID_TARGET_W: float = 0.0
_GRID_TOLERANCE_W: float = 100.0

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
    bat_soc_full_pct: float = 100.0
    bat_spread_max_pct: float = 1.0
    bat_lower_ratio: float = 0.8
    bat_higher_ratio: float = 0.2
    # EV ramp tröghet: ramp UP kräver N konsekutiva export-cykler
    ev_ramp_up_hold_cycles: int = 2   # trög upp (moln kan återkomma)
    ev_ramp_down_hold_cycles: int = 1  # snabb ner (skydda mot import)
    # Bat discharge support for EV
    bat_discharge_support: bool = True
    # Evening cutoff — bat prio after this hour
    evening_cutoff_h: int = 17


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


@dataclass
class BudgetState:
    """Mutable state persisted between cycles (caller owns)."""

    consecutive_export_cycles: int = 0
    consecutive_import_cycles: int = 0
    ev_current_amps: int = 0


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
        0.0, cfg.bat_soc_full_pct - (pv_for_bat / bat_total_cap * 100.0),
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

    elif hour >= cfg.evening_cutoff_h:
        # Evening 17-22: bat prio, NO EV
        bat_alloc, remaining = _allocate_bat(inp, remaining, cfg)
        ev_target = 0
        reasons.append(f"EVENING: bat {sum(bat_alloc.values())}W, EV off")

    elif fm and ev_wants_charge:
        # FM 06-12: EV first + bat-discharge-support if needed
        ev_target, remaining = _allocate_ev_with_ramp(
            inp, remaining, cfg, state,
        )
        # Remaining PV → bat
        bat_alloc, remaining = _allocate_bat(inp, remaining, cfg)
        # Bat discharge support if EV needs more than PV provides
        if ev_target > 0 and bat_can_support_ev and cfg.bat_discharge_support:
            ev_power = _ev_power_at_amps(ev_target)
            gap = ev_power - surplus
            if gap > 0:
                bat_discharge = int(gap)
        reasons.append(
            f"FM: EV {ev_target}A bat_charge {sum(bat_alloc.values())}W"
            f" bat_discharge {bat_discharge}W"
        )

    else:
        # EM 12-17: bat first, EV with remainder or bat-support
        bat_alloc, remaining = _allocate_bat(inp, remaining, cfg)

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
            reasons.append(f"EM: bat {sum(bat_alloc.values())}W")

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
        # Bat-discharge-support for EV — proportional per battery
        shares = _bat_discharge_shares(inp, cfg)
        for bid, share in shares.items():
            discharge_w = int(bat_discharge * share)
            if discharge_w > 0:
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bid,
                    value=EMSMode.DISCHARGE_PV.value,
                    rule_id=_RULE_ID,
                    reason=f"Budget: discharge_pv {discharge_w}W (EV support)",
                ))
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_POWER_LIMIT,
                    target_id=bid, value=discharge_w,
                    rule_id=_RULE_ID,
                    reason=f"Budget: limit {discharge_w}W",
                ))
    else:
        # Charge or standby
        for bid, limit_w in bat_alloc.items():
            if limit_w > 0:
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bid,
                    value=EMSMode.CHARGE_PV.value,
                    rule_id=_RULE_ID,
                    reason=f"Budget: charge_pv {limit_w}W",
                ))
            else:
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bid,
                    value=EMSMode.BATTERY_STANDBY.value,
                    rule_id=_RULE_ID,
                    reason="Budget: standby",
                ))

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


def _bat_discharge_shares(
    inp: BudgetInput,
    cfg: BudgetConfig,
) -> dict[str, float]:
    """Proportional discharge shares so both bats reach min_soc simultaneously."""
    _PCT = 100.0
    _MIN_SOC = 15.0  # absolute floor
    avail = {}
    for bid, soc in inp.bat_socs.items():
        cap = inp.bat_caps.get(bid, 10.0)
        avail[bid] = max(0.0, (soc - _MIN_SOC) / _PCT * cap)
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

    # Skip bats already at 100% — they don't need surplus
    active = {bid: soc for bid, soc in inp.bat_socs.items()
              if soc < cfg.bat_soc_full_pct}
    if not active:
        return {bid: 0 for bid in inp.bat_socs}, remaining_w

    bids = list(inp.bat_socs.keys())
    total_alloc = int(remaining_w)

    if len(bids) == 1:
        return {bids[0]: total_alloc}, 0.0

    # Two batteries — check spread
    soc_a, soc_b = inp.bat_socs[bids[0]], inp.bat_socs[bids[1]]
    cap_a, cap_b = inp.bat_caps.get(bids[0], 15.0), inp.bat_caps.get(bids[1], 5.0)
    total_cap = cap_a + cap_b
    spread = abs(soc_a - soc_b)

    if spread > cfg.bat_spread_max_pct:
        # Unbalanced — lower gets 80%, higher gets 20%
        lower = bids[0] if soc_a < soc_b else bids[1]
        higher = bids[1] if lower == bids[0] else bids[0]
        return {
            lower: int(total_alloc * cfg.bat_lower_ratio),
            higher: int(total_alloc * cfg.bat_higher_ratio),
        }, 0.0

    # Balanced — proportional by capacity
    return {
        bids[0]: int(total_alloc * cap_a / total_cap),
        bids[1]: int(total_alloc * cap_b / total_cap),
    }, 0.0
