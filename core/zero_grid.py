"""Zero-Grid Controller (PLAT-1718).

Closed-loop bat-power regulator whose sole target is grid_power = 0 W.
Runs every cycle; uses MEASURED grid + bat powers (never a model) so it
is robust against stale PV sensors, cloud transients, and surprise
house loads.

Principles:
  1. Pure function. Input is the current snapshot, output is a plan for
     the next cycle. No side effects, no I/O.
  2. Every cycle = one correction step. A deviation from 0 W at cycle N
     is erased at cycle N+1 unless physical limits prevent it.
  3. Physical limits respected:
       - max_charge_w / max_discharge_w per battery from site.yaml
       - soc must stay within [soc_min_pct, soc_max_pct]
  4. Multi-battery aware: distributes the target across active bats with
     an SoC-balance weight so the lower-SoC bat catches up while the
     higher bat coasts.
  5. Deterministic — same input always produces the same plan.

Out of scope (handled elsewhere):
  - EV ramp-up/ramp-down (SurplusDispatch / BudgetAllocator.allocate).
  - Consumer start/stop cascade (see budget._cascade_consumers).
  - Scenario selection / state machine.

The caller (BudgetAllocator) is responsible for emitting the commands
returned here through CommandExecutor.
"""

from __future__ import annotations

from dataclasses import dataclass


_TARGET_GRID_W: float = 0.0
_DEAD_BAND_W: float = 50.0  # below this deviation, hold (avoid chatter)


@dataclass(frozen=True)
class BatLimits:
    """Physical limits for one battery — all from site.yaml."""

    max_charge_w: int
    max_discharge_w: int
    soc_min_pct: float
    soc_max_pct: float  # charge-stop threshold (matches S8 PV_SURPLUS entry)


@dataclass(frozen=True)
class BatSnapshot:
    """Current measured state of one battery."""

    battery_id: str
    power_w: float  # positive = discharge, negative = charge
    soc_pct: float


@dataclass(frozen=True)
class ZeroGridPlan:
    """Output: per-battery mode + limit."""

    modes: dict[str, str]
    limits_w: dict[str, int]
    total_target_net_w: int  # + discharge, - charge
    reason: str


def _target_net_w(
    current_bat_net_w: float,
    grid_power_w: float,
    deadband_w: float = _DEAD_BAND_W,
) -> float:
    """Compute target bat net power to drive grid to zero.

    Convention (matches GoodWe + ``bat_powers`` through BudgetInput):
      - bat power_w > 0  → battery is discharging
      - bat power_w < 0  → battery is charging
      - grid_power_w > 0 → grid import (house > PV+bat)
      - grid_power_w < 0 → grid export (PV+bat > house)

    To drive grid to ``_TARGET_GRID_W`` (0 W) we shift the bat side by
    ``grid_power_w``:

      - grid_power_w > 0 (import)  → bat net should rise by grid_power_w
        (charge less or discharge more).
      - grid_power_w < 0 (export)  → bat net should fall by |grid_power_w|
        (charge more or discharge less).

    Checked: new bat net + PV + house == grid, so
    grid_new = grid_now - (bat_new - bat_now) = grid_now - grid_now = 0.
    """
    deviation = grid_power_w - _TARGET_GRID_W
    if abs(deviation) < deadband_w:
        return current_bat_net_w  # stay inside the deadband
    return current_bat_net_w + deviation


def _clamp_for_soc(
    target_net_w: float,
    soc_pct: float,
    limits: BatLimits,
) -> float:
    """Respect SoC-dependent physical caps.

    - Charging (target < 0) is forbidden when SoC is at/above the
      charge-stop threshold — returns 0 (standby).
    - Discharging (target > 0) is forbidden when SoC is at/below the
      discharge floor — returns 0 (standby).
    - Otherwise clamp to the max_charge / max_discharge envelope.
    """
    if target_net_w < 0:
        if soc_pct >= limits.soc_max_pct:
            return 0.0
        return max(target_net_w, -float(limits.max_charge_w))
    if target_net_w > 0:
        if soc_pct <= limits.soc_min_pct:
            return 0.0
        return min(target_net_w, float(limits.max_discharge_w))
    return 0.0


def _plan_for_net(target_net_w: float) -> tuple[str, int]:
    """Map a target bat net power to (mode, limit_w).

    Limits are ALWAYS positive magnitudes — the mode tells the inverter
    which direction to go. Values below the deadband round to standby
    so the inverter is not thrashed by ±10 W noise.
    """
    if target_net_w <= -_DEAD_BAND_W:
        return "charge_battery", int(-target_net_w)
    if target_net_w >= _DEAD_BAND_W:
        return "discharge_pv", int(target_net_w)
    return "battery_standby", 0


def _distribute(
    total_target_net_w: float,
    bats: list[BatSnapshot],
    limits_by_id: dict[str, BatLimits],
    spread_aggressive_pct: float = 5.0,
) -> dict[str, float]:
    """Split a total net target (W) across N batteries.

    Rules (matches PLAT-1715 user rule):
      - Single bat → it owns the whole target.
      - Multiple bats and SoC-spread above ``spread_aggressive_pct`` →
        lower-half-by-SoC bat(s) take the full charge target; upper half
        goes to standby. For discharge the opposite: upper-SoC
        discharges, lower-SoC holds (protects low bat).
      - Otherwise proportional by capacity across all eligible bats.
    """
    if not bats:
        return {}
    if len(bats) == 1:
        return {bats[0].battery_id: total_target_net_w}

    sorted_by_soc = sorted(bats, key=lambda b: b.soc_pct)
    spread = sorted_by_soc[-1].soc_pct - sorted_by_soc[0].soc_pct
    alloc: dict[str, float] = {b.battery_id: 0.0 for b in bats}

    if spread > spread_aggressive_pct:
        mid = max(1, len(sorted_by_soc) // 2)
        # Charging: lower half absorbs, upper half standby.
        # Discharging: upper half discharges, lower half holds.
        if total_target_net_w < 0:
            movers = sorted_by_soc[:mid]
        else:
            movers = sorted_by_soc[-mid:]
        share = total_target_net_w / len(movers)
        for b in movers:
            alloc[b.battery_id] = share
        return alloc

    # Balanced — proportional by capacity against each bat's relevant cap.
    # For charging: weight = max_charge_w; for discharging: max_discharge_w.
    if total_target_net_w < 0:
        weights = {
            b.battery_id: float(limits_by_id[b.battery_id].max_charge_w)
            for b in bats
        }
    else:
        weights = {
            b.battery_id: float(limits_by_id[b.battery_id].max_discharge_w)
            for b in bats
        }
    total_weight = sum(weights.values()) or 1.0
    for b in bats:
        alloc[b.battery_id] = total_target_net_w * weights[b.battery_id] / total_weight
    return alloc


def plan_zero_grid(
    grid_power_w: float,
    bats: list[BatSnapshot],
    limits_by_id: dict[str, BatLimits],
    deadband_w: float = _DEAD_BAND_W,
    spread_aggressive_pct: float = 5.0,
) -> ZeroGridPlan:
    """Compute the per-battery plan that drives ``grid_power_w`` to 0.

    Steps:
      1. Work out the net bat power required so that the system balance
         (PV + bat − house) = 0.
      2. Distribute that target across the configured batteries.
      3. Clamp per-battery values against the physical + SoC envelopes
         (a side effect: if no bat can satisfy the request, the grid
         simply won't reach 0 this cycle — deviation will shrink next
         cycle via the deadband-controller loop).

    Returns a full plan: mode + limit for every bat in ``bats``, and a
    summary of the total net target used for logging / diagnostics.
    """
    current_net = sum(b.power_w for b in bats)
    target_net = _target_net_w(current_net, grid_power_w, deadband_w)
    per_bat = _distribute(target_net, bats, limits_by_id, spread_aggressive_pct)

    modes: dict[str, str] = {}
    limits: dict[str, int] = {}
    final_net_total = 0.0
    for b in bats:
        clamped = _clamp_for_soc(
            per_bat.get(b.battery_id, 0.0),
            b.soc_pct,
            limits_by_id[b.battery_id],
        )
        mode, limit_w = _plan_for_net(clamped)
        modes[b.battery_id] = mode
        limits[b.battery_id] = limit_w
        final_net_total += clamped

    reason = (
        f"zero_grid: grid={grid_power_w:.0f}W "
        f"bat_now={current_net:.0f}W target={target_net:.0f}W "
        f"applied={final_net_total:.0f}W"
    )
    return ZeroGridPlan(
        modes=modes,
        limits_w=limits,
        total_target_net_w=int(final_net_total),
        reason=reason,
    )
