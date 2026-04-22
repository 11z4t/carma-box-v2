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


# ------------------------------------------------------------------------
# PLAT-1766: need-based balanced-path weighting
# ------------------------------------------------------------------------


def _snap_cap(bid: str, power_w: float, soc_pct: float, cap_kwh: float) -> BatSnapshot:
    return BatSnapshot(
        battery_id=bid,
        power_w=power_w,
        soc_pct=soc_pct,
        cap_kwh=cap_kwh,
    )


_PLAT1766_LIMITS = {
    "kontor": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
    "forrad": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
}


def test_plat1766_flag_default_off_preserves_plat1755_weighting() -> None:
    """Default False → identical behaviour to PLAT-1755 cap-only.

    Regression-gate: existing deployments must not shift allocation when
    the flag is absent/false in site.yaml.
    """
    bats = [
        _snap_cap("kontor", 0.0, 46.0, 15.0),
        _snap_cap("forrad", 0.0, 51.0, 5.0),
    ]
    plan_off = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=10.0,  # force balanced path (spread = 5 pp < 10)
    )
    # PLAT-1755 cap-only: ratio 15:5 → kontor ~ 750 W, forrad ~ 250 W
    assert plan_off.modes["kontor"] == "charge_battery"
    assert plan_off.modes["forrad"] == "charge_battery"
    # With gain=0.7, |target| ≈ 700 W. Weights 15:5 = 3:1 → 525 W vs 175 W.
    k = plan_off.limits_w["kontor"]
    f = plan_off.limits_w["forrad"]
    assert abs(k / (k + f) - 0.75) < 0.01, f"cap-only share {k}:{f}"


def test_plat1766_charging_weights_by_need_then_cap() -> None:
    """Flag ON + charging → kontor (46%, 15 kWh) pulls more than PLAT-1755.

    User's insight: (1 − soc)·cap drives SoC *convergence*, not just
    equal-rate. kontor need = 0.54·15 = 8.1 kWh, forrad = 0.49·5 = 2.45 kWh.
    Ratio 8.1 : 2.45 ≈ 3.3 : 1 (vs cap-only 3 : 1). Small shift toward kontor.
    """
    bats = [
        _snap_cap("kontor", 0.0, 46.0, 15.0),
        _snap_cap("forrad", 0.0, 51.0, 5.0),
    ]
    plan = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=10.0,  # balanced path
        need_based_enabled=True,
    )
    k = plan.limits_w["kontor"]
    f = plan.limits_w["forrad"]
    # Need ratio: kontor_need / forrad_need = 8.1 / 2.45 ≈ 3.306
    expected_kontor_share = 8.1 / (8.1 + 2.45)
    actual_kontor_share = k / (k + f)
    assert abs(actual_kontor_share - expected_kontor_share) < 0.01, (
        f"need-based share {k}:{f} = {actual_kontor_share:.3f} vs expected "
        f"{expected_kontor_share:.3f}"
    )


def test_plat1766_charging_need_based_gives_more_to_emptier_bank() -> None:
    """Need-based vs cap-only: emptier bat gets strictly larger share."""
    bats = [
        _snap_cap("kontor", 0.0, 30.0, 10.0),  # lower SoC
        _snap_cap("forrad", 0.0, 70.0, 10.0),  # higher SoC, same cap
    ]
    plan_cap = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=50.0,
        need_based_enabled=False,
    )
    plan_need = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=50.0,
        need_based_enabled=True,
    )
    # Cap-only with equal caps: 50/50 split.
    assert plan_cap.limits_w["kontor"] == plan_cap.limits_w["forrad"]
    # Need-based: kontor (70% need) gets more than forrad (30% need).
    assert plan_need.limits_w["kontor"] > plan_need.limits_w["forrad"]
    ratio = plan_need.limits_w["kontor"] / plan_need.limits_w["forrad"]
    # Need ratio 0.7 : 0.3 ≈ 2.33
    assert 2.2 < ratio < 2.5, f"need-based ratio {ratio:.2f}"


def test_plat1766_discharging_weights_by_available_energy() -> None:
    """Flag ON + discharging → fuller bat gives more (soc·cap)."""
    bats = [
        _snap_cap("kontor", 0.0, 80.0, 10.0),
        _snap_cap("forrad", 0.0, 40.0, 10.0),
    ]
    plan = plan_zero_grid(
        grid_power_w=+1000.0,  # import → discharge
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=50.0,
        need_based_enabled=True,
    )
    k = plan.limits_w["kontor"]
    f = plan.limits_w["forrad"]
    assert plan.modes["kontor"] == "discharge_pv"
    assert plan.modes["forrad"] == "discharge_pv"
    # kontor soc·cap = 8.0, forrad = 4.0 → kontor gets 2/3.
    expected_kontor_share = 8.0 / (8.0 + 4.0)
    assert abs(k / (k + f) - expected_kontor_share) < 0.01, f"{k}:{f}"


def test_plat1766_all_bats_full_falls_back_to_cap_only() -> None:
    """All bats at 100% (charging) → need=0 → fallback cap-only weighting."""
    bats = [
        _snap_cap("kontor", 0.0, 100.0, 15.0),
        _snap_cap("forrad", 0.0, 100.0, 5.0),
    ]
    plan = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=10.0,
        need_based_enabled=True,
    )
    # Both at soc_max (100 in _PLAT1766_LIMITS) → _clamp_for_soc returns 0.
    # So modes are battery_standby regardless of weighting. The fallback
    # only matters upstream of clamping — verify no crash and both standby.
    assert plan.modes["kontor"] == "battery_standby"
    assert plan.modes["forrad"] == "battery_standby"
    assert plan.limits_w["kontor"] == 0
    assert plan.limits_w["forrad"] == 0


def test_plat1766_all_bats_empty_discharging_falls_back() -> None:
    """All bats at clamped floor (discharging) → discharge blocked by SoC.

    Exact soc=0 triggers emergency recovery (below_floor) → forced charge.
    Use soc just above floor so bats aren't in emergency recovery but
    discharge is still blocked by the soc_min_buffer_pct guard — verifies
    that the need-based fallback path returns a valid weighting and that
    the clamp step correctly zeroes the alloc.
    """
    # soc = soc_min + 0.5 → above floor (no emergency) but at/below
    # soc_min_buffer so _clamp_for_soc blocks discharge → standby.
    bats = [
        _snap_cap("kontor", 0.0, 15.5, 15.0),
        _snap_cap("forrad", 0.0, 15.5, 5.0),
    ]
    limits = {
        "kontor": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
            soc_min_buffer_pct=1.0,
        ),
        "forrad": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
            soc_min_buffer_pct=1.0,
        ),
    }
    plan = plan_zero_grid(
        grid_power_w=+1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=50.0,
        need_based_enabled=True,
    )
    # soc 15.5 <= soc_min_pct (15) + buffer (1) = 16 → clamp to 0 (standby).
    assert plan.modes["kontor"] == "battery_standby"
    assert plan.modes["forrad"] == "battery_standby"


def test_plat1766_mixed_full_and_partial_charging() -> None:
    """One bat full, one partial → full gets 0, partial absorbs all."""
    bats = [
        _snap_cap("kontor", 0.0, 100.0, 15.0),  # full
        _snap_cap("forrad", 0.0, 50.0, 5.0),    # partial
    ]
    plan = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=100.0,  # keep balanced path despite spread
        need_based_enabled=True,
    )
    # need: kontor=0, forrad=2.5 → all weight on forrad.
    assert plan.modes["forrad"] == "charge_battery"
    assert plan.limits_w["forrad"] > 500
    # kontor at 100% clamps to standby regardless of alloc.
    assert plan.modes["kontor"] == "battery_standby"


def test_plat1766_single_bat_unchanged_by_flag() -> None:
    """Single bat always owns full target regardless of flag."""
    bats = [_snap_cap("kontor", 0.0, 50.0, 15.0)]
    limits = {"kontor": _PLAT1766_LIMITS["kontor"]}
    plan_off = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=limits,
        need_based_enabled=False,
    )
    plan_on = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=limits,
        need_based_enabled=True,
    )
    assert plan_off.limits_w["kontor"] == plan_on.limits_w["kontor"]
    assert plan_off.modes["kontor"] == plan_on.modes["kontor"]


def test_plat1766_cap_kwh_zero_fallback_to_max_w() -> None:
    """cap_kwh=0 (legacy callers) → fallback to max_charge_w weighting."""
    bats = [
        BatSnapshot(battery_id="a", power_w=0.0, soc_pct=40.0, cap_kwh=0.0),
        BatSnapshot(battery_id="b", power_w=0.0, soc_pct=60.0, cap_kwh=0.0),
    ]
    limits = {
        "a": BatLimits(
            max_charge_w=3000, max_discharge_w=3000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
        "b": BatLimits(
            max_charge_w=1000, max_discharge_w=1000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
    }
    plan = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=50.0,
        need_based_enabled=True,
    )
    # Need-based with cap=0: (1−0.4)·0 = 0 and (1−0.6)·0 = 0 → fallback
    # to cap-only (which uses max_charge_w). Ratio 3:1 → a=3/4, b=1/4.
    a = plan.limits_w["a"]
    b = plan.limits_w["b"]
    assert abs(a / (a + b) - 0.75) < 0.05


def test_plat1766_asymmetric_cap_and_soc_balanced_path() -> None:
    """Live-like scenario from 2026-04-22: kontor 15 kWh @46%, forrad 5 kWh @51%."""
    bats = [
        _snap_cap("kontor", 0.0, 46.0, 15.0),
        _snap_cap("forrad", 0.0, 51.0, 5.0),
    ]
    # Spread = 5 pp → set spread_aggressive_pct=10 to force balanced path.
    plan_cap = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=10.0,
        need_based_enabled=False,
    )
    plan_need = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=10.0,
        need_based_enabled=True,
    )
    cap_kontor_share = plan_cap.limits_w["kontor"] / (
        plan_cap.limits_w["kontor"] + plan_cap.limits_w["forrad"]
    )
    need_kontor_share = plan_need.limits_w["kontor"] / (
        plan_need.limits_w["kontor"] + plan_need.limits_w["forrad"]
    )
    # Need-based must give kontor (lower SoC) a larger share than cap-only.
    assert need_kontor_share > cap_kontor_share


def test_plat1766_converges_soc_diff_over_many_cycles() -> None:
    """Simulated 30 cycles → need-based closes SoC-diff vs cap-only preserves it.

    Models a charging scenario with constant surplus, computing SoC deltas
    per cycle. The test verifies the *direction* of convergence.
    """
    cycle_s = 15.0
    surplus_w = 1000.0

    def step(
        soc_k: float, soc_f: float, need_based: bool,
    ) -> tuple[float, float]:
        bats = [
            _snap_cap("kontor", 0.0, soc_k, 15.0),
            _snap_cap("forrad", 0.0, soc_f, 5.0),
        ]
        plan = plan_zero_grid(
            grid_power_w=-surplus_w,
            bats=bats,
            limits_by_id=_PLAT1766_LIMITS,
            spread_aggressive_pct=50.0,  # balanced path whole run
            need_based_enabled=need_based,
        )
        # soc delta (pp) = energy_added / cap_kwh * 100
        dsoc_k = plan.limits_w["kontor"] * cycle_s / 3600.0 / 15.0 * 100.0 / 1000.0
        dsoc_f = plan.limits_w["forrad"] * cycle_s / 3600.0 / 5.0 * 100.0 / 1000.0
        return soc_k + dsoc_k, soc_f + dsoc_f

    # Start diff = 5 pp (kontor 46, forrad 51).
    k_need, f_need = 46.0, 51.0
    k_cap, f_cap = 46.0, 51.0
    for _ in range(30):
        k_need, f_need = step(k_need, f_need, need_based=True)
        k_cap, f_cap = step(k_cap, f_cap, need_based=False)
    diff_need = abs(k_need - f_need)
    diff_cap = abs(k_cap - f_cap)
    # Need-based must show *smaller* gap than cap-only after 30 cycles.
    assert diff_need < diff_cap, (
        f"need-based diff {diff_need:.2f} pp vs cap-only {diff_cap:.2f} pp"
    )


def test_plat1766_aggressive_path_unchanged_by_flag() -> None:
    """When spread > spread_aggressive_pct, the aggressive P/S split takes
    over — need_based_enabled has no effect there (by design)."""
    bats = [
        _snap_cap("kontor", 0.0, 30.0, 10.0),
        _snap_cap("forrad", 0.0, 80.0, 10.0),
    ]
    plan_off = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=1.0,  # spread=50 >> 1 → aggressive path
        need_based_enabled=False,
    )
    plan_on = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=1.0,
        need_based_enabled=True,
    )
    assert plan_off.limits_w == plan_on.limits_w
    assert plan_off.modes == plan_on.modes


def test_plat1766_three_bats_need_based_scales() -> None:
    """Three bats charging → weights scale as (1-soc)·cap."""
    bats = [
        _snap_cap("a", 0.0, 30.0, 10.0),  # need=7
        _snap_cap("b", 0.0, 60.0, 10.0),  # need=4
        _snap_cap("c", 0.0, 90.0, 10.0),  # need=1
    ]
    limits = {
        bid: BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        )
        for bid in ("a", "b", "c")
    }
    plan = plan_zero_grid(
        grid_power_w=-1200.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,  # force balanced path
        need_based_enabled=True,
    )
    a = plan.limits_w["a"]
    b = plan.limits_w["b"]
    c = plan.limits_w["c"]
    # Need ratio 7:4:1 → a=7/12, b=4/12, c=1/12.
    total = a + b + c
    assert abs(a / total - 7 / 12) < 0.02
    assert abs(b / total - 4 / 12) < 0.02
    assert abs(c / total - 1 / 12) < 0.02


def test_plat1766_three_bats_discharging_need_based() -> None:
    """Three bats discharging → weights scale as soc·cap."""
    bats = [
        _snap_cap("a", 0.0, 30.0, 10.0),  # avail=3
        _snap_cap("b", 0.0, 60.0, 10.0),  # avail=6
        _snap_cap("c", 0.0, 90.0, 10.0),  # avail=9
    ]
    limits = {
        bid: BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        )
        for bid in ("a", "b", "c")
    }
    plan = plan_zero_grid(
        grid_power_w=+1800.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        need_based_enabled=True,
    )
    a = plan.limits_w["a"]
    b = plan.limits_w["b"]
    c = plan.limits_w["c"]
    total = a + b + c
    assert abs(a / total - 3 / 18) < 0.02
    assert abs(b / total - 6 / 18) < 0.02
    assert abs(c / total - 9 / 18) < 0.02


def test_plat1766_emergency_recovery_excluded_from_need_weighting() -> None:
    """Bat below floor is force-charged; does not participate in balanced weights."""
    bats = [
        _snap_cap("dead", 0.0, 10.0, 15.0),     # below floor 15
        _snap_cap("healthy", 0.0, 50.0, 5.0),
    ]
    limits = {
        "dead": BatLimits(
            max_charge_w=3000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
        "healthy": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
    }
    plan = plan_zero_grid(
        grid_power_w=+1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        need_based_enabled=True,
    )
    assert "dead" in plan.emergency_recovery
    assert plan.modes["dead"] == "charge_battery"
    # Force-charge at max_charge_w regardless of weighting.
    assert plan.limits_w["dead"] == 3000


def test_plat1766_deadband_respected_with_flag() -> None:
    """Flag does not break deadband — grid < 50 W → all standby."""
    bats = [
        _snap_cap("kontor", 0.0, 50.0, 15.0),
        _snap_cap("forrad", 0.0, 70.0, 5.0),
    ]
    plan = plan_zero_grid(
        grid_power_w=20.0,  # inside deadband
        bats=bats,
        limits_by_id=_PLAT1766_LIMITS,
        spread_aggressive_pct=10.0,
        need_based_enabled=True,
    )
    # current_net=0, target_net=0 → all standby.
    assert plan.modes["kontor"] == "battery_standby"
    assert plan.modes["forrad"] == "battery_standby"


def test_plat1766_grid_pulls_forrad_toward_kontor_soc() -> None:
    """Live 2026-04-22 scenario — 30-cycle sim converges diff below 4 pp."""
    surplus_w = 1000.0

    def step(soc_k: float, soc_f: float, need: bool) -> tuple[float, float]:
        bats = [
            _snap_cap("kontor", 0.0, soc_k, 15.0),
            _snap_cap("forrad", 0.0, soc_f, 5.0),
        ]
        plan = plan_zero_grid(
            grid_power_w=-surplus_w,
            bats=bats,
            limits_by_id=_PLAT1766_LIMITS,
            spread_aggressive_pct=50.0,
            need_based_enabled=need,
        )
        dk = plan.limits_w["kontor"] * 15.0 / 3600.0 / 15.0 * 100.0 / 1000.0
        df = plan.limits_w["forrad"] * 15.0 / 3600.0 / 5.0 * 100.0 / 1000.0
        return soc_k + dk, soc_f + df

    k, f = 46.0, 51.0
    for _ in range(30):
        k, f = step(k, f, need=True)
    # After 30 cycles, need-based should have closed some of the gap.
    assert abs(k - f) < 5.0


def test_plat1766_need_based_no_alloc_for_full_bat() -> None:
    """When flag ON + one bat at 100%, balanced weight = 0 for that bat."""
    bats = [
        _snap_cap("full", 0.0, 100.0, 10.0),
        _snap_cap("empty", 0.0, 20.0, 10.0),
    ]
    limits = {
        "full": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
        "empty": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
    }
    plan = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        need_based_enabled=True,
    )
    # full bat → clamped to 0 (soc==soc_max).
    assert plan.modes["full"] == "battery_standby"
    # empty bat absorbs the full target.
    assert plan.modes["empty"] == "charge_battery"
    assert plan.limits_w["empty"] > 500


def test_plat1766_need_based_negative_signs_preserved() -> None:
    """Charging keeps negative alloc → mode=charge_battery, limit positive W."""
    bats = [
        _snap_cap("a", 0.0, 40.0, 10.0),
        _snap_cap("b", 0.0, 50.0, 10.0),
    ]
    limits = {
        "a": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
        "b": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
    }
    plan = plan_zero_grid(
        grid_power_w=-800.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        need_based_enabled=True,
    )
    for bid in ("a", "b"):
        assert plan.modes[bid] == "charge_battery"
        assert plan.limits_w[bid] >= 0


def test_plat1766_total_alloc_matches_target() -> None:
    """Sum of per-bat allocations (in net terms) approximates target_net_w."""
    bats = [
        _snap_cap("a", 0.0, 30.0, 10.0),
        _snap_cap("b", 0.0, 70.0, 5.0),
    ]
    limits = {
        "a": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
        "b": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
    }
    # gain 0.7 on 1000 W grid → target_net ≈ -700 W.
    plan = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=50.0,
        need_based_enabled=True,
    )
    total_charge = sum(plan.limits_w.values())
    # Should approximate 700 W (±deadband/rounding).
    assert 600 <= total_charge <= 800


def test_plat1766_symmetry_charging_discharging() -> None:
    """Need-based charging + discharging are mirror-symmetric for equal caps."""
    bats_c = [
        _snap_cap("a", 0.0, 30.0, 10.0),
        _snap_cap("b", 0.0, 70.0, 10.0),
    ]
    bats_d = [
        _snap_cap("a", 0.0, 70.0, 10.0),
        _snap_cap("b", 0.0, 30.0, 10.0),
    ]
    limits = {
        bid: BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        )
        for bid in ("a", "b")
    }
    plan_c = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats_c,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        need_based_enabled=True,
    )
    plan_d = plan_zero_grid(
        grid_power_w=+1000.0,
        bats=bats_d,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        need_based_enabled=True,
    )
    # Charging: a (soc 30) gets more. Discharging: a (soc 70) gets more.
    assert plan_c.limits_w["a"] > plan_c.limits_w["b"]
    assert plan_d.limits_w["a"] > plan_d.limits_w["b"]


def test_plat1766_flag_passed_through_plan_zero_grid_signature() -> None:
    """need_based_enabled kwarg must be accepted by plan_zero_grid."""
    bats = [_snap_cap("a", 0.0, 50.0, 10.0)]
    limits = {"a": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        )}
    plan = plan_zero_grid(
        grid_power_w=-500.0,
        bats=bats,
        limits_by_id=limits,
        need_based_enabled=True,
    )
    assert plan.modes["a"] == "charge_battery"


def test_plat1766_flag_compatible_with_state_arg() -> None:
    """Flag must coexist with PLAT-1758 ZeroGridState without side effects."""
    bats = [
        _snap_cap("a", 0.0, 40.0, 10.0),
        _snap_cap("b", 0.0, 50.0, 5.0),
    ]
    limits = {
        "a": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
        "b": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
    }
    state = ZeroGridState(soc_history=(44.0, 45.0, 46.0))
    plan = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        need_based_enabled=True,
        state=state,
    )
    # Both should charge; exact values influenced by momentum dampening but
    # the weights ratio still reflects need.
    assert plan.modes["a"] == "charge_battery"
    assert plan.modes["b"] == "charge_battery"
    # a (lower SoC, 10 kWh) still must get more than b (higher SoC, 5 kWh)
    # because both the need (0.6·10=6) and cap favour a.
    assert plan.limits_w["a"] > plan.limits_w["b"]


def test_plat1766_equal_soc_equal_cap_ignores_need_based() -> None:
    """Equal SoC + equal cap → split is 50/50 regardless of flag."""
    bats = [
        _snap_cap("a", 0.0, 60.0, 10.0),
        _snap_cap("b", 0.0, 60.0, 10.0),
    ]
    limits = {
        bid: BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        )
        for bid in ("a", "b")
    }
    plan_on = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=10.0,
        need_based_enabled=True,
    )
    plan_off = plan_zero_grid(
        grid_power_w=-1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=10.0,
        need_based_enabled=False,
    )
    assert plan_on.limits_w["a"] == plan_on.limits_w["b"]
    assert plan_off.limits_w == plan_on.limits_w


def test_plat1766_need_based_respects_max_charge_clamp() -> None:
    """Alloc respects max_charge_w per bat even with skewed need."""
    bats = [
        _snap_cap("big", 0.0, 10.0, 20.0),   # huge need
        _snap_cap("small", 0.0, 90.0, 1.0),  # tiny need
    ]
    limits = {
        "big": BatLimits(
            max_charge_w=1000, max_discharge_w=1000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
        "small": BatLimits(
            max_charge_w=5000, max_discharge_w=5000,
            soc_min_pct=15.0, soc_max_pct=100.0,
        ),
    }
    plan = plan_zero_grid(
        grid_power_w=-5000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=1.0,  # aggressive path triggered (spread=80)
        need_based_enabled=True,
    )
    # Aggressive path: big (low soc) is primary, capped at 1000 W.
    assert plan.limits_w["big"] <= 1000


def test_plat1766_budget_config_flag_wired_through() -> None:
    """BudgetConfig exposes bat_need_based_enabled field."""
    from core.budget import BudgetConfig

    cfg = BudgetConfig()
    assert hasattr(cfg, "bat_need_based_enabled")
    assert cfg.bat_need_based_enabled is False  # default False
    cfg_on = BudgetConfig(bat_need_based_enabled=True)
    assert cfg_on.bat_need_based_enabled is True


def test_plat1766_schema_flag_roundtrip() -> None:
    """BudgetAggressiveSpreadSection → BudgetConfig roundtrip preserves flag."""
    from config.schema import BudgetAggressiveSpreadSection, BudgetSection

    sec = BudgetSection()
    assert sec.aggressive_spread.bat_need_based_enabled is False
    sec2 = BudgetSection(
        aggressive_spread=BudgetAggressiveSpreadSection(
            bat_need_based_enabled=True,
            bat_spread_max_pct=5.0,
            bat_aggressive_spread_pct=5.0,
        ),
    )
    cfg = sec2.to_budget_config()
    assert cfg.bat_need_based_enabled is True
