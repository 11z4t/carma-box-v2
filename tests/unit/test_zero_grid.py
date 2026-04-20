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
  - PLAT-1758: SoC momentum dampening prevents oscillation while still
    converging within 3 cycles.
"""

from __future__ import annotations

import pytest

from core.zero_grid import (
    BatLimits,
    BatSnapshot,
    ZeroGridPlan,
    ZeroGridState,
    _MOMENTUM_DAMPING_FACTOR,
    _MOMENTUM_WINDOW,
    _momentum_gain,
    plan_zero_grid,
    update_zero_grid_state,
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
        max_charge_w=5000,
        max_discharge_w=5000,
        soc_min_pct=15.0,
        soc_max_pct=95.0,
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
        grid_power_w=-2000.0,  # export — normally would charge anyway
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
        grid_power_w=3000.0,  # big import — would normally discharge
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.limits_w["kontor"] == _DEFAULT_LIMITS.max_charge_w
    assert plan.emergency_recovery == frozenset({"kontor"})


def test_emergency_recovery_per_bat_only() -> None:
    """Only the bat below floor is in emergency mode; the other runs normal."""
    bats = [
        _snap("kontor", power_w=0, soc_pct=14.0),  # below
        _snap("forrad", power_w=0, soc_pct=60.0),  # healthy
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
        grid_power_w=-3000.0,
        bats=bats,
        limits_by_id=limits,
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
        grid_power_w=2000.0,
        bats=bats,
        limits_by_id=limits,
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
        grid_power_w=-7000.0,
        bats=bats,
        limits_by_id=limits,
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
        grid_power_w=6500.0,
        bats=bats,
        limits_by_id=limits,
    )
    assert plan.limits_w["forrad"] == _DEFAULT_LIMITS.max_discharge_w
    assert plan.modes["forrad"] == "discharge_pv"
    assert plan.modes["kontor"] == "discharge_pv"
    assert plan.limits_w["kontor"] == 1500


def test_aggressive_mode_spill_with_unequal_primaries() -> None:
    """PLAT-1756: aggressive mode with asymmetric primary caps uses per-bat weighting.

    4-bat scenario (mid=2) → two primaries: kontor (3000 W) and forrad_small
    (1000 W).  Equal split (bug) gives each 1800 W, which exceeds forrad_small's
    cap.  Per-cap weighting must give kontor=2700 W and forrad_small=900 W so
    the total allocation reaches the 3600 W target without overflowing any cap.
    """
    bats = [
        _snap("kontor", power_w=0, soc_pct=30),  # primary (lower SoC)
        _snap("forrad_small", power_w=0, soc_pct=40),  # primary (lower SoC)
        _snap("bat_c", power_w=0, soc_pct=50),  # secondary
        _snap("bat_d", power_w=0, soc_pct=60),  # secondary
    ]
    limits = {
        "kontor": BatLimits(
            max_charge_w=3000,
            max_discharge_w=3000,
            soc_min_pct=15.0,
            soc_max_pct=95.0,
        ),
        "forrad_small": BatLimits(
            max_charge_w=1000,
            max_discharge_w=1000,
            soc_min_pct=15.0,
            soc_max_pct=95.0,
        ),
        "bat_c": _DEFAULT_LIMITS,
        "bat_d": _DEFAULT_LIMITS,
    }
    # 3600 W export → charge target = 3600 W
    # Primary cap = 3000+1000 = 4000 ≥ 3600 → no overflow to secondary
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-3600.0,
        bats=bats,
        limits_by_id=limits,
    )
    # Secondary stays idle — primary absorbs the full target
    assert plan.modes["bat_c"] == "battery_standby"
    assert plan.modes["bat_d"] == "battery_standby"
    # Per-cap split: kontor=3600*3000/4000=2700, forrad_small=3600*1000/4000=900
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.modes["forrad_small"] == "charge_battery"
    assert plan.limits_w["kontor"] == pytest.approx(2700, abs=1)
    assert plan.limits_w["forrad_small"] == pytest.approx(900, abs=1)
    # Total allocation reaches target
    assert plan.limits_w["kontor"] + plan.limits_w["forrad_small"] == pytest.approx(3600, abs=1)


def test_two_bats_small_spread_proportional_by_capacity() -> None:
    """Inside 5 pp spread → split proportional by capacity."""
    bats = [
        _snap("kontor", power_w=0, soc_pct=50.0),
        _snap("forrad", power_w=0, soc_pct=51.0),
    ]
    limits = {
        "kontor": BatLimits(
            max_charge_w=5000,
            max_discharge_w=5000,
            soc_min_pct=15.0,
            soc_max_pct=95.0,
        ),
        "forrad": BatLimits(
            max_charge_w=2500,
            max_discharge_w=2500,  # half the capacity
            soc_min_pct=15.0,
            soc_max_pct=95.0,
        ),
    }
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-3000.0,
        bats=bats,
        limits_by_id=limits,
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
        max_charge_w=5000,
        max_discharge_w=5000,
        soc_min_pct=15.0,
        soc_max_pct=95.0,
    )
    bat_power_w = 0.0  # starts idle
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
        bat_power_w = (
            -limit if mode == "charge_battery" else (limit if mode == "discharge_pv" else 0.0)
        )

    final_grid = house_w - pv_w - bat_power_w
    assert (
        abs(final_grid) <= 50.0
    ), f"Did not converge: grids across cycles={grids}, final={final_grid}"


def test_proportional_by_capacity_asymmetric() -> None:
    """PLAT-1755 kärnfix: balanced split uses cap_kwh, not max_charge_w.

    kontor = 15 kWh, förråd = 5 kWh — same max_charge_w (5 kW each).
    Without cap_kwh weighting: 50/50 split → förråd fills 3x faster.
    With cap_kwh weighting: 15/(15+5)=75 % → kontor, 25 % → förråd.

    Reference: PLAT-1715 (Highest prio).
    """
    bats = [
        BatSnapshot(battery_id="kontor", power_w=0.0, soc_pct=50.0, cap_kwh=15.0),
        BatSnapshot(battery_id="forrad", power_w=0.0, soc_pct=51.0, cap_kwh=5.0),
    ]
    limits = {
        "kontor": BatLimits(
            max_charge_w=5000,
            max_discharge_w=5000,
            soc_min_pct=15.0,
            soc_max_pct=95.0,
        ),
        "forrad": BatLimits(
            max_charge_w=5000,
            max_discharge_w=5000,  # same power cap, different cap_kwh
            soc_min_pct=15.0,
            soc_max_pct=95.0,
        ),
    }
    plan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-3000.0,
        bats=bats,
        limits_by_id=limits,
    )
    # 15/(15+5) * 3000 = 2250 W kontor, 5/20 * 3000 = 750 W förråd
    assert plan.limits_w["kontor"] == pytest.approx(2250, abs=1)
    assert plan.limits_w["forrad"] == pytest.approx(750, abs=1)
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.modes["forrad"] == "charge_battery"


def test_plan_contains_reason_for_logging() -> None:
    """Plan reason must expose grid + net target for operator diagnostics."""
    bats = [_snap("kontor", power_w=-500, soc_pct=50)]
    plan: ZeroGridPlan = plan_zero_grid(
        gain=1.0,
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id={"kontor": _DEFAULT_LIMITS},
    )
    assert "grid=" in plan.reason
    assert "target=" in plan.reason


# -------------------------------------------------------------------
# PLAT-1758: SoC momentum dampening
# -------------------------------------------------------------------

_MOMENTUM_SOC_START: float = 50.0
_MOMENTUM_SOC_RISING: float = 52.0
_MOMENTUM_SOC_RISING_2: float = 54.0
_MOMENTUM_BASE_GAIN: float = 0.7
_MOMENTUM_SINGLE_STEP: float = 1.0
_MOMENTUM_FALLING: float = 48.0
_MOMENTUM_FALLING_2: float = 46.0
_OSCILLATION_SOC_HIGH: float = 55.0
_CONVERGENCE_DELTA_PCT: float = 5.0
_CONVERGENCE_TARGET_SOC: float = 60.0
_CONVERGENCE_MAX_CYCLES: int = 3
_CONVERGENCE_GRID_W: float = -4000.0
_CONVERGENCE_HOUSE_W: float = 1000.0
_CONVERGENCE_PV_W: float = 5000.0


def test_update_zero_grid_state_from_none() -> None:
    """First call with state=None creates 1-element history."""
    bats = [_snap("k", power_w=0.0, soc_pct=_MOMENTUM_SOC_START)]
    state = update_zero_grid_state(None, bats)
    assert isinstance(state, ZeroGridState)
    assert len(state.soc_history) == 1
    assert state.soc_history[0] == pytest.approx(_MOMENTUM_SOC_START)


def test_update_zero_grid_state_accumulates() -> None:
    """Successive updates accumulate up to _MOMENTUM_WINDOW readings."""
    bats_a = [_snap("k", power_w=0.0, soc_pct=_MOMENTUM_SOC_START)]
    bats_b = [_snap("k", power_w=0.0, soc_pct=_MOMENTUM_SOC_RISING)]
    state = update_zero_grid_state(None, bats_a)
    state = update_zero_grid_state(state, bats_b)
    assert len(state.soc_history) == 2
    assert state.soc_history[0] == pytest.approx(_MOMENTUM_SOC_START)
    assert state.soc_history[1] == pytest.approx(_MOMENTUM_SOC_RISING)


def test_update_zero_grid_state_capped_at_window() -> None:
    """History never exceeds _MOMENTUM_WINDOW entries (oldest dropped)."""
    state: ZeroGridState | None = None
    for i in range(_MOMENTUM_WINDOW + 2):
        bats_i = [_snap("k", power_w=0.0, soc_pct=_MOMENTUM_SOC_START + i)]
        state = update_zero_grid_state(state, bats_i)
    assert state is not None
    assert len(state.soc_history) == _MOMENTUM_WINDOW


def test_update_zero_grid_state_averages_multiple_bats() -> None:
    """Average SoC across all bats is stored, not first-bat value."""
    bats = [
        _snap("kontor", power_w=0.0, soc_pct=40.0),
        _snap("forrad", power_w=0.0, soc_pct=60.0),
    ]
    state = update_zero_grid_state(None, bats)
    assert state.soc_history[0] == pytest.approx(50.0)


def test_momentum_gain_no_state_returns_base() -> None:
    """state=None → no dampening, returns base_gain unchanged."""
    result = _momentum_gain(None, _MOMENTUM_BASE_GAIN)
    assert result == pytest.approx(_MOMENTUM_BASE_GAIN)


def test_momentum_gain_single_reading_returns_base() -> None:
    """One SoC reading — insufficient for trend detection, no dampening."""
    state = ZeroGridState(soc_history=(_MOMENTUM_SOC_START,))
    result = _momentum_gain(state, _MOMENTUM_BASE_GAIN)
    assert result == pytest.approx(_MOMENTUM_BASE_GAIN)


def test_momentum_gain_consistent_rising_trend_dampens() -> None:
    """Consistently rising SoC → correction in progress → dampen gain."""
    state = ZeroGridState(
        soc_history=(
            _MOMENTUM_SOC_START,
            _MOMENTUM_SOC_RISING,
            _MOMENTUM_SOC_RISING_2,
        )
    )
    result = _momentum_gain(state, _MOMENTUM_BASE_GAIN)
    assert result == pytest.approx(_MOMENTUM_BASE_GAIN * _MOMENTUM_DAMPING_FACTOR)


def test_momentum_gain_consistent_falling_trend_dampens() -> None:
    """Consistently falling SoC → correction in progress → dampen gain."""
    state = ZeroGridState(
        soc_history=(
            _MOMENTUM_SOC_RISING_2,
            _MOMENTUM_SOC_RISING,
            _MOMENTUM_SOC_START,
        )
    )
    result = _momentum_gain(state, _MOMENTUM_BASE_GAIN)
    assert result == pytest.approx(_MOMENTUM_BASE_GAIN * _MOMENTUM_DAMPING_FACTOR)


def test_momentum_gain_oscillating_trend_no_dampen() -> None:
    """Oscillating SoC (alternating sign) → no dampening — system unstable."""
    # up then down: deltas are +5, -5 → mixed signs → no dampen
    state_osc = ZeroGridState(
        soc_history=(_MOMENTUM_SOC_START, _OSCILLATION_SOC_HIGH, _MOMENTUM_SOC_START)
    )
    result = _momentum_gain(state_osc, _MOMENTUM_BASE_GAIN)
    assert result == pytest.approx(_MOMENTUM_BASE_GAIN)


def test_plan_zero_grid_state_none_unchanged() -> None:
    """state=None produces identical result to calling without state param."""
    bats = [_snap("k", power_w=0.0, soc_pct=50.0)]
    limits = {"k": _DEFAULT_LIMITS}
    plan_no_state = plan_zero_grid(
        grid_power_w=-2000.0, bats=bats, limits_by_id=limits, gain=_MOMENTUM_BASE_GAIN
    )
    plan_with_none = plan_zero_grid(
        grid_power_w=-2000.0,
        bats=bats,
        limits_by_id=limits,
        gain=_MOMENTUM_BASE_GAIN,
        state=None,
    )
    assert plan_no_state.modes == plan_with_none.modes
    assert plan_no_state.limits_w == plan_with_none.limits_w
    assert plan_no_state.total_target_net_w == plan_with_none.total_target_net_w


def test_plan_zero_grid_converging_state_reduces_aggressiveness() -> None:
    """AC2: consistent trend → plan limit is lower than without momentum."""
    bats = [_snap("k", power_w=-1000.0, soc_pct=50.0)]
    limits = {"k": _DEFAULT_LIMITS}
    converging_state = ZeroGridState(
        soc_history=(
            _MOMENTUM_SOC_START,
            _MOMENTUM_SOC_RISING,
            _MOMENTUM_SOC_RISING_2,
        )
    )
    plan_full = plan_zero_grid(
        grid_power_w=-3000.0,
        bats=bats,
        limits_by_id=limits,
        gain=_MOMENTUM_BASE_GAIN,
        state=None,
    )
    plan_dampened = plan_zero_grid(
        grid_power_w=-3000.0,
        bats=bats,
        limits_by_id=limits,
        gain=_MOMENTUM_BASE_GAIN,
        state=converging_state,
    )
    # Dampened plan should request less charge power
    assert plan_dampened.limits_w["k"] < plan_full.limits_w["k"]
    assert plan_dampened.modes["k"] == "charge_battery"


def test_convergence_with_momentum_within_three_cycles() -> None:
    """AC3: ±5% SoC oscillation scenario converges to grid=0 within 3 cycles.

    Starts with fresh state (no prior history) so first 3 cycles run
    without dampening — momentum kicks in only when sufficient history
    exists. This guarantees AC3 is unaffected by the dampening logic.
    """
    limits = BatLimits(
        max_charge_w=5000,
        max_discharge_w=5000,
        soc_min_pct=15.0,
        soc_max_pct=95.0,
    )
    bat_power_w = 0.0
    soc = _CONVERGENCE_TARGET_SOC - _CONVERGENCE_DELTA_PCT  # 5 pp below target
    state: ZeroGridState | None = None
    grids: list[float] = []

    for _ in range(_CONVERGENCE_MAX_CYCLES):
        grid_w = _CONVERGENCE_HOUSE_W - _CONVERGENCE_PV_W - bat_power_w
        grids.append(grid_w)
        bats = [BatSnapshot("k", bat_power_w, soc)]
        plan = plan_zero_grid(
            gain=1.0,
            grid_power_w=grid_w,
            bats=bats,
            limits_by_id={"k": limits},
            state=state,
        )
        state = update_zero_grid_state(state, bats)
        mode = plan.modes["k"]
        limit = plan.limits_w["k"]
        bat_power_w = (
            -limit if mode == "charge_battery" else (limit if mode == "discharge_pv" else 0.0)
        )
        soc += (bat_power_w * 15.0 / 3600.0 / limits.max_charge_w) * 100.0

    final_grid = _CONVERGENCE_HOUSE_W - _CONVERGENCE_PV_W - bat_power_w
    assert (
        abs(final_grid) <= 50.0
    ), f"Convergence failed: cycle grids={grids}, final={final_grid:.0f}W"


def test_no_new_oscillation_with_momentum_state() -> None:
    """AC4-adjacent: oscillating state does not introduce extra oscillation.

    When SoC history shows oscillation (alternating sign), gain is NOT
    dampened — the system should correct at full gain to stabilise.
    """
    bats = [_snap("k", power_w=0.0, soc_pct=50.0)]
    limits = {"k": _DEFAULT_LIMITS}
    oscillating_state = ZeroGridState(
        soc_history=(_MOMENTUM_SOC_START, _OSCILLATION_SOC_HIGH, _MOMENTUM_SOC_START)
    )
    plan_baseline = plan_zero_grid(
        grid_power_w=-2000.0,
        bats=bats,
        limits_by_id=limits,
        gain=_MOMENTUM_BASE_GAIN,
        state=None,
    )
    plan_oscillating = plan_zero_grid(
        grid_power_w=-2000.0,
        bats=bats,
        limits_by_id=limits,
        gain=_MOMENTUM_BASE_GAIN,
        state=oscillating_state,
    )
    # Oscillating history → same as no state (full gain)
    assert plan_oscillating.limits_w["k"] == plan_baseline.limits_w["k"]
    assert plan_oscillating.modes["k"] == plan_baseline.modes["k"]
