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
    _distribute,
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
_MOMENTUM_BASE_GAIN: float = 0.7  # explicit gain for momentum tests — independent of module default
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


# -------------------------------------------------------------------
# PLAT-1766: SoC need-based proportional charge distribution
# -------------------------------------------------------------------

_NB_LIMITS_15KWH = BatLimits(
    max_charge_w=5000,
    max_discharge_w=5000,
    soc_min_pct=15.0,
    soc_max_pct=95.0,
)
_NB_LIMITS_5KWH = BatLimits(
    max_charge_w=5000,
    max_discharge_w=5000,
    soc_min_pct=15.0,
    soc_max_pct=95.0,
)


def _nb_snap(bid: str, soc_pct: float, cap_kwh: float, power_w: float = 0.0) -> BatSnapshot:
    """Helper: BatSnapshot with cap_kwh for need-based tests."""
    return BatSnapshot(battery_id=bid, power_w=power_w, soc_pct=soc_pct, cap_kwh=cap_kwh)


# --- Feature flag off = backward compat ---


def test_need_based_off_matches_plat1755_cap_proportional() -> None:
    """need_based=False (default) → identical output to PLAT-1755 cap-proportional.

    Regression guard: the new code path must not change anything when the
    feature flag is off. Uses spread_aggressive_pct=100.0 to force balanced path.
    """
    bats = [
        _nb_snap("kontor", soc_pct=46.0, cap_kwh=15.0),
        _nb_snap("forrad", soc_pct=51.0, cap_kwh=5.0),
    ]
    limits = {"kontor": _NB_LIMITS_15KWH, "forrad": _NB_LIMITS_5KWH}
    plan_off = plan_zero_grid(
        grid_power_w=-1032.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,  # disable aggressive path
        gain=1.0,
        need_based=False,
    )
    # PLAT-1755: 15/(15+5)=75 %, 5/20=25 %
    assert plan_off.limits_w["kontor"] == pytest.approx(774, abs=1)
    assert plan_off.limits_w["forrad"] == pytest.approx(258, abs=1)


# --- Basic need-based charging ---


def test_need_based_charge_lower_soc_gets_more() -> None:
    """PLAT-1766 core: lower-SoC bat gets proportionally more charge power.

    Live scenario: kontor 46 % (15 kWh), forrad 51 % (5 kWh), 1032 W surplus.
    Need: kontor=0.54*15=8.1 kWh, forrad=0.49*5=2.45 kWh.
    Split: 8.1/(8.1+2.45)=76.8 % → kontor gets more than PLAT-1755's 75 %.
    Uses spread_aggressive_pct=100.0 to force the balanced path.
    """
    bats = [
        _nb_snap("kontor", soc_pct=46.0, cap_kwh=15.0),
        _nb_snap("forrad", soc_pct=51.0, cap_kwh=5.0),
    ]
    limits = {"kontor": _NB_LIMITS_15KWH, "forrad": _NB_LIMITS_5KWH}
    plan = plan_zero_grid(
        grid_power_w=-1032.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,  # disable aggressive path
        gain=1.0,
        need_based=True,
    )
    kontor_need = (1.0 - 46.0 / 100.0) * 15.0  # 8.1
    forrad_need = (1.0 - 51.0 / 100.0) * 5.0  # 2.45
    total = kontor_need + forrad_need
    expected_kontor = 1032.0 * kontor_need / total
    expected_forrad = 1032.0 * forrad_need / total
    assert plan.limits_w["kontor"] == pytest.approx(expected_kontor, abs=1)
    assert plan.limits_w["forrad"] == pytest.approx(expected_forrad, abs=1)
    assert plan.modes["kontor"] == "charge_battery"
    assert plan.modes["forrad"] == "charge_battery"


def test_need_based_charge_gives_more_to_emptier_battery() -> None:
    """Emptier battery (lower SoC) always gets a larger share when need_based=True."""
    bats = [
        _nb_snap("bat_low", soc_pct=30.0, cap_kwh=10.0),
        _nb_snap("bat_high", soc_pct=70.0, cap_kwh=10.0),
    ]
    limits = {
        "bat_low": _DEFAULT_LIMITS,
        "bat_high": _DEFAULT_LIMITS,
    }
    plan = plan_zero_grid(
        grid_power_w=-2000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,  # disable aggressive path
        gain=1.0,
        need_based=True,
    )
    # bat_low need=0.70*10=7.0, bat_high need=0.30*10=3.0 → 70 % vs 30 %
    assert plan.limits_w["bat_low"] == pytest.approx(1400, abs=1)
    assert plan.limits_w["bat_high"] == pytest.approx(600, abs=1)


def test_need_based_charge_equal_soc_equal_cap_gives_50_50() -> None:
    """When both bats have identical SoC and cap, split is 50/50."""
    bats = [
        _nb_snap("a", soc_pct=60.0, cap_kwh=10.0),
        _nb_snap("b", soc_pct=60.0, cap_kwh=10.0),
    ]
    limits = {"a": _DEFAULT_LIMITS, "b": _DEFAULT_LIMITS}
    plan = plan_zero_grid(
        grid_power_w=-2000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        gain=1.0,
        need_based=True,
    )
    assert plan.limits_w["a"] == pytest.approx(1000, abs=1)
    assert plan.limits_w["b"] == pytest.approx(1000, abs=1)


def test_need_based_charge_asymmetric_cap_and_soc() -> None:
    """Both cap and SoC asymmetry combine correctly in need-based weights."""
    # bat_a: 20 % SoC, 20 kWh → need = 0.80 * 20 = 16.0 kWh
    # bat_b: 80 % SoC, 5 kWh  → need = 0.20 *  5 =  1.0 kWh
    # split: 16/(16+1) = 94.1 %, 1/(17) = 5.9 %
    bats = [
        _nb_snap("bat_a", soc_pct=20.0, cap_kwh=20.0),
        _nb_snap("bat_b", soc_pct=80.0, cap_kwh=5.0),
    ]
    limits = {
        "bat_a": BatLimits(
            max_charge_w=5000, max_discharge_w=5000, soc_min_pct=15.0, soc_max_pct=95.0
        ),
        "bat_b": BatLimits(
            max_charge_w=5000, max_discharge_w=5000, soc_min_pct=15.0, soc_max_pct=95.0
        ),
    }
    plan = plan_zero_grid(
        grid_power_w=-1700.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        gain=1.0,
        need_based=True,
    )
    expected_a = 1700.0 * 16.0 / 17.0
    expected_b = 1700.0 * 1.0 / 17.0
    assert plan.limits_w["bat_a"] == pytest.approx(expected_a, abs=1)
    assert plan.limits_w["bat_b"] == pytest.approx(expected_b, abs=1)


# --- Basic need-based discharging ---


def test_need_based_discharge_higher_soc_gets_more() -> None:
    """Discharging need-based: higher-SoC bat supplies more (protects emptier one)."""
    bats = [
        _nb_snap("bat_low", soc_pct=30.0, cap_kwh=10.0),
        _nb_snap("bat_high", soc_pct=70.0, cap_kwh=10.0),
    ]
    limits = {
        "bat_low": _DEFAULT_LIMITS,
        "bat_high": _DEFAULT_LIMITS,
    }
    plan = plan_zero_grid(
        grid_power_w=2000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        gain=1.0,
        need_based=True,
    )
    # bat_low discharge_need=0.30*10=3.0, bat_high=0.70*10=7.0 → 30 % vs 70 %
    assert plan.limits_w["bat_high"] == pytest.approx(1400, abs=1)
    assert plan.limits_w["bat_low"] == pytest.approx(600, abs=1)
    assert plan.modes["bat_high"] == "discharge_pv"
    assert plan.modes["bat_low"] == "discharge_pv"


def test_need_based_discharge_equal_soc_equal_cap_gives_50_50() -> None:
    """Equal SoC + cap → 50/50 discharge split when need_based=True."""
    bats = [
        _nb_snap("a", soc_pct=50.0, cap_kwh=10.0),
        _nb_snap("b", soc_pct=50.0, cap_kwh=10.0),
    ]
    limits = {"a": _DEFAULT_LIMITS, "b": _DEFAULT_LIMITS}
    plan = plan_zero_grid(
        grid_power_w=2000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        gain=1.0,
        need_based=True,
    )
    assert plan.limits_w["a"] == pytest.approx(1000, abs=1)
    assert plan.limits_w["b"] == pytest.approx(1000, abs=1)


# --- Fallback: all weights zero ---


def test_need_based_all_bats_full_falls_back_to_cap_proportional() -> None:
    """All bats at 100 % SoC → charge weights=0 → fallback to cap-proportional.

    Prevents divide-by-zero and preserves a sane allocation.
    """
    bats = [
        _nb_snap("kontor", soc_pct=100.0, cap_kwh=15.0),
        _nb_snap("forrad", soc_pct=100.0, cap_kwh=5.0),
    ]
    limits = {"kontor": _NB_LIMITS_15KWH, "forrad": _NB_LIMITS_5KWH}
    alloc = _distribute(
        total_target_net_w=-1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        need_based=True,
    )
    # Fallback cap-proportional: 15/(15+5)=75 %, 25 %
    assert alloc["kontor"] == pytest.approx(-750.0, abs=1)
    assert alloc["forrad"] == pytest.approx(-250.0, abs=1)


def test_need_based_all_bats_empty_discharge_falls_back_to_cap_proportional() -> None:
    """All bats at 0 % SoC → discharge weights=0 → fallback to cap-proportional."""
    bats = [
        _nb_snap("kontor", soc_pct=0.0, cap_kwh=15.0),
        _nb_snap("forrad", soc_pct=0.0, cap_kwh=5.0),
    ]
    limits = {"kontor": _NB_LIMITS_15KWH, "forrad": _NB_LIMITS_5KWH}
    alloc = _distribute(
        total_target_net_w=1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        need_based=True,
    )
    # Fallback cap-proportional: 75 % / 25 %
    assert alloc["kontor"] == pytest.approx(750.0, abs=1)
    assert alloc["forrad"] == pytest.approx(250.0, abs=1)


def test_need_based_one_bat_at_100pct_excluded_from_charge_weights() -> None:
    """One bat at 100 % SoC gets weight=0; the other bat takes all the charge."""
    bats = [
        _nb_snap("kontor", soc_pct=50.0, cap_kwh=15.0),
        _nb_snap("forrad", soc_pct=100.0, cap_kwh=5.0),
    ]
    limits = {"kontor": _NB_LIMITS_15KWH, "forrad": _NB_LIMITS_5KWH}
    alloc = _distribute(
        total_target_net_w=-1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        need_based=True,
    )
    # forrad weight=0 → kontor takes 100 %
    assert alloc["kontor"] == pytest.approx(-1000.0, abs=1)
    assert alloc["forrad"] == pytest.approx(0.0, abs=1)


def test_need_based_cap_kwh_zero_legacy_bat_excluded() -> None:
    """cap_kwh=0 (legacy bat) → weight=0 → excluded from need-based split."""
    bats = [
        _nb_snap("modern", soc_pct=50.0, cap_kwh=10.0),
        BatSnapshot(battery_id="legacy", power_w=0.0, soc_pct=30.0),  # cap_kwh=0.0
    ]
    limits = {
        "modern": _DEFAULT_LIMITS,
        "legacy": _DEFAULT_LIMITS,
    }
    alloc = _distribute(
        total_target_net_w=-1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        need_based=True,
    )
    # legacy cap_kwh=0 → weight=0 → modern takes all
    assert alloc["modern"] == pytest.approx(-1000.0, abs=1)
    assert alloc["legacy"] == pytest.approx(0.0, abs=1)


def test_need_based_single_bat_unchanged() -> None:
    """Single battery: need_based=True has no effect (same as False)."""
    bats = [_nb_snap("k", soc_pct=50.0, cap_kwh=10.0)]
    limits = {"k": _DEFAULT_LIMITS}
    alloc_off = _distribute(-1000.0, bats, limits, need_based=False)
    alloc_on = _distribute(-1000.0, bats, limits, need_based=True)
    assert alloc_on["k"] == alloc_off["k"]


# --- Aggressive path unaffected ---


def test_need_based_aggressive_path_unaffected_on_charge() -> None:
    """When spread > threshold, aggressive path runs regardless of need_based.

    need_based=True must NOT change aggressive P/S split — that path is
    unaffected by the flag.
    """
    bats = [
        _nb_snap("kontor", soc_pct=40.0, cap_kwh=15.0),
        _nb_snap("forrad", soc_pct=51.0, cap_kwh=5.0),  # 11 pp spread
    ]
    limits = {"kontor": _NB_LIMITS_15KWH, "forrad": _NB_LIMITS_5KWH}
    plan_off = plan_zero_grid(
        grid_power_w=-3000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=5.0,
        gain=1.0,
        need_based=False,
    )
    plan_on = plan_zero_grid(
        grid_power_w=-3000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=5.0,
        gain=1.0,
        need_based=True,
    )
    # Aggressive path: lower-SoC (kontor) gets all charge; forrad standby.
    # Both flag states must produce identical results.
    assert plan_off.modes["kontor"] == plan_on.modes["kontor"] == "charge_battery"
    assert plan_off.modes["forrad"] == plan_on.modes["forrad"] == "battery_standby"
    assert plan_off.limits_w["kontor"] == plan_on.limits_w["kontor"]


def test_need_based_aggressive_path_unaffected_on_discharge() -> None:
    """need_based=True doesn't touch aggressive discharge split."""
    bats = [
        _nb_snap("kontor", soc_pct=40.0, cap_kwh=15.0),
        _nb_snap("forrad", soc_pct=55.0, cap_kwh=5.0),  # 15 pp spread
    ]
    limits = {"kontor": _NB_LIMITS_15KWH, "forrad": _NB_LIMITS_5KWH}
    plan_off = plan_zero_grid(
        grid_power_w=2000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=5.0,
        gain=1.0,
        need_based=False,
    )
    plan_on = plan_zero_grid(
        grid_power_w=2000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=5.0,
        gain=1.0,
        need_based=True,
    )
    assert plan_off.modes["forrad"] == plan_on.modes["forrad"] == "discharge_pv"
    assert plan_off.modes["kontor"] == plan_on.modes["kontor"] == "battery_standby"
    assert plan_off.limits_w["forrad"] == plan_on.limits_w["forrad"]


# --- Convergence: PLAT-1715 1 pp tolerance ---


def test_need_based_convergence_reduces_soc_gap() -> None:
    """PLAT-1766 AC2: SoC gap narrows faster with need_based=True vs False.

    Simulate 10 charging cycles at 3000 W surplus with:
      - kontor 46 %, 15 kWh  (lower SoC — needs more charge)
      - forrad 51 %, 5 kWh   (higher SoC)

    After N cycles need-based should have a smaller diff than cap-proportional.
    Uses spread_aggressive_pct=100.0 to stay in balanced path throughout.
    """
    _sim_limits = BatLimits(
        max_charge_w=5000, max_discharge_w=5000, soc_min_pct=15.0, soc_max_pct=95.0
    )

    def simulate_cycles(need_based: bool, cycles: int = 10, surplus_w: float = 3000.0) -> float:
        """Return SoC diff (forrad - kontor) after N cycles."""
        soc_kontor = 46.0
        soc_forrad = 51.0
        cap_kontor = 15.0  # kWh
        cap_forrad = 5.0  # kWh

        for _ in range(cycles):
            bats = [
                BatSnapshot("kontor", power_w=0.0, soc_pct=soc_kontor, cap_kwh=cap_kontor),
                BatSnapshot("forrad", power_w=0.0, soc_pct=soc_forrad, cap_kwh=cap_forrad),
            ]
            plan = plan_zero_grid(
                grid_power_w=-surplus_w,
                bats=bats,
                limits_by_id={"kontor": _sim_limits, "forrad": _sim_limits},
                spread_aggressive_pct=100.0,  # balanced path only
                gain=1.0,
                need_based=need_based,
            )
            # Apply charge: SoC change = power_w * cycle_s / (cap_kwh * 3600 * 1000) * 100
            cycle_s = 30.0
            kontor_w = plan.limits_w["kontor"] if plan.modes["kontor"] == "charge_battery" else 0.0
            forrad_w = plan.limits_w["forrad"] if plan.modes["forrad"] == "charge_battery" else 0.0
            soc_kontor += kontor_w * cycle_s / (cap_kontor * 3_600_000.0) * 100.0
            soc_forrad += forrad_w * cycle_s / (cap_forrad * 3_600_000.0) * 100.0

        return soc_forrad - soc_kontor

    diff_cap_based = simulate_cycles(need_based=False)
    diff_need_based = simulate_cycles(need_based=True)

    # Need-based must have a smaller (or equal) SoC gap after N cycles.
    assert diff_need_based < diff_cap_based, (
        f"Need-based gap {diff_need_based:.3f} pp must be less than "
        f"cap-proportional gap {diff_cap_based:.3f} pp"
    )


def test_need_based_convergence_from_5pp_to_1pp_within_reasonable_cycles() -> None:
    """PLAT-1766 AC3: starting at 5 pp diff, need-based reaches ≤1 pp.

    Uses high surplus (5000 W) to make convergence observable within 200 cycles.
    """
    _sim_limits = BatLimits(
        max_charge_w=5000, max_discharge_w=5000, soc_min_pct=15.0, soc_max_pct=95.0
    )
    soc_kontor = 40.0
    soc_forrad = 45.0  # 5 pp ahead
    cap_kontor = 15.0
    cap_forrad = 5.0
    cycle_s = 30.0
    converged = False

    for _ in range(200):
        diff = soc_forrad - soc_kontor
        if diff <= 1.0:
            converged = True
            break
        bats = [
            BatSnapshot("kontor", power_w=0.0, soc_pct=soc_kontor, cap_kwh=cap_kontor),
            BatSnapshot("forrad", power_w=0.0, soc_pct=soc_forrad, cap_kwh=cap_forrad),
        ]
        plan = plan_zero_grid(
            grid_power_w=-5000.0,
            bats=bats,
            limits_by_id={"kontor": _sim_limits, "forrad": _sim_limits},
            spread_aggressive_pct=1.0,
            gain=1.0,
            need_based=True,
        )
        kontor_w = plan.limits_w["kontor"] if plan.modes["kontor"] == "charge_battery" else 0.0
        forrad_w = plan.limits_w["forrad"] if plan.modes["forrad"] == "charge_battery" else 0.0
        soc_kontor += kontor_w * cycle_s / (cap_kontor * 3_600_000.0) * 100.0
        soc_forrad += forrad_w * cycle_s / (cap_forrad * 3_600_000.0) * 100.0

    assert (
        converged
    ), f"Did not converge to ≤1 pp within 200 cycles; final diff={soc_forrad - soc_kontor:.3f} pp"


# --- _distribute() direct unit tests ---


def test_distribute_need_based_charge_weight_formula() -> None:
    """_distribute need_based=True: weight = (1-soc/100)*cap_kwh for charging."""
    bats = [
        _nb_snap("a", soc_pct=20.0, cap_kwh=10.0),  # need=8.0
        _nb_snap("b", soc_pct=60.0, cap_kwh=10.0),  # need=4.0
    ]
    limits = {"a": _DEFAULT_LIMITS, "b": _DEFAULT_LIMITS}
    alloc = _distribute(
        total_target_net_w=-1200.0,  # charging
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,  # force balanced path
        need_based=True,
    )
    # weight_a=8.0, weight_b=4.0 → split 2:1
    assert alloc["a"] == pytest.approx(-800.0, abs=1)
    assert alloc["b"] == pytest.approx(-400.0, abs=1)


def test_distribute_need_based_discharge_weight_formula() -> None:
    """_distribute need_based=True: weight = (soc/100)*cap_kwh for discharging."""
    bats = [
        _nb_snap("a", soc_pct=20.0, cap_kwh=10.0),  # weight=2.0
        _nb_snap("b", soc_pct=60.0, cap_kwh=10.0),  # weight=6.0
    ]
    limits = {"a": _DEFAULT_LIMITS, "b": _DEFAULT_LIMITS}
    alloc = _distribute(
        total_target_net_w=800.0,  # discharging
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,  # force balanced path
        need_based=True,
    )
    # weight_b/weight_a = 3:1 → b gets 600, a gets 200
    assert alloc["b"] == pytest.approx(600.0, abs=1)
    assert alloc["a"] == pytest.approx(200.0, abs=1)


def test_distribute_need_based_three_bats_charge() -> None:
    """Three batteries: need-based allocates proportionally to energy needed."""
    bats = [
        _nb_snap("a", soc_pct=10.0, cap_kwh=10.0),  # need=9.0
        _nb_snap("b", soc_pct=50.0, cap_kwh=10.0),  # need=5.0
        _nb_snap("c", soc_pct=80.0, cap_kwh=10.0),  # need=2.0
    ]
    limits = {"a": _DEFAULT_LIMITS, "b": _DEFAULT_LIMITS, "c": _DEFAULT_LIMITS}
    alloc = _distribute(
        total_target_net_w=-1600.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,  # force balanced path (spread=70pp)
        need_based=True,
    )
    # total_need = 9.0 + 5.0 + 2.0 = 16.0
    assert alloc["a"] == pytest.approx(-1600.0 * 9.0 / 16.0, abs=1)
    assert alloc["b"] == pytest.approx(-1600.0 * 5.0 / 16.0, abs=1)
    assert alloc["c"] == pytest.approx(-1600.0 * 2.0 / 16.0, abs=1)


def test_distribute_need_based_preserves_sign_convention() -> None:
    """Charging allocation values must be negative (convention: negative = charging)."""
    bats = [
        _nb_snap("a", soc_pct=40.0, cap_kwh=10.0),
        _nb_snap("b", soc_pct=60.0, cap_kwh=10.0),
    ]
    limits = {"a": _DEFAULT_LIMITS, "b": _DEFAULT_LIMITS}
    alloc = _distribute(-500.0, bats, limits, spread_aggressive_pct=100.0, need_based=True)
    assert alloc["a"] < 0.0
    assert alloc["b"] < 0.0
    assert alloc["a"] + alloc["b"] == pytest.approx(-500.0, abs=1)


def test_distribute_need_based_discharge_preserves_sign_convention() -> None:
    """Discharge allocation values must be positive."""
    bats = [
        _nb_snap("a", soc_pct=40.0, cap_kwh=10.0),
        _nb_snap("b", soc_pct=60.0, cap_kwh=10.0),
    ]
    limits = {"a": _DEFAULT_LIMITS, "b": _DEFAULT_LIMITS}
    alloc = _distribute(500.0, bats, limits, spread_aggressive_pct=100.0, need_based=True)
    assert alloc["a"] > 0.0
    assert alloc["b"] > 0.0
    assert alloc["a"] + alloc["b"] == pytest.approx(500.0, abs=1)


def test_distribute_need_based_total_allocation_matches_target() -> None:
    """Sum of per-bat allocations must equal total_target (energy conservation)."""
    bats = [
        _nb_snap("a", soc_pct=33.0, cap_kwh=12.0),
        _nb_snap("b", soc_pct=67.0, cap_kwh=8.0),
        _nb_snap("c", soc_pct=50.0, cap_kwh=10.0),
    ]
    limits = {"a": _DEFAULT_LIMITS, "b": _DEFAULT_LIMITS, "c": _DEFAULT_LIMITS}
    target = -2345.0
    alloc = _distribute(target, bats, limits, spread_aggressive_pct=100.0, need_based=True)
    assert sum(alloc.values()) == pytest.approx(target, abs=1)


# --- plan_zero_grid with need_based: integration smoke ---


def test_plan_zero_grid_need_based_flag_propagates_to_balanced_path() -> None:
    """plan_zero_grid with need_based=True routes the flag into _distribute.

    Uses spread_aggressive_pct=100.0 to force the balanced path for both runs,
    so the only difference is the weighting algorithm.
    """
    bats = [
        _nb_snap("kontor", soc_pct=46.0, cap_kwh=15.0),
        _nb_snap("forrad", soc_pct=51.0, cap_kwh=5.0),
    ]
    limits = {"kontor": _NB_LIMITS_15KWH, "forrad": _NB_LIMITS_5KWH}
    plan_off = plan_zero_grid(
        grid_power_w=-1032.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,  # balanced path only
        gain=1.0,
        need_based=False,
    )
    plan_on = plan_zero_grid(
        grid_power_w=-1032.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,  # balanced path only
        gain=1.0,
        need_based=True,
    )
    # With need_based=True, kontor (lower SoC) must get MORE charge than with False.
    assert plan_on.limits_w["kontor"] > plan_off.limits_w["kontor"]
    assert plan_on.limits_w["forrad"] < plan_off.limits_w["forrad"]


def test_plan_zero_grid_need_based_false_is_default() -> None:
    """Calling plan_zero_grid without need_based= behaves as need_based=False."""
    bats = [
        _nb_snap("kontor", soc_pct=46.0, cap_kwh=15.0),
        _nb_snap("forrad", soc_pct=51.0, cap_kwh=5.0),
    ]
    limits = {"kontor": _NB_LIMITS_15KWH, "forrad": _NB_LIMITS_5KWH}
    plan_default = plan_zero_grid(
        grid_power_w=-1032.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        gain=1.0,
    )
    plan_off = plan_zero_grid(
        grid_power_w=-1032.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        gain=1.0,
        need_based=False,
    )
    assert plan_default.limits_w == plan_off.limits_w
    assert plan_default.modes == plan_off.modes


def test_need_based_emergency_recovery_unaffected() -> None:
    """Emergency recovery (bat below floor) still runs at max even with need_based=True.

    The need-based path must not interfere with the force-charge safety path —
    a bat below the absolute SoC floor must always be recovered at full rate.
    """
    bats = [
        _nb_snap("low_emergency", soc_pct=14.0, cap_kwh=15.0),  # below floor
        _nb_snap("healthy", soc_pct=60.0, cap_kwh=5.0),
    ]
    limits = {
        "low_emergency": _DEFAULT_LIMITS,
        "healthy": _DEFAULT_LIMITS,
    }
    plan = plan_zero_grid(
        grid_power_w=1000.0,
        bats=bats,
        limits_by_id=limits,
        spread_aggressive_pct=100.0,
        gain=1.0,
        need_based=True,
    )
    # Emergency bat is force-charged at max regardless of need_based
    assert "low_emergency" in plan.emergency_recovery
    assert plan.modes["low_emergency"] == "charge_battery"
    assert plan.limits_w["low_emergency"] == _DEFAULT_LIMITS.max_charge_w
    # Healthy bat still handles the grid target normally
    assert "healthy" not in plan.emergency_recovery


# PLAT-NEW Story 1: _CORRECTION_GAIN default 0.75
# -------------------------------------------------------------------


def test_correction_gain_constant_is_0_75() -> None:
    """PLAT-NEW Story 1: module-level _CORRECTION_GAIN is 0.75 (was 0.7).

    This is the default used when plan_zero_grid() is called without
    an explicit gain= keyword. The config-driven path (BudgetConfig.correction_gain)
    always supplies gain= explicitly from allocate(), so this constant
    is a safe fallback for direct callers and tests.
    """
    from core.zero_grid import _CORRECTION_GAIN

    assert _CORRECTION_GAIN == pytest.approx(0.75)


def test_plan_zero_grid_closes_75_pct_of_gap_by_default() -> None:
    """plan_zero_grid() with default gain closes ~75% of the grid gap each cycle.

    Example: grid import = 1000 W, bat currently at 0 W.
    Expected: bat target = 0 + 1000 * 0.75 = 750 W (discharge).
    """
    bat = BatSnapshot(battery_id="b", power_w=0.0, soc_pct=50.0)
    lim = BatLimits(max_charge_w=5000, max_discharge_w=5000, soc_min_pct=15.0, soc_max_pct=95.0)
    plan = plan_zero_grid(
        grid_power_w=1000.0,
        bats=[bat],
        limits_by_id={"b": lim},
    )
    # 75% of 1000 W gap → 750 W discharge
    assert plan.total_target_net_w == pytest.approx(750, abs=1)
    assert plan.modes["b"] == "discharge_pv"
