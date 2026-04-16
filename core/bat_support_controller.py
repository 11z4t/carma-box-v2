"""Bat Support Controller — proactive bat discharge to keep grid under tak.

PLAT-1673 Case 2 + Case 3.

Mission: when the night-window has high concurrent load (EV + dishwasher +
baseload), the bat discharges proactively so that grid_kw_weighted never
exceeds the Ellevio target. This is a *preventive* action — it must fire
BEFORE G3 grid-breach guard kicks in.

Proportional allocation (Case 3 fix): both batteries are drained such that
each reaches its min_soc at the same time. Formula:

    available_i = max(0, (soc_i - min_soc_i) / 100 * cap_i)
    share_i     = available_i / sum(available)

Pure function — no I/O. Caller (engine) executes returned commands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.models import Command, CommandType, EMSMode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — internal only
# ---------------------------------------------------------------------------

_RULE_ID: str = "BAT_SUPPORT"
_PCT_FACTOR: float = 100.0
_W_TO_KW: float = 1000.0


# ---------------------------------------------------------------------------
# Config + I/O dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatSupportConfig:
    """Configuration for bat support controller."""

    enabled: bool = True
    tak_weighted_kw: float = 3.0
    night_weight: float = 0.5
    safety_margin: float = 0.95
    min_soc_normal_pct: float = 15.0
    min_soc_cold_pct: float = 20.0
    cold_temp_c: float = 4.0


@dataclass(frozen=True)
class BatInfo:
    """Battery snapshot subset needed for support decision."""

    battery_id: str
    soc_pct: float
    cap_kwh: float
    cell_temp_c: float
    max_discharge_w: float
    current_mode: EMSMode


@dataclass(frozen=True)
class BatSupportInput:
    """All facts needed to evaluate one cycle."""

    batteries: list[BatInfo]
    total_load_kw: float       # EV + dishwasher + baseload, summed
    grid_weighted_kw: float    # current weighted hourly average


@dataclass(frozen=True)
class BatSupportDecision:
    """Output: per-battery discharge limit + commands."""

    commands: list[Command]
    per_battery_w: dict[str, int]
    total_discharge_w: int
    reason: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _effective_min_soc(bat: BatInfo, cfg: BatSupportConfig) -> float:
    """Return effective min_soc — higher when cell is cold."""
    if bat.cell_temp_c < cfg.cold_temp_c:
        return cfg.min_soc_cold_pct
    return cfg.min_soc_normal_pct


def _available_kwh(bat: BatInfo, cfg: BatSupportConfig) -> float:
    """Energy available above min_soc (kWh)."""
    floor = _effective_min_soc(bat, cfg)
    headroom_pct = max(0.0, bat.soc_pct - floor)
    return headroom_pct / _PCT_FACTOR * bat.cap_kwh


def _proportional_shares(
    batteries: list[BatInfo],
    cfg: BatSupportConfig,
) -> dict[str, float]:
    """Return per-battery share of total discharge such that all batteries
    reach min_soc simultaneously.

    Returns 0-share for batteries already at/below floor.
    """
    avail = {b.battery_id: _available_kwh(b, cfg) for b in batteries}
    total = sum(avail.values())
    if total <= 0:
        return {b.battery_id: 0.0 for b in batteries}
    return {bid: a / total for bid, a in avail.items()}


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def evaluate(inp: BatSupportInput, cfg: BatSupportConfig) -> BatSupportDecision:
    """Evaluate one cycle. Return per-battery discharge commands.

    Algorithm:
      1. Compute effective tak_raw = tak_weighted / night_weight (~6 kW night)
      2. Compute grid_planned_raw = total_load_kw  (assume bat=0)
      3. If grid_planned_raw > tak_raw * margin: bat_total_w = (excess) * 1000
         Else: bat_total_w = 0 (sparar bat)
      4. Distribute bat_total_w proportionally across batteries
         such that all reach min_soc simultaneously
      5. Clamp each battery to its max_discharge_w
      6. Emit SET_EMS_MODE(discharge_pv) + SET_EMS_POWER_LIMIT per bat
    """
    if not cfg.enabled:
        return BatSupportDecision(
            commands=[], per_battery_w={b.battery_id: 0 for b in inp.batteries},
            total_discharge_w=0, reason="DISABLED",
        )

    # 1. Effective grid cap (raw watts, considering night weight)
    if cfg.night_weight > 0:
        tak_raw_kw = cfg.tak_weighted_kw / cfg.night_weight
    else:
        tak_raw_kw = cfg.tak_weighted_kw
    cap_raw_kw = tak_raw_kw * cfg.safety_margin

    # 2. Required bat support to keep grid under cap
    grid_excess_kw = max(0.0, inp.total_load_kw - cap_raw_kw)
    bat_total_w = int(grid_excess_kw * _W_TO_KW)

    # 3. No support needed
    if bat_total_w <= 0:
        return BatSupportDecision(
            commands=[],
            per_battery_w={b.battery_id: 0 for b in inp.batteries},
            total_discharge_w=0,
            reason=(
                f"NO_SUPPORT_NEEDED total_load={inp.total_load_kw:.2f}kW "
                f"<= cap_raw={cap_raw_kw:.2f}kW"
            ),
        )

    # 4. Proportional allocation
    shares = _proportional_shares(inp.batteries, cfg)
    raw_alloc = {bid: int(bat_total_w * share) for bid, share in shares.items()}

    # 5. Clamp + emit commands
    cmds: list[Command] = []
    actual_alloc: dict[str, int] = {}
    actual_total = 0
    for bat in inp.batteries:
        alloc = raw_alloc.get(bat.battery_id, 0)
        clamped = min(alloc, int(bat.max_discharge_w))
        actual_alloc[bat.battery_id] = clamped
        actual_total += clamped

        if clamped <= 0:
            continue

        # Mode change to discharge_pv if needed
        if bat.current_mode != EMSMode.DISCHARGE_PV:
            cmds.append(Command(
                command_type=CommandType.SET_EMS_MODE,
                target_id=bat.battery_id,
                value=EMSMode.DISCHARGE_PV.value,
                rule_id=_RULE_ID,
                reason=(
                    f"Support: grid_excess={grid_excess_kw:.2f}kW "
                    f"share={shares[bat.battery_id]*100:.1f}%"
                ),
            ))
        cmds.append(Command(
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id=bat.battery_id,
            value=clamped,
            rule_id=_RULE_ID,
            reason=(
                f"Support: {clamped}W (share {shares[bat.battery_id]*100:.1f}%)"
            ),
        ))

    return BatSupportDecision(
        commands=cmds,
        per_battery_w=actual_alloc,
        total_discharge_w=actual_total,
        reason=(
            f"SUPPORT total_load={inp.total_load_kw:.2f}kW excess={grid_excess_kw:.2f}kW "
            f"discharge={actual_total}W "
            + " ".join(f"{bid}={w}W" for bid, w in actual_alloc.items())
        ),
    )
