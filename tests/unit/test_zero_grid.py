"""Tests for core.zero_grid (PLAT-1718 Zero-Grid Controller).

Goals covered:
  - Sign conventions (bat + grid) are honoured end-to-end.
  - Deadband keeps the inverter still when the deviation is ≤ 50 W.
  - Physical limits are respected — no charge above SoC cap, no
    discharge below SoC floor, no magnitude above max_*_w.
  - Multi-battery distribution matches the aggressive-balance rule when
    SoC-spread is >5 pp, otherwise proportional by capacity.
  - Convergence: simulating a few cycles drives a synthetic house
    load/PV imbalance to within the deadband.
"""

from __future__ import annotations

import pytest

from core.zero_grid import (
    BatLimits,
    BatSnapshot,
    ZeroGridPlan,
    plan_zero_grid,
)


_DEFAULT_LIMITS = BatLimits(
    max_charge_w=5000,
    max_discharge_w=5000,
    soc_min_pct=15.0,
    soc_max_pct=95.0,
)


def _snap(bid: str, power_w: float, soc_pct: float) -> BatSnapshot:
    return BatSnapshot(battery_id=bid, power_w=power_w, soc_pct=soc_pct)


# -------------------------------------------------------------------
# Sign + deadband
# -------------------------------------------------------------------


def test_grid_inside_deadband_holds_current_state() -> None:
    """|grid| < deadband → bat keeps its current power (no command shift)."""
    bats = [_snap("kontor", power_w=-1000, soc_pct=50)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=30.0,
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.limits_w["kontor"] == 1000


def test_grid_export_increases_charge() -> None:
    """Export of 500 W with bat already charging 1000 W → charge 1500 W."""
    bats = [_snap("kontor", power_w=-1000, soc_pct=50)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-500.0,
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.limits_w["kontor"] == 1500


def test_grid_import_reduces_charge() -> None:
    """Import of 400 W while charging 1000 W → drop charge to 600 W."""
    bats = [_snap("kontor", power_w=-1000, soc_pct=50)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=400.0,
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.limits_w["kontor"] == 600


def test_grid_import_above_charge_triggers_discharge() -> None:
    """Import larger than current charge flips the bat to discharge_pv."""
    bats = [_snap("kontor", power_w=-200, soc_pct=50)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=1500.0,
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "discharge_pv"
    assert plan.limits_w["kontor"] == 1300


def test_grid_export_with_bat_discharging_reduces_discharge() -> None:
    """Export while bat is discharging → reduce discharge (300 W less)."""
    bats = [_snap("kontor", power_w=800, soc_pct=50)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-300.0,
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    # target = 800 + (-300) = 500 discharge
    assert plan.modes["kontor"] == "discharge_pv"
    assert plan.limits_w["kontor"] == 500


# -------------------------------------------------------------------
# SoC caps
# -------------------------------------------------------------------


def test_charge_is_blocked_at_soc_max() -> None:
    """SoC at/above soc_max_pct → no charging, fallback standby."""
    bats = [_snap("kontor", power_w=0, soc_pct=95.0)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-2000.0,  # big export, would want charge
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "battery_standby"
    assert plan.limits_w["kontor"] == 0


def test_discharge_is_blocked_at_soc_min() -> None:
    """SoC at/below soc_min_pct → no discharging, fallback standby."""
    bats = [_snap("kontor", power_w=0, soc_pct=15.0)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=1500.0,  # big import, would want discharge
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "battery_standby"


def test_discharge_blocked_inside_soc_min_buffer() -> None:
    """SoC within soc_min_buffer_pct of the floor → still standby.

    Protects the in-flight drain between cycles: at SoC 15.8 % (inside
    the default 1 pp buffer) zero_grid must already say standby so the
    next-cycle sensor read does not catch the bat below 15 %.
    """
    bats = [_snap("kontor", power_w=0, soc_pct=15.8)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=3000.0,
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "battery_standby"
    assert plan.limits_w["kontor"] == 0


def test_discharge_allowed_above_soc_min_buffer() -> None:
    """SoC > soc_min_pct + buffer → discharge allowed normally."""
    bats = [_snap("kontor", power_w=0, soc_pct=16.5)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=1500.0,
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "discharge_pv"
    assert plan.limits_w["kontor"] == 1500


def test_soc_min_buffer_is_configurable() -> None:
    """Customer sites can override the buffer (e.g. larger hardware lag)."""
    strict_limits = BatLimits(
        max_charge_w=5000, max_discharge_w=5000,
        soc_min_pct=15.0, soc_max_pct=95.0,
        soc_min_buffer_pct=3.0,  # bigger margin
    )
    bats = [_snap("kontor", power_w=0, soc_pct=17.5)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=1500.0,
        bats=bats,
        limits_by_id={"kontor": strict_limits},
    )
    # 17.5 <= 15 + 3 → standby
    assert plan.modes["kontor"] == "battery_standby"


# -------------------------------------------------------------------
# Emergency recovery — SoC below floor → force grid-charge
# -------------------------------------------------------------------


def test_emergency_recovery_flag_set_when_below_floor() -> None:
    """A bat with SoC < soc_min_pct must be listed in emergency_recovery."""
    bats = [_snap("kontor", power_w=0, soc_pct=14.2)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-2000.0,     # export — normally would charge anyway
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert "kontor" in plan.emergency_recovery
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.limits_w["kontor"] == _DEFAULT_LIMITS.max_charge_w
    assert "emergency_recovery" in plan.reason


def test_emergency_recovery_overrides_grid_target() -> None:
    """SoC below floor forces charge even if grid is importing.

    The caller pairs this with fast_charging=True so the bat pulls the
    deficit from the grid regardless of PV/house balance — the floor
    safety trumps grid=0.
    """
    bats = [_snap("kontor", power_w=0, soc_pct=12.0)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=3000.0,     # big import — would normally discharge
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.limits_w["kontor"] == _DEFAULT_LIMITS.max_charge_w
    assert plan.emergency_recovery == frozenset({"kontor"})


def test_emergency_recovery_per_bat_only() -> None:
    """Only the bat below floor is in emergency mode; the other runs normal."""
    bats = [
        _snap("kontor", power_w=0, soc_pct=14.0),   # below
        _snap("forrad", power_w=0, soc_pct=60.0),   # healthy
    ]
    limits = {
        "kontor": _DEFAULT_LIMITS,
        "forrad": _DEFAULT_LIMITS,
    }
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=2000.0,
        bats=bats,
        limits_by_id=limits,
    )
    # kontor forced into charge_battery at max; forrad still handles the grid.
    assert plan.emergency_recovery == frozenset({"kontor"})
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.limits_w["kontor"] == _DEFAULT_LIMITS.max_charge_w
    assert plan.modes["forrad"] == "discharge_pv"  # covers the import


def test_emergency_recovery_clears_once_above_floor() -> None:
    """SoC exactly at soc_min_pct → NOT in emergency (buffer-standby only)."""
    bats = [_snap("kontor", power_w=0, soc_pct=15.0)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=1500.0,
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.emergency_recovery == frozenset()
    assert plan.modes["kontor"] == "battery_standby"


def test_charge_is_clamped_at_max_charge_w() -> None:
    """Target above physical cap is clamped."""
    bats = [_snap("kontor", power_w=0, soc_pct=50)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-10_000.0,
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.limits_w["kontor"] == _DEFAULT_LIMITS.max_charge_w


def test_discharge_is_clamped_at_max_discharge_w() -> None:
    bats = [_snap("kontor", power_w=0, soc_pct=50)]
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=10_000.0,
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "discharge_pv"
    assert plan.limits_w["kontor"] == _DEFAULT_LIMITS.max_discharge_w


# -------------------------------------------------------------------
# Multi-battery distribution
# -------------------------------------------------------------------


def test_two_bats_large_spread_aggressive_on_charge() -> None:
    """Charging with >5 pp spread → lower SoC gets all the charge."""
    bats = [
        _snap("kontor", power_w=0, soc_pct=40),
        _snap("forrad", power_w=0, soc_pct=50),
    ]
    limits = {
        "kontor": _DEFAULT_LIMITS,
        "forrad": _DEFAULT_LIMITS,
    }
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-3000.0, bats=bats, limits_by_id=limits,
    )
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.limits_w["kontor"] == 3000
    assert plan.modes["forrad"] == "battery_standby"
    assert plan.limits_w["forrad"] == 0


def test_two_bats_large_spread_aggressive_on_discharge() -> None:
    """Discharging with >5 pp spread → higher SoC bat discharges."""
    bats = [
        _snap("kontor", power_w=0, soc_pct=40),
        _snap("forrad", power_w=0, soc_pct=55),
    ]
    limits = {
        "kontor": _DEFAULT_LIMITS,
        "forrad": _DEFAULT_LIMITS,
    }
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=2000.0, bats=bats, limits_by_id=limits,
    )
    assert plan.modes["forrad"] == "discharge_pv"
    assert plan.limits_w["forrad"] == 2000
    assert plan.modes["kontor"] == "battery_standby"


def test_aggressive_charge_spills_overflow_to_secondary_bat() -> None:
    """PLAT-1718 live incident regression: target exceeds primary cap →
    secondary MUST also charge so grid stays ≤ 100 W.

    Scenario: 10 pp SoC spread makes kontor the primary charger; 7 kW
    export puts the target at -7 kW, but kontor can only absorb its
    5 kW cap. Without spill the grid kept exporting 2 kW (observed
    live: ``zero_grid: grid=-636W target=-5625W applied=-5000W``).
    """
    bats = [
        _snap("kontor", power_w=0, soc_pct=55),
        _snap("forrad", power_w=0, soc_pct=65),
    ]
    limits = {
        "kontor": _DEFAULT_LIMITS,  # max_charge_w = 5000
        "forrad": _DEFAULT_LIMITS,
    }
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-7000.0, bats=bats, limits_by_id=limits,
    )
    assert plan.limits_w["kontor"] == _DEFAULT_LIMITS.max_charge_w
    assert plan.modes["kontor"] == "charge_battery"
    # The overflow (2 kW) MUST spill into forrad so grid converges to 0.
    assert plan.modes["forrad"] == "charge_battery"
    assert plan.limits_w["forrad"] == 2000


def test_aggressive_discharge_spills_overflow_to_secondary_bat() -> None:
    """Mirror of the charge spill: large import, higher-SoC primary
    saturates at max_discharge_w → lower-SoC secondary fills the gap."""
    bats = [
        _snap("kontor", power_w=0, soc_pct=40),
        _snap("forrad", power_w=0, soc_pct=55),
    ]
    limits = {
        "kontor": _DEFAULT_LIMITS,
        "forrad": _DEFAULT_LIMITS,  # max_discharge_w = 5000
    }
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=6500.0, bats=bats, limits_by_id=limits,
    )
    assert plan.limits_w["forrad"] == _DEFAULT_LIMITS.max_discharge_w
    assert plan.modes["forrad"] == "discharge_pv"
    assert plan.modes["kontor"] == "discharge_pv"
    assert plan.limits_w["kontor"] == 1500


def test_two_bats_small_spread_proportional_by_capacity() -> None:
    """Inside 5 pp spread → split proportional by capacity."""
    bats = [
        _snap("kontor", power_w=0, soc_pct=50.0),
        _snap("forrad", power_w=0, soc_pct=51.0),
    ]
    limits = {
        "kontor": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=95.0,
        ),
        "forrad": BatLimits(
            max_charge_w=2500, max_discharge_w=2500,  # half the capacity
            soc_min_pct=15.0, soc_max_pct=95.0,
        ),
    }
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-3000.0, bats=bats, limits_by_id=limits,
    )
    # 2:1 split → 2000 kontor / 1000 forrad
    assert plan.limits_w["kontor"] == pytest.approx(2000, abs=1)
    assert plan.limits_w["forrad"] == pytest.approx(1000, abs=1)


# -------------------------------------------------------------------
# Convergence over multiple cycles
# -------------------------------------------------------------------


def test_convergence_to_zero_grid_within_three_cycles() -> None:
    """Simulate a 4 kW mismatch: bat should close the gap in ≤ 3 cycles."""
    # House load = 1000 W, PV = 5000 W → surplus 4000 W will export.
    house_w = 1000.0
    pv_w = 5000.0
    limits = BatLimits(
        max_charge_w=5000, max_discharge_w=5000,
        soc_min_pct=15.0, soc_max_pct=95.0,
    )
    bat_power_w = 0.0   # starts idle
    soc = 60.0
    grids: list[float] = []

    for _ in range(3):
        grid_w = house_w - pv_w - bat_power_w  # import minus export
        grids.append(grid_w)
        plan = plan_zero_grid(
        gain=1.0,
            grid_power_w=grid_w,
            bats=[BatSnapshot("k", bat_power_w, soc)],
            limits_by_id={"k": limits},
        )
        mode = plan.modes["k"]
        limit = plan.limits_w["k"]
        # Apply the plan: bat_power_w becomes mode-signed limit
        bat_power_w = -limit if mode == "charge_battery" else (
            limit if mode == "discharge_pv" else 0.0
        )

    final_grid = house_w - pv_w - bat_power_w
    assert abs(final_grid) <= 50.0, (
        f"Did not converge: grids across cycles={grids}, final={final_grid}"
    )


def test_plan_contains_reason_for_logging() -> None:
    """Plan reason must expose grid + net target for operator diagnostics."""
    bats = [_snap("kontor", power_w=-500, soc_pct=50)]
    plan: ZeroGridPlan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-1000.0, bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert "grid=" in plan.reason
    assert "target=" in plan.reason
