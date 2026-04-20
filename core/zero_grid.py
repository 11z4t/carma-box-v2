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

# PLAT-1696 step 1 — underdamp the correction so one cycle closes ~70 %
# of the gap instead of 100 %. At cycle=15 s + GoodWe internal ramp ~
# 10 s, full-gain 1.0 overshot live (grid ±1.5 kW oscillation). P=0.7
# gives a geometric series: after 3 cycles (45 s) we're within 3 % of
# the set-point without ringing.
_CORRECTION_GAIN: float = 0.7


@dataclass(frozen=True)
class BatLimits:
    """Physical limits for one battery — all from site.yaml.

    ``soc_min_pct`` is the absolute floor; discharge is blocked at or
    below this value. ``soc_min_buffer_pct`` adds a safety margin on top
    so the in-flight discharge between cycles cannot dip below the
    floor — without it a 2.5 kW discharge + 15 s cycle can drain up to
    ~0.5 pp between the check and the next read (SoC = 15.1 % → 14.6 %
    before we react).
    """

    max_charge_w: int
    max_discharge_w: int
    soc_min_pct: float
    soc_max_pct: float  # charge-stop threshold (matches S8 PV_SURPLUS entry)
    soc_min_buffer_pct: float = 1.0


@dataclass(frozen=True)
class BatSnapshot:
    """Current measured state of one battery."""

    battery_id: str
    power_w: float  # positive = discharge, negative = charge
    soc_pct: float


@dataclass(frozen=True)
class ZeroGridPlan:
    """Output: per-battery mode + limit.

    ``emergency_recovery`` flags batteries whose SoC dropped BELOW the
    absolute floor (e.g. because a guard failed or a manual override
    slipped past). For those bats the caller MUST emit:
      - SET_EMS_MODE = charge_battery (mode 11)
      - SET_FAST_CHARGING = ON
      - SET_EMS_POWER_LIMIT = max_charge_w
    so the bat is force-charged from the grid back to floor ASAP.
    Once SoC ≥ soc_min_pct the flag clears and normal operation resumes.
    """

    modes: dict[str, str]
    limits_w: dict[str, int]
    total_target_net_w: int  # + discharge, - charge
    reason: str
    emergency_recovery: frozenset[str] = frozenset()  # battery_ids below floor


def _target_net_w(
    current_bat_net_w: float,
    grid_power_w: float,
    deadband_w: float = _DEAD_BAND_W,
    gain: float = _CORRECTION_GAIN,
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
    # Proportional-only underdamped: shift bat ``gain`` of the way toward
    # the full correction (default 70 % — see _CORRECTION_GAIN docstring).
    return current_bat_net_w + deviation * gain


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
        # Block discharge at ``soc_min_pct + soc_min_buffer_pct`` so the
        # in-flight drain between cycles cannot cross the absolute floor.
        # The buffer protects against the blackout at 10 % physical SoC.
        if soc_pct <= limits.soc_min_pct + limits.soc_min_buffer_pct:
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

    def _cap(b: BatSnapshot, *, charging: bool) -> float:
        lim = limits_by_id[b.battery_id]
        return float(lim.max_charge_w if charging else lim.max_discharge_w)

    if spread > spread_aggressive_pct:
        # PLAT-1718 grid-zero > PLAT-1715 SoC-balance. The primary half
        # (lower-SoC for charging, higher-SoC for discharging) runs at
        # its physical cap; any unmet demand spills to the secondary
        # half so the ±100 W grid invariant holds even when a single
        # battery cannot absorb/supply the full target.
        mid = max(1, len(sorted_by_soc) // 2)
        charging = total_target_net_w < 0
        if charging:
            primary = sorted_by_soc[:mid]
            secondary = sorted_by_soc[mid:]
        else:
            primary = sorted_by_soc[-mid:]
            secondary = sorted_by_soc[:-mid]

        target_mag = abs(total_target_net_w)
        sign = -1.0 if charging else 1.0

        primary_cap = sum(_cap(b, charging=charging) for b in primary)
        primary_mag = min(target_mag, primary_cap)
        if primary:
            # PLAT-1756: weight by individual cap, not equal share.
            # Equal split overflows a weaker bat when caps are asymmetric.
            for b in primary:
                bat_cap = _cap(b, charging=charging)
                alloc[b.battery_id] = sign * primary_mag * (bat_cap / (primary_cap or 1.0))

        overflow = target_mag - primary_mag
        if overflow > 0 and secondary:
            secondary_cap = sum(_cap(b, charging=charging) for b in secondary)
            secondary_mag = min(overflow, secondary_cap)
            # PLAT-1756: same per-bat cap weighting for secondary group.
            for b in secondary:
                bat_cap = _cap(b, charging=charging)
                alloc[b.battery_id] = sign * secondary_mag * (bat_cap / (secondary_cap or 1.0))
        return alloc

    # Balanced — proportional by capacity against each bat's relevant cap.
    charging_balanced = total_target_net_w < 0
    weights = {b.battery_id: _cap(b, charging=charging_balanced) for b in bats}
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
    gain: float = _CORRECTION_GAIN,
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
    # Emergency recovery detection — any bat BELOW the absolute floor is
    # force-charged independently of the grid target. This overrides the
    # zero-grid distribution for the affected batteries; the grid may
    # briefly import while the floor is restored.
    below_floor: set[str] = set()
    forced_modes: dict[str, str] = {}
    forced_limits: dict[str, int] = {}
    for b in bats:
        limits = limits_by_id[b.battery_id]
        if b.soc_pct < limits.soc_min_pct:
            below_floor.add(b.battery_id)
            forced_modes[b.battery_id] = "charge_battery"
            forced_limits[b.battery_id] = int(limits.max_charge_w)

    # Normal path — only runs for bats NOT in emergency recovery.
    current_net = sum(b.power_w for b in bats if b.battery_id not in below_floor)
    normal_bats = [b for b in bats if b.battery_id not in below_floor]
    target_net = _target_net_w(current_net, grid_power_w, deadband_w, gain)
    per_bat = _distribute(
        target_net,
        normal_bats,
        limits_by_id,
        spread_aggressive_pct,
    )

    modes: dict[str, str] = {}
    limits_out: dict[str, int] = {}
    final_net_total = 0.0
    for b in bats:
        if b.battery_id in below_floor:
            modes[b.battery_id] = forced_modes[b.battery_id]
            limits_out[b.battery_id] = forced_limits[b.battery_id]
            final_net_total -= float(forced_limits[b.battery_id])
            continue
        clamped = _clamp_for_soc(
            per_bat.get(b.battery_id, 0.0),
            b.soc_pct,
            limits_by_id[b.battery_id],
        )
        mode, limit_w = _plan_for_net(clamped)
        modes[b.battery_id] = mode
        limits_out[b.battery_id] = limit_w
        final_net_total += clamped

    reason_suffix = f" emergency_recovery={sorted(below_floor)}" if below_floor else ""
    reason = (
        f"zero_grid: grid={grid_power_w:.0f}W "
        f"bat_now={current_net:.0f}W target={target_net:.0f}W "
        f"applied={final_net_total:.0f}W{reason_suffix}"
    )
    return ZeroGridPlan(
        modes=modes,
        limits_w=limits_out,
        total_target_net_w=int(final_net_total),
        reason=reason,
        emergency_recovery=frozenset(below_floor),
    )
