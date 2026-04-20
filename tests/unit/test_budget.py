"""Unit tests for core.budget — Unified Power Budget Allocator (PLAT-1686).

Per 901 AC:
  - AC1: EN allocator per cycle
  - AC2: sum(allocations) <= surplus
  - AC3: inga parallella dispatch
  - AC4: immutable per cycle
"""

from __future__ import annotations

from datetime import datetime

import pytest

from core.budget import (
    BudgetState,
    BudgetConfig,
    BudgetInput,
    _available_surplus_w,
    _is_daytime,
    _is_fm,
    allocate,
)
from core.models import CommandType


def _inp(
    *,
    hour: int = 10,
    grid_w: float = 0.0,
    pv_w: float = 3000.0,
    house_w: float = 500.0,
    ev_connected: bool = False,
    ev_charging: bool = False,
    ev_amps: int = 0,
    ev_soc: float = 50.0,
    ev_target: float = 100.0,
    bat_k_soc: float = 70.0,
    bat_f_soc: float = 70.0,
    bat_k_mode: str = "charge_pv",
    bat_f_mode: str = "charge_pv",
) -> BudgetInput:
    """Default bat_modes set to 'charge_pv' (not target 'charge_battery') so
    transition-testing triggers the SET_EMS_MODE emission path (PLAT-1715
    idempotency: mode is only emitted when current differs from target).
    """
    return BudgetInput(
        now=datetime(2026, 4, 17, hour, 0, 0),
        grid_power_w=grid_w,
        pv_power_w=pv_w,
        house_load_w=house_w,
        ev_connected=ev_connected,
        ev_charging=ev_charging,
        ev_current_amps=ev_amps,
        ev_soc_pct=ev_soc,
        ev_target_soc_pct=ev_target,
        bat_socs={"kontor": bat_k_soc, "forrad": bat_f_soc},
        bat_caps={"kontor": 15.0, "forrad": 5.0},
        bat_powers={"kontor": 0.0, "forrad": 0.0},
        bat_modes={"kontor": bat_k_mode, "forrad": bat_f_mode},
    )


@pytest.fixture
def cfg() -> BudgetConfig:
    return BudgetConfig()


# -------------------------------------------------------------------
# Time helpers
# -------------------------------------------------------------------


def test_is_daytime() -> None:
    assert _is_daytime(6) is True
    assert _is_daytime(12) is True
    assert _is_daytime(21) is True
    assert _is_daytime(22) is False
    assert _is_daytime(5) is False


def test_is_fm() -> None:
    assert _is_fm(6) is True
    assert _is_fm(11) is True
    assert _is_fm(12) is False
    assert _is_fm(5) is False


# -------------------------------------------------------------------
# Surplus calculation
# -------------------------------------------------------------------


def test_surplus_daytime_never_negative() -> None:
    """DAG: surplus = max(0, PV - house). ALDRIG negativ."""
    inp = _inp(hour=10, pv_w=500, house_w=2000)
    assert _available_surplus_w(inp) == 0.0


def test_surplus_daytime_positive() -> None:
    inp = _inp(hour=10, pv_w=3000, house_w=500)
    assert _available_surplus_w(inp) == 2500.0


def test_surplus_night_can_be_negative() -> None:
    inp = _inp(hour=23, pv_w=0, house_w=500)
    assert _available_surplus_w(inp) == -500.0


# -------------------------------------------------------------------
# AC2: sum(allocations) <= surplus
# -------------------------------------------------------------------


def test_allocations_never_exceed_surplus(cfg: BudgetConfig) -> None:
    inp = _inp(pv_w=2000, house_w=500)
    state = BudgetState()
    result = allocate(inp, cfg, state)
    total = sum(result.bat_allocations.values())
    surplus = 2000 - 500
    assert total <= surplus


# -------------------------------------------------------------------
# FM priority: EV → bat
# -------------------------------------------------------------------


def test_fm_ev_priority_when_connected(cfg: BudgetConfig) -> None:
    """FM + EV connected → EV gets allocation."""
    inp = _inp(
        hour=9,
        pv_w=6000,
        house_w=500,
        ev_connected=True,
        ev_soc=50,
        grid_w=-2000,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps > 0


def test_fm_ev_no_start_without_surplus(cfg: BudgetConfig) -> None:
    """FM + EV connected but no surplus → EV stays off."""
    inp = _inp(
        hour=9,
        pv_w=500,
        house_w=500,
        ev_connected=True,
        ev_soc=50,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 0


# -------------------------------------------------------------------
# EM priority: bat → EV
# -------------------------------------------------------------------


def test_em_bat_priority(cfg: BudgetConfig) -> None:
    """EM + bat < 100% → zero-grid absorbs exported surplus, EV off.

    Fixture sets grid_w to reflect the physics (PV 3 kW, house 0.5 kW,
    bat 0 → grid ≈ -2.5 kW export). Zero-grid sends that to the bat.
    """
    inp = _inp(
        hour=14,
        pv_w=3000,
        house_w=500,
        grid_w=-2500,
        ev_connected=True,
        bat_k_soc=80,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 0
    assert sum(result.bat_allocations.values()) > 0


def test_em_ev_after_bat_full(cfg: BudgetConfig) -> None:
    """EM + bat 100% + EV connected → EV gets surplus (ramp-up path)."""
    inp = _inp(
        hour=14,
        pv_w=6000,
        house_w=500,
        ev_connected=True,
        ev_charging=True,
        ev_amps=6,
        ev_soc=50,
        grid_w=-2000,
        bat_k_soc=100,
        bat_f_soc=100,
    )
    # Two consecutive export cycles unlock the ramp-up
    state = BudgetState(consecutive_export_cycles=2, ev_current_amps=6)
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps > 0


# -------------------------------------------------------------------
# EV ramp ±1A
# -------------------------------------------------------------------


def test_ev_ramp_up_on_export(cfg: BudgetConfig) -> None:
    """Grid exporting 2 consecutive cycles → ramp +1A (tröghet)."""
    inp = _inp(
        hour=9,
        pv_w=6000,
        house_w=500,
        ev_connected=True,
        ev_charging=True,
        ev_amps=7,
        grid_w=-500,
    )
    state = BudgetState(consecutive_export_cycles=2, ev_current_amps=7)
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 8


def test_ev_ramp_down_on_import(cfg: BudgetConfig) -> None:
    """Grid importing 1 cycle → ramp -1A (snabb ner)."""
    inp = _inp(
        hour=9,
        pv_w=6000,
        house_w=500,
        ev_connected=True,
        ev_charging=True,
        ev_amps=9,
        grid_w=500,
    )
    state = BudgetState(consecutive_import_cycles=1, ev_current_amps=9)
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 8


def test_ev_hold_in_band(cfg: BudgetConfig) -> None:
    """Grid within ±100W → hold."""
    inp = _inp(
        hour=9,
        pv_w=6000,
        house_w=500,
        ev_connected=True,
        ev_charging=True,
        ev_amps=8,
        grid_w=50,
    )
    state = BudgetState(ev_current_amps=8)
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 8


def test_ev_max_capped(cfg: BudgetConfig) -> None:
    """At max amps (16) → no ramp up."""
    inp = _inp(
        hour=9,
        pv_w=15000,
        house_w=500,
        ev_connected=True,
        ev_charging=True,
        ev_amps=16,
        grid_w=-2000,
    )
    state = BudgetState(consecutive_export_cycles=3, ev_current_amps=16)
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 16


def test_ev_min_floored(cfg: BudgetConfig) -> None:
    """At min amps + import → stays at min (not 0, but stops if no surplus)."""
    inp = _inp(
        hour=9,
        pv_w=6000,
        house_w=500,
        ev_connected=True,
        ev_charging=True,
        ev_amps=6,
        grid_w=500,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    # At min amps with import → ramp down but floored at min
    assert result.ev_target_amps == 6  # can't go below min


# -------------------------------------------------------------------
# Bat balance: spread ≤ 1pp
# -------------------------------------------------------------------


def test_bat_balanced_proportional(cfg: BudgetConfig) -> None:
    """Same SoC, measured surplus (export) → proportional by capacity.

    With the zero-grid controller the grid reading drives allocation, so
    the fixture must reflect the physics: PV 3 kW, house 0.5 kW, bat 0 →
    grid ≈ -2.5 kW export. Expected split = 3:1 by max_charge_w
    (both bats share the default 5 kW cap → 1:1) — assert k > 0 and
    forrad also > 0.
    """
    inp = _inp(
        pv_w=3000,
        house_w=500,
        grid_w=-2500,
        bat_k_soc=70,
        bat_f_soc=70,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    k = result.bat_allocations["kontor"]
    f = result.bat_allocations["forrad"]
    assert k > 0
    assert f > 0


def test_bat_unbalanced_lower_gets_more(cfg: BudgetConfig) -> None:
    """Large SoC spread (>5 pp) → aggressive split, lower gets 100 %."""
    inp = _inp(
        pv_w=3000,
        house_w=500,
        grid_w=-2500,
        bat_k_soc=85,
        bat_f_soc=95,  # 10 pp spread → aggressive
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    k = result.bat_allocations["kontor"]  # lower
    f = result.bat_allocations["forrad"]  # higher
    assert k > 0
    assert f == 0, "forrad should be standby in aggressive-spread mode"


# -------------------------------------------------------------------
# EVENING_DISCHARGE: bat covers house load, grid target 0W
# -------------------------------------------------------------------

_EVENING_DISCHARGE_HOUR: int = 18
_EVENING_HOUSE_LOAD_W: float = 2000.0
_EVENING_GRID_IMPORT_W: float = 2000.0
_EVENING_BAT_SOC: float = 80.0
_EVENING_BAT_LOW_SOC: float = 15.0  # exactly at min_soc floor


def test_evening_discharge_covers_house_load(cfg: BudgetConfig) -> None:
    """17-20: zero-grid discharges the bat to cover grid import (target 0 W).

    PLAT-1718 owns the evening window — zero_grid produces the same
    net result as the legacy allocator (total discharge ≈ grid import)
    while being closed-loop on the measured bat + grid state.
    """
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR,
        pv_w=0,
        house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=_EVENING_BAT_SOC,
        bat_f_soc=_EVENING_BAT_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    # PLAT-1696 proportional gain 0.7 → cycle closes 70 % of the gap.
    expected = int(_EVENING_GRID_IMPORT_W * 0.7)
    assert result.bat_discharge_w == expected, (
        f"discharge={result.bat_discharge_w}, expected gain-damped "
        f"{expected} (0.7 × {_EVENING_GRID_IMPORT_W})"
    )
    assert "zero_grid" in result.reason


def test_evening_discharge_proportional(cfg: BudgetConfig) -> None:
    """Evening discharge proportional by max_discharge_w (zero-grid).

    Both default bats have the same max_discharge_w → 50/50 split.
    Previously (legacy) used bat_caps-weighted split; zero-grid uses
    inverter rate capacity because that is the real physical constraint.
    """
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR,
        pv_w=0,
        house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=_EVENING_BAT_SOC,
        bat_f_soc=_EVENING_BAT_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    k = result.bat_allocations.get("kontor", 0)
    f = result.bat_allocations.get("forrad", 0)
    # Equal split with default max_discharge_w (5000 each), gain=0.7.
    assert k == f
    assert k + f == int(_EVENING_GRID_IMPORT_W * 0.7)


def test_evening_discharge_discharge_pv_commands(cfg: BudgetConfig) -> None:
    """Evening discharge emits discharge_pv mode + limit commands."""
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR,
        pv_w=0,
        house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=_EVENING_BAT_SOC,
        bat_f_soc=_EVENING_BAT_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    cmd_types = [c.command_type for c in result.commands]
    assert CommandType.SET_EMS_MODE in cmd_types
    assert CommandType.SET_EMS_POWER_LIMIT in cmd_types
    # All mode commands should be discharge_pv
    mode_cmds = [c for c in result.commands if c.command_type == CommandType.SET_EMS_MODE]
    for cmd in mode_cmds:
        assert cmd.value == "discharge_pv"


def test_evening_discharge_no_ev(cfg: BudgetConfig) -> None:
    """Evening discharge: EV always off (preserve bat for peak shaving)."""
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR,
        pv_w=0,
        house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        ev_connected=True,
        ev_soc=50,
        bat_k_soc=_EVENING_BAT_SOC,
        bat_f_soc=_EVENING_BAT_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 0


def test_evening_discharge_skipped_bat_low(cfg: BudgetConfig) -> None:
    """Evening discharge: bat at min_soc → no discharge, falls through to standby."""
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR,
        pv_w=0,
        house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=_EVENING_BAT_LOW_SOC,
        bat_f_soc=_EVENING_BAT_LOW_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    # Should NOT be evening discharge (bat too low)
    assert "EVENING_DISCHARGE" not in result.reason


def test_evening_discharge_bat_full_still_discharges(cfg: BudgetConfig) -> None:
    """Evening discharge: bat at 100% → zero_grid still discharges to grid=0."""
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR,
        pv_w=0,
        house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=100.0,
        bat_f_soc=100.0,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert "zero_grid" in result.reason
    assert result.bat_discharge_w > 0


def test_evening_discharge_grid_responsive(cfg: BudgetConfig) -> None:
    """Grid exporting (negative) → no discharge needed (bat already covers)."""
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR,
        pv_w=0,
        house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=-500.0,  # exporting = bat gives too much
        bat_k_soc=_EVENING_BAT_SOC,
        bat_f_soc=_EVENING_BAT_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    # grid_w=-500 (export) → target = max(0, -500) = 0 → no discharge
    assert result.bat_discharge_w == 0


def test_evening_20h_covers_grid_import(cfg: BudgetConfig) -> None:
    """After 20:00 zero_grid owns the bat 24/7 (user invariant:
    max 100 W import/export). If the grid is importing, the bat MUST
    discharge to close the gap — the legacy "evening standby" behaviour
    leaked multi-kW imports during expensive hours.
    """
    inp = _inp(
        hour=20,
        pv_w=0,
        house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=_EVENING_BAT_SOC,
        bat_f_soc=_EVENING_BAT_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.bat_discharge_w > 0
    assert "zero_grid" in result.reason


# -------------------------------------------------------------------
# HARD RULE: ALDRIG grid import dagtid
# -------------------------------------------------------------------


def test_never_import_daytime(cfg: BudgetConfig) -> None:
    """No PV surplus → no allocation. Grid stays at 0."""
    inp = _inp(hour=14, pv_w=500, house_w=1000, ev_connected=True)
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 0
    assert sum(result.bat_allocations.values()) == 0


# -------------------------------------------------------------------
# Night
# -------------------------------------------------------------------


def test_night_defers(cfg: BudgetConfig) -> None:
    """Night → budget allocator defers (no bat/EV commands)."""
    inp = _inp(hour=23)
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 0
    assert "NIGHT" in result.reason


# -------------------------------------------------------------------
# Commands
# -------------------------------------------------------------------


def test_start_ev_command(cfg: BudgetConfig) -> None:
    """EV not charging + allocation → START command."""
    inp = _inp(
        hour=9,
        pv_w=8000,
        house_w=500,
        ev_connected=True,
        ev_charging=False,
        ev_amps=0,
        grid_w=-4000,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    cmd_types = [c.command_type for c in result.commands]
    if result.ev_target_amps > 0:
        assert CommandType.START_EV_CHARGING in cmd_types


def test_stop_ev_command(cfg: BudgetConfig) -> None:
    """EV charging + no surplus → STOP command.

    Scenario: Budget previously turned EV on (intended_ev_enabled=True,
    ev_current_amps=6). Surplus falls to 0. Budget must emit STOP once.
    PLAT-1740: comparison is against intended state, not HA-reported.
    """
    inp = _inp(
        hour=14,
        pv_w=500,
        house_w=500,
        ev_connected=True,
        ev_charging=True,
        ev_amps=6,
        bat_k_soc=80,
    )
    state = BudgetState(intended_ev_enabled=True, ev_current_amps=6)
    result = allocate(inp, cfg, state)
    cmd_types = [c.command_type for c in result.commands]
    assert CommandType.STOP_EV_CHARGING in cmd_types
    # PLAT-1740: intent flipped to False after emit
    assert state.intended_ev_enabled is False


def test_all_commands_have_rule_id(cfg: BudgetConfig) -> None:
    inp = _inp(pv_w=3000, house_w=500)
    state = BudgetState()
    result = allocate(inp, cfg, state)
    for cmd in result.commands:
        assert cmd.rule_id == "BUDGET"


# -------------------------------------------------------------------
# Guard tests (per 901 reject krav)
# -------------------------------------------------------------------


def test_house_load_formula_sign_convention() -> None:
    """Guard: house = grid + pv - bat_charge - ev. Never grid + bat_charge."""
    # Read engine.py source and verify formula
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "core" / "engine.py").read_text()
    for i, line in enumerate(source.splitlines(), 1):
        if "house_w" in line and "bat_charge_w" in line and "=" in line:
            assert "- bat_charge_w" in line, (
                f"engine.py:{i}: house_load must SUBTRACT bat_charge_w, "
                f"not add it. Line: {line.strip()}"
            )


def test_balance_ratios_are_named_constants() -> None:
    """Guard: no raw 0.8/0.2 in engine.py balance logic."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "core" / "engine.py").read_text()
    for i, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "allocations[" in stripped and ("* 0.8" in stripped or "* 0.2" in stripped):
            assert False, (
                f"engine.py:{i}: Raw 0.8/0.2 in allocation. "
                f"Use _SOC_BALANCE_LOWER/HIGHER_RATIO. Line: {stripped}"
            )


# -------------------------------------------------------------------
# PLAT-1695: Regulator convergence — SoC-gate at bat_charge_stop_soc_pct
# -------------------------------------------------------------------
# Regression for the 5pp dead zone: state machine enters S8 PV_SURPLUS at
# SoC >= surplus_entry_soc_pct (95%) but budget previously kept charging
# up to bat_soc_full_pct (100%). Result: at SoC 95-99% the regulator kept
# adding PV to bat → grid export grew instead of shrinking.
# Fix: filter in _allocate_bat uses bat_charge_stop_soc_pct (default 95).


@pytest.mark.parametrize(
    "soc_k,soc_f,stop_pct,expect_bat_cmds",
    [
        # Both below threshold → charge both
        (90.0, 88.0, 95.0, True),
        # Both at threshold → no charging
        (95.0, 95.0, 95.0, False),
        # Both above threshold → no charging
        (96.0, 97.0, 95.0, False),
        # Mixed (one below, one above) → lower bat still gets allocation
        (94.0, 96.0, 95.0, True),
        # Both at legacy full (100%) → no charging (previous behaviour)
        (100.0, 100.0, 95.0, False),
        # Kund-agnostisk: different threshold (e.g. 90%) — verify config-driven
        (91.0, 92.0, 90.0, False),
        (89.0, 89.0, 90.0, True),
    ],
)
def test_plat1695_bat_charge_stops_at_config_threshold(
    soc_k: float,
    soc_f: float,
    stop_pct: float,
    expect_bat_cmds: bool,
) -> None:
    """PLAT-1695: Stop charging at bat_charge_stop_soc_pct (config-driven).

    Must match state_machine.surplus_entry_soc_pct to eliminate dead zone.
    """
    cfg = BudgetConfig(bat_charge_stop_soc_pct=stop_pct)
    inp = _inp(
        hour=14,  # EM path (bat-prio)
        pv_w=5000.0,  # plenty of PV
        house_w=500.0,
        grid_w=-4000.0,  # exporting
        bat_k_soc=soc_k,
        bat_f_soc=soc_f,
    )

    result = allocate(inp, cfg)

    bat_charge_cmds = [
        c
        for c in result.commands
        if c.command_type == CommandType.SET_EMS_MODE and c.value == "charge_battery"
    ]
    if expect_bat_cmds:
        assert bat_charge_cmds, (
            f"SoC kontor={soc_k}% forrad={soc_f}% stop={stop_pct}%: "
            f"expected at least one bat to charge (lower SoC)"
        )
    else:
        assert not bat_charge_cmds, (
            f"SoC kontor={soc_k}% forrad={soc_f}% stop={stop_pct}%: "
            f"bat should NOT charge above stop threshold. "
            f"Got cmds: {[(c.target_id, c.reason) for c in bat_charge_cmds]}"
        )


def test_plat1695_only_lower_bat_charges_when_mixed() -> None:
    """PLAT-1695: Mixed SoC — only the below-threshold bat gets allocation."""
    cfg = BudgetConfig(bat_charge_stop_soc_pct=95.0)
    inp = _inp(
        hour=14,
        pv_w=5000.0,
        house_w=500.0,
        grid_w=-4000.0,
        bat_k_soc=94.0,  # below threshold
        bat_f_soc=96.0,  # above threshold
    )

    result = allocate(inp, cfg)

    charge_targets = {
        c.target_id
        for c in result.commands
        if c.command_type == CommandType.SET_EMS_MODE and c.value == "charge_battery"
    }
    standby_targets = {
        c.target_id
        for c in result.commands
        if c.command_type == CommandType.SET_EMS_MODE and c.value == "battery_standby"
    }

    assert (
        "kontor" in charge_targets
    ), f"kontor (SoC 94%) should charge. Charge targets: {charge_targets}"
    assert (
        "forrad" in standby_targets
    ), f"forrad (SoC 96%) should be standby. Standby targets: {standby_targets}"


@pytest.mark.parametrize(
    "grid_w,soc_k,soc_f,should_kontor_charge",
    [
        # Exporting → zero-grid sends it to bat → kontor charges.
        (-4000.0, 50.0, 50.0, True),
        (-500.0, 50.0, 50.0, True),
        # Inside deadband (|grid| ≤ 50 W) → bat holds its current state;
        # with bat idle that means standby → no charge command.
        (0.0, 50.0, 50.0, False),
        (40.0, 50.0, 50.0, False),
        # Importing → zero-grid needs bat to discharge/reduce; kontor
        # cannot charge in this state.
        (3000.0, 50.0, 50.0, False),
        (6000.0, 50.0, 50.0, False),
        # SoC at/above stop threshold — charging is blocked regardless.
        (-4000.0, 95.0, 95.0, False),
        (0.0, 95.0, 95.0, False),
        (-4000.0, 98.0, 98.0, False),
    ],
)
def test_plat1695_grid_w_variation(
    grid_w: float,
    soc_k: float,
    soc_f: float,
    should_kontor_charge: bool,
) -> None:
    """PLAT-1695 + PLAT-1718: grid-driven allocation × SoC-gate matrix.

    The zero-grid controller uses measured grid_power_w to decide the
    bat plan. The SoC-gate still blocks charging once the stop threshold
    is reached — see core/zero_grid._clamp_for_soc.
    """
    cfg = BudgetConfig()
    inp = _inp(
        hour=14,
        pv_w=5000.0,
        house_w=500.0,
        grid_w=grid_w,
        bat_k_soc=soc_k,
        bat_f_soc=soc_f,
    )
    result = allocate(inp, cfg)
    kontor_cmd = next(
        (
            c
            for c in result.commands
            if c.command_type == CommandType.SET_EMS_MODE and c.target_id == "kontor"
        ),
        None,
    )
    kontor_charges = kontor_cmd is not None and kontor_cmd.value == "charge_battery"
    assert kontor_charges is should_kontor_charge, (
        f"grid={grid_w} soc_k={soc_k} soc_f={soc_f}: "
        f"expected charge={should_kontor_charge}, "
        f"got mode={kontor_cmd.value if kontor_cmd else 'none'}"
    )


def test_plat1708_bat_full_soc100_goes_to_standby() -> None:
    """PLAT-1708: Full battery (SoC=100) → standby mode + limit=0.

    Guards against a regression where the SoC-stop threshold accidentally
    lets 100 % batteries keep charging (mode/limit drift).
    """
    cfg = BudgetConfig()
    inp = _inp(
        hour=14,
        pv_w=5000.0,
        house_w=500.0,
        grid_w=-4000.0,
        bat_k_soc=100.0,
        bat_f_soc=100.0,
    )
    result = allocate(inp, cfg)
    standby_cmds = [
        c
        for c in result.commands
        if c.command_type == CommandType.SET_EMS_MODE and c.value == "battery_standby"
    ]
    limit_cmds = {
        c.target_id: c.value
        for c in result.commands
        if c.command_type == CommandType.SET_EMS_POWER_LIMIT
    }
    assert {c.target_id for c in standby_cmds} >= {
        "kontor",
        "forrad",
    }, "Both full bats must be commanded to battery_standby"
    for bid in ("kontor", "forrad"):
        assert (
            limit_cmds.get(bid) == 0
        ), f"{bid} limit should be 0 when SoC=100, got {limit_cmds.get(bid)}"


def test_plat1695_default_stop_matches_state_machine_s8_entry() -> None:
    """PLAT-1695: Default bat_charge_stop_soc_pct must match state machine
    surplus_entry_soc_pct. Prevents reintroducing the 5pp dead zone."""
    from core.state_machine import StateMachineConfig  # noqa: PLC0415

    cfg = BudgetConfig()
    sm_cfg = StateMachineConfig()

    assert cfg.bat_charge_stop_soc_pct == sm_cfg.surplus_entry_soc_pct, (
        f"BudgetConfig.bat_charge_stop_soc_pct ({cfg.bat_charge_stop_soc_pct}) "
        f"must equal StateMachineConfig.surplus_entry_soc_pct "
        f"({sm_cfg.surplus_entry_soc_pct}) to avoid PV_SURPLUS dead zone."
    )


# -------------------------------------------------------------------
# PLAT-1714: Budget Allocator MUST use charge_battery (mode 11) + limit
# -------------------------------------------------------------------
# Regression for "800W grid import during PV absorption":
# charge_pv + ems_power_limit is UNCONTROLLABLE in peak_shaving firmware
# (GoodWe absorbs from grid too). charge_battery (mode 11) RESPECTS the
# limit and absorbs PV-surplus only, no grid import.


def test_plat1714_bat_charge_uses_charge_battery_not_charge_pv() -> None:
    """PLAT-1714: Budget charge must emit charge_battery (mode 11), not charge_pv."""
    cfg = BudgetConfig()
    inp = _inp(
        hour=14,  # EM
        pv_w=5000.0,
        house_w=500.0,
        grid_w=-4000.0,
        bat_k_soc=50.0,
        bat_f_soc=50.0,
    )

    result = allocate(inp, cfg)
    mode_cmds = [c for c in result.commands if c.command_type == CommandType.SET_EMS_MODE]
    charge_pv_cmds = [c for c in mode_cmds if c.value == "charge_pv"]
    charge_bat_cmds = [c for c in mode_cmds if c.value == "charge_battery"]

    assert not charge_pv_cmds, (
        f"PLAT-1714: Budget must NOT emit charge_pv (uncontrollable in peak_shaving). "
        f"Offending cmds: {[(c.target_id, c.reason) for c in charge_pv_cmds]}"
    )
    assert charge_bat_cmds, "PLAT-1714: Budget must emit charge_battery for PV-surplus absorption"


def test_plat1714_bat_charge_emits_matching_power_limit() -> None:
    """PLAT-1714: charge_battery must be paired with SET_EMS_POWER_LIMIT.

    Without the limit, GoodWe firmware defaults to uncontrolled behaviour.
    """
    cfg = BudgetConfig()
    inp = _inp(
        hour=14,
        pv_w=5000.0,
        house_w=500.0,
        grid_w=-4000.0,
        bat_k_soc=50.0,
        bat_f_soc=50.0,
    )

    result = allocate(inp, cfg)
    # Group mode + limit commands by target_id
    modes = {
        c.target_id: c.value for c in result.commands if c.command_type == CommandType.SET_EMS_MODE
    }
    limits = {
        c.target_id: c.value
        for c in result.commands
        if c.command_type == CommandType.SET_EMS_POWER_LIMIT
    }

    for bid, mode in modes.items():
        if mode == "charge_battery":
            assert bid in limits, (
                f"PLAT-1714: bat {bid} in charge_battery mode but no "
                f"SET_EMS_POWER_LIMIT emitted"
            )
            assert limits[bid] > 0, (
                f"PLAT-1714: bat {bid} charge_battery limit must be > 0, " f"got {limits[bid]}"
            )


def test_plat1714_standby_emits_limit_zero() -> None:
    """PLAT-1714: standby bats should also get SET_EMS_POWER_LIMIT=0 to
    defeat the truthy-trap (B9) and prevent stale limits leaking."""
    cfg = BudgetConfig(bat_charge_stop_soc_pct=95.0)
    inp = _inp(
        hour=14,
        pv_w=5000.0,
        house_w=500.0,
        grid_w=-4000.0,
        bat_k_soc=96.0,  # above threshold → standby
        bat_f_soc=97.0,
    )

    result = allocate(inp, cfg)
    standby_targets = {
        c.target_id
        for c in result.commands
        if c.command_type == CommandType.SET_EMS_MODE and c.value == "battery_standby"
    }
    limit_zero_targets = {
        c.target_id
        for c in result.commands
        if c.command_type == CommandType.SET_EMS_POWER_LIMIT and c.value == 0
    }

    assert standby_targets == limit_zero_targets, (
        f"PLAT-1714: Every standby bat must also emit limit=0. "
        f"standby={standby_targets}, limit_zero={limit_zero_targets}"
    )


# -------------------------------------------------------------------
# PLAT-1715: consumer cascade (unified priority — bat → EV → consumers)
# -------------------------------------------------------------------


from core.models import ConsumerState  # noqa: E402


def _c(cid: str, active: bool, priority: int, priority_shed: int) -> ConsumerState:
    return ConsumerState(
        consumer_id=cid,
        name=cid,
        active=active,
        power_w=400.0 if active else 0.0,
        priority=priority,
        priority_shed=priority_shed,
        load_type="on_off",
    )


def test_plat1715_cascade_turn_on_lowest_priority_on_sustained_export() -> None:
    """grid exporting > 100 W for ≥ cascade_sustained_cycles → turn on
    the LOWEST-priority-number inactive consumer.

    PLAT-1738: bats must also be at charge-stop SoC for cascade to fire.
    """
    cfg = BudgetConfig(
        cascade_cooldown_s=0.0,
        cascade_sustained_cycles=2,
        bat_charge_stop_soc_pct=95.0,
    )
    inp = _inp(
        hour=14,
        pv_w=6000,
        house_w=500,
        grid_w=-1500,
        bat_k_soc=96,
        bat_f_soc=96,  # PLAT-1738: at stop-SoC
    )
    inp_with_consumers = BudgetInput(
        now=inp.now,
        grid_power_w=inp.grid_power_w,
        pv_power_w=inp.pv_power_w,
        house_load_w=inp.house_load_w,
        ev_connected=False,
        ev_charging=False,
        ev_current_amps=0,
        ev_soc_pct=50.0,
        ev_target_soc_pct=100.0,
        bat_socs=inp.bat_socs,
        bat_caps=inp.bat_caps,
        bat_powers=inp.bat_powers,
        bat_modes=inp.bat_modes,
        consumers=(
            _c("vp", active=False, priority=2, priority_shed=2),
            _c("miner", active=False, priority=99, priority_shed=1),
        ),
    )
    # 2 consecutive export cycles required
    state = BudgetState(consecutive_export_cycles=2)
    result = allocate(inp_with_consumers, cfg, state)
    starts = [c for c in result.commands if c.command_type == CommandType.TURN_ON_CONSUMER]
    assert len(starts) == 1
    assert starts[0].target_id == "vp"  # lowest priority number wins


def test_plat1715_cascade_turn_off_highest_priority_shed_on_import() -> None:
    """grid importing > 100 W → turn off the highest-priority_shed ACTIVE
    consumer (shed the most expendable load first)."""
    cfg = BudgetConfig(cascade_cooldown_s=0.0, cascade_sustained_cycles=2)
    inp_with_consumers = BudgetInput(
        now=_inp().now,
        grid_power_w=400.0,  # import
        pv_power_w=500.0,
        house_load_w=1200.0,
        ev_connected=False,
        ev_charging=False,
        ev_current_amps=0,
        ev_soc_pct=50.0,
        ev_target_soc_pct=100.0,
        bat_socs={"k": 60.0, "f": 60.0},
        bat_caps={"k": 15.0, "f": 5.0},
        bat_powers={"k": 0.0, "f": 0.0},
        bat_modes={"k": "charge_pv", "f": "charge_pv"},
        consumers=(
            _c("vp", active=True, priority=2, priority_shed=2),
            _c("pool_heater", active=True, priority=4, priority_shed=4),
        ),
    )
    state = BudgetState()
    result = allocate(inp_with_consumers, cfg, state)
    stops = [c for c in result.commands if c.command_type == CommandType.TURN_OFF_CONSUMER]
    assert len(stops) == 1
    # pool_heater has priority_shed=4 — highest → first to shed
    assert stops[0].target_id == "pool_heater"


def test_plat1715_cascade_cooldown_prevents_immediate_reswitch() -> None:
    """A consumer that was just switched is not touched again inside the
    cooldown window, even if the grid signal would justify it."""
    cfg = BudgetConfig(cascade_cooldown_s=60.0, cascade_sustained_cycles=2)
    import time as _time

    now = _time.monotonic()
    state = BudgetState(
        consecutive_import_cycles=5,
        consumer_last_switch_ts={"vp": now},  # just switched
    )
    inp_with_consumers = BudgetInput(
        now=_inp().now,
        grid_power_w=500.0,
        pv_power_w=0.0,
        house_load_w=500.0,
        ev_connected=False,
        ev_charging=False,
        ev_current_amps=0,
        ev_soc_pct=50.0,
        ev_target_soc_pct=100.0,
        bat_socs={"k": 60.0},
        bat_caps={"k": 15.0},
        bat_powers={"k": 0.0},
        bat_modes={"k": "charge_pv"},
        consumers=(_c("vp", active=True, priority=2, priority_shed=2),),
    )
    result = allocate(inp_with_consumers, cfg, state)
    # vp is in cooldown → no turn_off should be emitted for it
    turn_offs_for_vp = [
        c
        for c in result.commands
        if c.command_type == CommandType.TURN_OFF_CONSUMER and c.target_id == "vp"
    ]
    assert len(turn_offs_for_vp) == 0


def test_plat1715_cascade_no_start_without_sustained_export() -> None:
    """A single export cycle is not enough; user rule requires sustain."""
    cfg = BudgetConfig(cascade_cooldown_s=0.0, cascade_sustained_cycles=2)
    inp_with_consumers = BudgetInput(
        now=_inp().now,
        grid_power_w=-500.0,
        pv_power_w=5000.0,
        house_load_w=500.0,
        ev_connected=False,
        ev_charging=False,
        ev_current_amps=0,
        ev_soc_pct=50.0,
        ev_target_soc_pct=100.0,
        bat_socs={"k": 60.0},
        bat_caps={"k": 15.0},
        bat_powers={"k": 0.0},
        bat_modes={"k": "charge_pv"},
        consumers=(_c("vp", active=False, priority=2, priority_shed=2),),
    )
    state = BudgetState(consecutive_export_cycles=0)
    result = allocate(inp_with_consumers, cfg, state)
    starts = [c for c in result.commands if c.command_type == CommandType.TURN_ON_CONSUMER]
    assert len(starts) == 0


# -------------------------------------------------------------------
# SET_FAST_CHARGING safety-steady-state command-stream coverage.
# Since PLAT-1696 (4172f81) Budget emits SET_FAST_CHARGING=True on
# emergency bats (SoC < soc_min_pct) and =False on all others, every
# cycle for ALL batteries. This is a safety-critical stream that drives
# GoodWe firmware behaviour:
#   - True + charge_battery → force grid-charge (recovery from SoC dip)
#   - False + discharge_pv → honours INV-3 (no fast_charging in discharge)
# Tests below cover both paths via allocate() so the stream never goes
# untested across refactors.
# -------------------------------------------------------------------


def test_emergency_bat_gets_fast_charging_true_via_allocate() -> None:
    """SoC below floor → allocate() emits SET_FAST_CHARGING=True for that
    bat. The twin bat (healthy SoC) must get SET_FAST_CHARGING=False in
    the same cycle (INV-3 invariant — no fast_charging outside recovery).
    """
    cfg = BudgetConfig(bat_discharge_min_soc_pct=15.0)
    # Need a scenario where zero_grid_active and daytime so bat emit path
    # is reachable. EM + modest surplus does the job.
    inp = _inp(
        hour=14,
        pv_w=3000,
        house_w=500,
        grid_w=-500,
        bat_k_soc=10.0,  # below floor (15) → emergency
        bat_f_soc=60.0,  # healthy
    )
    result = allocate(inp, cfg)

    fc_cmds = [c for c in result.commands if c.command_type == CommandType.SET_FAST_CHARGING]
    fc_map = {c.target_id: c.value for c in fc_cmds}

    assert "kontor" in fc_map, "Expected SET_FAST_CHARGING emitted for emergency bat 'kontor'"
    assert fc_map["kontor"] is True, (
        f"Emergency bat (SoC=10 < floor=15) must get fast_charging=True, "
        f"got {fc_map['kontor']!r}"
    )
    assert "forrad" in fc_map, (
        "Expected SET_FAST_CHARGING also emitted for non-emergency bat "
        "(INV-3: explicit OFF elsewhere)"
    )
    assert fc_map["forrad"] is False, (
        f"Non-emergency bat must get fast_charging=False (INV-3), " f"got {fc_map['forrad']!r}"
    )

    # Reason-string sanity: emergency reason is explicit
    emergency_cmd = next(c for c in fc_cmds if c.target_id == "kontor")
    assert "EMERGENCY" in emergency_cmd.reason or "floor" in emergency_cmd.reason
    normal_cmd = next(c for c in fc_cmds if c.target_id == "forrad")
    assert "INV-3" in normal_cmd.reason or "OFF" in normal_cmd.reason


def test_normal_bat_gets_fast_charging_false_via_allocate() -> None:
    """No bat below floor → allocate() emits SET_FAST_CHARGING=False for
    EVERY bat. The stream runs every cycle regardless of mode (charge,
    discharge, standby) — we verify both bats receive the False command.
    """
    cfg = BudgetConfig(bat_discharge_min_soc_pct=15.0)
    inp = _inp(
        hour=14,
        pv_w=3000,
        house_w=500,
        grid_w=-500,
        bat_k_soc=60.0,
        bat_f_soc=60.0,
    )
    result = allocate(inp, cfg)

    fc_cmds = [c for c in result.commands if c.command_type == CommandType.SET_FAST_CHARGING]
    fc_map = {c.target_id: c.value for c in fc_cmds}

    assert set(fc_map.keys()) == {
        "kontor",
        "forrad",
    }, f"Expected SET_FAST_CHARGING for both bats, got {set(fc_map.keys())}"
    assert fc_map["kontor"] is False
    assert fc_map["forrad"] is False
    for c in fc_cmds:
        assert (
            "INV-3" in c.reason or "OFF" in c.reason
        ), f"Reason must reference INV-3 invariant, got: {c.reason!r}"


# -------------------------------------------------------------------
# PLAT-1738: cascade must verify bat is truly at max before turning on
# consumers. Prior behaviour triggered on 2 sustained export cycles
# regardless of bat headroom — miner turned on during cloud-dip even
# when both bats were at 50 % SoC with 3 kW charge-headroom each.
# -------------------------------------------------------------------


def test_plat1738_cascade_skips_when_bat_has_headroom() -> None:
    """Sustained export + both bats at 50 % SoC → cascade must NOT fire.

    Bat still has 3+ kW charge-headroom, so any surplus should be absorbed
    by the battery, not by dispatchable consumers.
    """
    cfg = BudgetConfig(cascade_cooldown_s=0.0, cascade_sustained_cycles=2)
    inp_with_consumers = BudgetInput(
        now=_inp().now,
        grid_power_w=-300.0,
        pv_power_w=3000.0,
        house_load_w=500.0,
        ev_connected=False,
        ev_charging=False,
        ev_current_amps=0,
        ev_soc_pct=50.0,
        ev_target_soc_pct=100.0,
        bat_socs={"k": 50.0, "f": 50.0},
        bat_caps={"k": 15.0, "f": 5.0},
        bat_powers={"k": -1000.0, "f": -1000.0},  # charging 1 kW each
        bat_modes={"k": "charge_battery", "f": "charge_battery"},
        consumers=(
            _c("miner", active=False, priority=99, priority_shed=1),
            _c("vp", active=False, priority=2, priority_shed=2),
        ),
    )
    state = BudgetState(consecutive_export_cycles=5)
    result = allocate(inp_with_consumers, cfg, state)
    starts = [c for c in result.commands if c.command_type == CommandType.TURN_ON_CONSUMER]
    assert not starts, (
        f"PLAT-1738: cascade fired despite bat headroom (SoC 50 %). "
        f"Offending reasons: {[c.reason for c in starts]}"
    )


def test_plat1738_cascade_fires_when_bat_at_stop_soc() -> None:
    """Both bats at or above charge_stop_soc_pct → cascade SHOULD fire.

    Bats cannot absorb more charge (firmware stops at stop-SoC), so
    surplus must be routed to dispatchable consumers.
    """
    cfg = BudgetConfig(
        cascade_cooldown_s=0.0,
        cascade_sustained_cycles=2,
        bat_charge_stop_soc_pct=95.0,
    )
    inp_with_consumers = BudgetInput(
        now=_inp().now,
        grid_power_w=-300.0,
        pv_power_w=3000.0,
        house_load_w=500.0,
        ev_connected=False,
        ev_charging=False,
        ev_current_amps=0,
        ev_soc_pct=50.0,
        ev_target_soc_pct=100.0,
        bat_socs={"k": 95.5, "f": 96.0},  # at/above stop
        bat_caps={"k": 15.0, "f": 5.0},
        bat_powers={"k": 0.0, "f": 0.0},
        bat_modes={"k": "battery_standby", "f": "battery_standby"},
        consumers=(_c("vp", active=False, priority=2, priority_shed=2),),
    )
    state = BudgetState(consecutive_export_cycles=5)
    result = allocate(inp_with_consumers, cfg, state)
    starts = [c for c in result.commands if c.command_type == CommandType.TURN_ON_CONSUMER]
    assert len(starts) == 1, (
        f"PLAT-1738: cascade should fire when both bats at stop-SoC. " f"Got {len(starts)} starts."
    )
    assert starts[0].target_id == "vp"


def test_plat1738_cascade_fires_when_both_bats_above_stop_soc() -> None:
    """Both bats are at/above charge_stop_soc_pct — cascade SHOULD fire.

    Covers the symmetric "all bats hit firmware-stop" path. The asymmetric
    case (one bat at stop, other with headroom) is NOT covered here because
    cascade should NOT fire in that case — the low-SoC bat still has
    capacity to absorb surplus.
    """
    cfg = BudgetConfig(
        cascade_cooldown_s=0.0,
        cascade_sustained_cycles=2,
        bat_charge_stop_soc_pct=95.0,
    )
    inp_with_consumers = BudgetInput(
        now=_inp().now,
        grid_power_w=-300.0,
        pv_power_w=3000.0,
        house_load_w=500.0,
        ev_connected=False,
        ev_charging=False,
        ev_current_amps=0,
        ev_soc_pct=50.0,
        ev_target_soc_pct=100.0,
        bat_socs={"k": 96.0, "f": 96.0},  # both at/above stop
        bat_caps={"k": 15.0, "f": 5.0},
        bat_powers={"k": 0.0, "f": 0.0},
        bat_modes={"k": "battery_standby", "f": "battery_standby"},
        consumers=(_c("vp", active=False, priority=2, priority_shed=2),),
    )
    state = BudgetState(consecutive_export_cycles=5)
    result = allocate(inp_with_consumers, cfg, state)
    starts = [c for c in result.commands if c.command_type == CommandType.TURN_ON_CONSUMER]
    assert len(starts) == 1


def test_plat1738_cascade_fires_when_bat_alloc_at_physical_max() -> None:
    """Both bats at SoC 88 % (under stop) but running at physical max
    charge-rate — firmware hasn't stopped yet but inverter is saturated.
    Cascade SHOULD fire via the alloc_at_max arm of _bat_at_max.

    Covers 901 QC F1: earlier test suite only exercised the soc_at_stop
    arm; this case verifies the alloc-based saturation path.
    """
    cfg = BudgetConfig(
        cascade_cooldown_s=0.0,
        cascade_sustained_cycles=2,
        bat_charge_stop_soc_pct=95.0,
        bat_default_max_charge_w=5000,
        bat_at_max_headroom_w=500,  # alloc ≥ 4500 W counts as saturated
    )
    # Massive PV surplus → zero_grid will allocate max charge to both bats
    inp_with_consumers = BudgetInput(
        now=_inp().now,
        grid_power_w=-8000.0,
        pv_power_w=15000.0,
        house_load_w=500.0,
        ev_connected=False,
        ev_charging=False,
        ev_current_amps=0,
        ev_soc_pct=50.0,
        ev_target_soc_pct=100.0,
        bat_socs={"k": 88.0, "f": 88.0},  # under stop-SoC
        bat_caps={"k": 15.0, "f": 5.0},
        bat_powers={"k": -4800.0, "f": -4800.0},  # each charging near max
        bat_modes={"k": "charge_battery", "f": "charge_battery"},
        consumers=(_c("vp", active=False, priority=2, priority_shed=2),),
    )
    state = BudgetState(consecutive_export_cycles=5)
    result = allocate(inp_with_consumers, cfg, state)
    starts = [c for c in result.commands if c.command_type == CommandType.TURN_ON_CONSUMER]
    assert len(starts) == 1, (
        f"PLAT-1738 F1: cascade should fire via alloc_at_max arm "
        f"(bats under stop-SoC but at physical max). Got {len(starts)} starts."
    )
    reason = starts[0].reason
    # Reason must show alloc-max tag (not stop-SoC) — proves alloc-arm triggered
    assert (
        "alloc-max" in reason
    ), f"PLAT-1738 F1: reason should mark bats as 'alloc-max', got: {reason!r}"


# -------------------------------------------------------------------
# PLAT-1740: Budget EV-path must be idempotent against HA-state flap.
# Root cause 2026-04-19: 277 switch.easee_home_12840_is_enabled on/off
# events in 8 hours — Easee integration's plug sensor kept flipping
# unavailable↔on, which toggled ev_charging input flag cycle-by-cycle.
# Budget saw "not charging" each flap → re-emitted start_ev_charging,
# which locked the charger in a state it could never escape.
# Fix: compare against Budget's INTENDED state (what we told the charger
# to be) rather than HA's reported state.
# -------------------------------------------------------------------


def test_plat1740_start_not_re_emitted_when_intended_already_on() -> None:
    """Budget has already told the charger to start. HA flaps ev_charging
    back to False (plug-sensor glitch). Budget must NOT re-emit START."""
    cfg = BudgetConfig()
    inp = _inp(
        hour=14,
        pv_w=6000,
        house_w=500,
        grid_w=-4500,
        ev_connected=True,
        ev_charging=False,  # flapped back to False
        ev_amps=6,
        ev_soc=50.0,
        ev_target=100.0,
    )
    # State: Budget already emitted START on a previous cycle
    state = BudgetState(intended_ev_enabled=True, ev_current_amps=6)
    result = allocate(inp, cfg, state)

    starts = [c for c in result.commands if c.command_type == CommandType.START_EV_CHARGING]
    assert not starts, (
        "PLAT-1740: START re-emitted despite intended_ev_enabled=True. "
        f"Offending: {[c.reason for c in starts]}"
    )


def test_plat1740_stop_not_re_emitted_when_intended_already_off() -> None:
    """Budget has already told the charger to stop. HA flaps ev_charging
    back to True. Budget must NOT re-emit STOP."""
    cfg = BudgetConfig()
    inp = _inp(
        hour=14,
        pv_w=0,
        house_w=2000,
        grid_w=2000,  # no surplus
        ev_connected=True,
        ev_charging=True,  # HA flapped to True
        ev_amps=0,
        ev_soc=50.0,
        ev_target=100.0,
    )
    # State: Budget already emitted STOP on a previous cycle
    state = BudgetState(intended_ev_enabled=False, ev_current_amps=0)
    result = allocate(inp, cfg, state)

    stops = [c for c in result.commands if c.command_type == CommandType.STOP_EV_CHARGING]
    assert not stops, (
        "PLAT-1740: STOP re-emitted despite intended_ev_enabled=False. "
        f"Offending: {[c.reason for c in stops]}"
    )


def test_plat1740_start_emitted_once_on_transition_from_off_to_on() -> None:
    """First cycle where Budget decides to enable EV: START must be emitted
    and intended_ev_enabled state flipped to True.

    Uses FM (hour=10) so ev_wants_charge drives ev_target directly without
    requiring specific bat states.
    """
    cfg = BudgetConfig()
    inp = _inp(
        hour=10,
        pv_w=6000,
        house_w=500,
        grid_w=-4500,
        ev_connected=True,
        ev_charging=False,
        ev_amps=0,
        ev_soc=50.0,
        ev_target=100.0,
    )
    state = BudgetState(intended_ev_enabled=False, ev_current_amps=0)
    result = allocate(inp, cfg, state)

    starts = [c for c in result.commands if c.command_type == CommandType.START_EV_CHARGING]
    assert len(starts) == 1, "START must be emitted on off→on transition"
    assert (
        state.intended_ev_enabled is True
    ), "intended_ev_enabled must flip to True after emitting START"


def test_plat1740_stop_emitted_once_on_transition_from_on_to_off() -> None:
    """First cycle where Budget decides to disable EV: STOP must be emitted
    and intended_ev_enabled state flipped to False."""
    cfg = BudgetConfig()
    inp = _inp(
        hour=14,
        pv_w=0,
        house_w=2000,
        grid_w=2000,
        ev_connected=True,
        ev_charging=True,
        ev_amps=6,
        ev_soc=50.0,
        ev_target=100.0,
    )
    state = BudgetState(intended_ev_enabled=True, ev_current_amps=6)
    result = allocate(inp, cfg, state)

    stops = [c for c in result.commands if c.command_type == CommandType.STOP_EV_CHARGING]
    assert len(stops) == 1, "STOP must be emitted on on→off transition"
    assert (
        state.intended_ev_enabled is False
    ), "intended_ev_enabled must flip to False after emitting STOP"


def test_plat1740_set_current_compares_to_intended_not_ha_state() -> None:
    """SET_EV_CURRENT must compare against intended amps (what we wrote
    last) not HA-reported amps (which can lag or flap)."""
    cfg = BudgetConfig()
    inp = _inp(
        hour=14,
        pv_w=6000,
        house_w=500,
        grid_w=-4500,
        ev_connected=True,
        ev_charging=True,
        ev_amps=0,  # HA reports 0 (stale/glitch)
        ev_soc=50.0,
        ev_target=100.0,
    )
    # State: Budget told charger 6A last cycle
    state = BudgetState(intended_ev_enabled=True, ev_current_amps=6)
    result = allocate(inp, cfg, state)

    sets = [c for c in result.commands if c.command_type == CommandType.SET_EV_CURRENT]
    # If ev_target resolves to 6A (same as intended), no re-emit
    # If Budget ramps it up, emit once with new value, not the stale HA one
    if sets:
        assert all(c.value != 0 for c in sets), (
            "PLAT-1740: SET_EV_CURRENT must target intended, not 0 "
            f"(which was the stale HA value). Got: {[c.value for c in sets]}"
        )


def test_plat1740_277_flap_regression() -> None:
    """Regression: 277 switch-flips in 8 h on 2026-04-19 night.
    Simulated: 20 cycles alternating ev_charging True/False while Budget
    wants EV on (PV surplus, intent=on). Budget must emit AT MOST 1 START
    for the entire flap sequence (idempotent against HA brus).
    """
    cfg = BudgetConfig()
    state = BudgetState(intended_ev_enabled=False, ev_current_amps=0)

    start_count = 0
    stop_count = 0
    for i in range(20):
        # alternate: HA reports charging true/false even though we want it on
        ha_charging = i % 2 == 0
        inp = _inp(
            hour=14,
            pv_w=6000,
            house_w=500,
            grid_w=-4500,
            ev_connected=True,
            ev_charging=ha_charging,
            ev_amps=6 if ha_charging else 0,
            ev_soc=50.0,
            ev_target=100.0,
        )
        result = allocate(inp, cfg, state)
        start_count += sum(
            1 for c in result.commands if c.command_type == CommandType.START_EV_CHARGING
        )
        stop_count += sum(
            1 for c in result.commands if c.command_type == CommandType.STOP_EV_CHARGING
        )

    assert start_count <= 1, (
        f"PLAT-1740: {start_count} START commands emitted over 20 HA-flap "
        f"cycles (expected ≤1). Idempotency broken."
    )
    assert stop_count == 0, (
        f"PLAT-1740: {stop_count} STOP commands emitted despite consistent "
        f"intent=on. Budget must not STOP just because HA flaps."
    )


def test_plat1740_night_mode_does_not_touch_intended_state() -> None:
    """Night window: Budget must stay out of EV entirely (NightEV owns).
    intended_ev_enabled must NOT be modified by Budget at night."""
    cfg = BudgetConfig()
    inp = _inp(
        hour=23,
        pv_w=0,
        house_w=2000,
        grid_w=2000,
        ev_connected=True,
        ev_charging=True,
        ev_amps=6,
        ev_soc=50.0,
        ev_target=100.0,
    )
    # NightEV has set state: EV enabled at 6A
    state = BudgetState(intended_ev_enabled=True, ev_current_amps=6)
    result = allocate(inp, cfg, state)

    ev_cmds = [
        c
        for c in result.commands
        if c.command_type
        in (
            CommandType.START_EV_CHARGING,
            CommandType.STOP_EV_CHARGING,
            CommandType.SET_EV_CURRENT,
        )
    ]
    assert not ev_cmds, (
        f"PLAT-1740: Budget must not emit EV cmds at night. "
        f"Offending: {[(c.command_type.value, c.reason) for c in ev_cmds]}"
    )
    # State also untouched
    assert state.intended_ev_enabled is True


# -------------------------------------------------------------------
# PLAT-1737 step 2: grid_tuner integration in Budget.
# Budget applies tune_grid_delta() to bat_alloc after zero_grid runs,
# gated by cfg.grid_tuner.enabled. Rolling window is always updated
# (cheap, enables future mode-change guard).
# -------------------------------------------------------------------


def test_plat1737_tuner_disabled_no_change_to_bat_alloc() -> None:
    """grid_tuner.enabled=False → bat_alloc unchanged vs baseline."""
    from core.grid_tuner import GridTunerConfig

    cfg = BudgetConfig(grid_tuner=GridTunerConfig(enabled=False))
    inp = _inp(
        hour=12,
        pv_w=5000,
        house_w=500,
        grid_w=-500.0,  # big enough export so zero_grid allocates real charge
        bat_k_soc=60,
        bat_f_soc=60,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    baseline_total = sum(result.bat_allocations.values())

    # Now with enabled=True (tier3 + 500 W correction added to charge)
    cfg_on = BudgetConfig(
        grid_tuner=GridTunerConfig(
            enabled=True,
            tiers_w=(50.0, 75.0, 100.0),
            corrections_w=(100, 300, 500),
            rolling_window_s=300,
            mode_change_stability_w=50.0,
        ),
    )
    state2 = BudgetState()
    result_on = allocate(inp, cfg_on, state2)
    tuned_total = sum(result_on.bat_allocations.values())

    # With tuner on, grid export > tier3 → negative delta (more charge)
    # so bat_alloc total should be HIGHER (charge more)
    assert tuned_total > baseline_total, (
        f"Tuner should increase charge on export. baseline={baseline_total}W "
        f"tuned={tuned_total}W"
    )


def test_plat1737_tuner_respects_bat_max_on_charge() -> None:
    """Delta applied to charge-mode must not push alloc past max_charge_w."""
    from core.grid_tuner import GridTunerConfig

    cfg = BudgetConfig(
        bat_default_max_charge_w=3000,  # low cap for test
        grid_tuner=GridTunerConfig(
            enabled=True,
            tiers_w=(50.0, 75.0, 100.0),
            corrections_w=(100, 300, 2000),  # huge tier3 correction
            rolling_window_s=300,
            mode_change_stability_w=50.0,
        ),
    )
    inp = _inp(
        hour=12,
        pv_w=10000,
        house_w=500,
        grid_w=-3000.0,  # big export → tier3
        bat_k_soc=60,
        bat_f_soc=60,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    for bid, alloc in result.bat_allocations.items():
        assert (
            alloc <= cfg.bat_default_max_charge_w
        ), f"PLAT-1737: {bid} alloc {alloc}W > max {cfg.bat_default_max_charge_w}W"


def test_plat1737_tuner_floors_alloc_at_zero() -> None:
    """Delta on charge-mode with big import must not drive alloc below 0."""
    from core.grid_tuner import GridTunerConfig

    cfg = BudgetConfig(
        grid_tuner=GridTunerConfig(
            enabled=True,
            tiers_w=(50.0, 75.0, 100.0),
            corrections_w=(100, 300, 10000),  # huge tier3 correction
            rolling_window_s=300,
            mode_change_stability_w=50.0,
        ),
    )
    inp = _inp(
        hour=12,
        pv_w=3000,
        house_w=500,
        grid_w=500.0,  # import → tier3 positive delta → reduce charge
        bat_k_soc=60,
        bat_f_soc=60,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    for bid, alloc in result.bat_allocations.items():
        assert alloc >= 0, f"PLAT-1737: {bid} alloc {alloc}W < 0"


def test_plat1737_rolling_window_accumulates_in_budget_state() -> None:
    """BudgetState.grid_rolling must accumulate samples each cycle, always
    (regardless of grid_tuner.enabled)."""
    cfg = BudgetConfig()  # default: tuner disabled
    state = BudgetState()
    for gw in (100.0, 200.0, 300.0):
        inp = _inp(hour=12, grid_w=gw, pv_w=1000, house_w=500)
        allocate(inp, cfg, state)
    # Rolling window has 3 samples
    assert len(state.grid_rolling.history) == 3


def test_plat1737_tuner_deadband_no_change() -> None:
    """|grid| < tier1 (50 W) → delta=0 → bat_alloc unchanged."""
    from core.grid_tuner import GridTunerConfig

    cfg_baseline = BudgetConfig()
    cfg_tuner = BudgetConfig(
        grid_tuner=GridTunerConfig(
            enabled=True,
            tiers_w=(50.0, 75.0, 100.0),
            corrections_w=(100, 300, 500),
            rolling_window_s=300,
            mode_change_stability_w=50.0,
        ),
    )
    inp = _inp(
        hour=12,
        pv_w=3000,
        house_w=500,
        grid_w=-30.0,  # below tier1
        bat_k_soc=60,
        bat_f_soc=60,
    )
    r1 = allocate(inp, cfg_baseline, BudgetState())
    r2 = allocate(inp, cfg_tuner, BudgetState())
    assert (
        r1.bat_allocations == r2.bat_allocations
    ), "PLAT-1737: inside deadband → tuner must be no-op"


def test_plat1738_cascade_reason_reflects_soc_state() -> None:
    """Reason-string must NOT lie — include actual bat SoC/headroom info."""
    cfg = BudgetConfig(
        cascade_cooldown_s=0.0,
        cascade_sustained_cycles=2,
        bat_charge_stop_soc_pct=95.0,
    )
    inp_with_consumers = BudgetInput(
        now=_inp().now,
        grid_power_w=-300.0,
        pv_power_w=3000.0,
        house_load_w=500.0,
        ev_connected=False,
        ev_charging=False,
        ev_current_amps=0,
        ev_soc_pct=50.0,
        ev_target_soc_pct=100.0,
        bat_socs={"k": 95.0, "f": 97.0},
        bat_caps={"k": 15.0, "f": 5.0},
        bat_powers={"k": 0.0, "f": 0.0},
        bat_modes={"k": "battery_standby", "f": "battery_standby"},
        consumers=(_c("vp", active=False, priority=2, priority_shed=2),),
    )
    state = BudgetState(consecutive_export_cycles=5)
    result = allocate(inp_with_consumers, cfg, state)
    starts = [c for c in result.commands if c.command_type == CommandType.TURN_ON_CONSUMER]
    assert starts
    reason = starts[0].reason
    # Reason must mention bat SoC state — no more "bat at max" lie
    assert (
        "SoC" in reason or "soc" in reason
    ), f"PLAT-1738: reason must describe actual bat state, got: {reason!r}"


# -------------------------------------------------------------------
# PLAT-1696 step 1: grid-sensor smoothing (median-of-N)
# -------------------------------------------------------------------


def test_grid_smoothing_window_filled_over_cycles(cfg: BudgetConfig) -> None:
    """allocate() pushes grid_power_w into state.grid_history_w each cycle."""
    state = BudgetState()
    for gw in (500.0, 600.0, 700.0):
        inp = _inp(hour=14, pv_w=1000, house_w=500, grid_w=gw)
        allocate(inp, cfg, state)
    assert state.grid_history_w == [500.0, 600.0, 700.0]


def test_grid_smoothing_window_bounded(cfg: BudgetConfig) -> None:
    """Window size is bounded by cfg.grid_smoothing_window."""
    cfg3 = BudgetConfig(grid_smoothing_window=3)
    state = BudgetState()
    for gw in (100.0, 200.0, 300.0, 400.0, 500.0):
        inp = _inp(hour=14, pv_w=1000, house_w=500, grid_w=gw)
        allocate(inp, cfg3, state)
    # Only the last 3 remain.
    assert state.grid_history_w == [300.0, 400.0, 500.0]


def test_grid_spike_rejected_by_median(cfg: BudgetConfig) -> None:
    """A single 12.9 kW spike in a quiet stream does NOT drive the bat.

    Simulates the live observation (23:26-23:27): real grid ≈ 2.5 kW,
    sensor reported 12.9 kW for one cycle. With median-of-3 the spike
    is rejected and bat allocation follows the real value.
    """
    state = BudgetState()
    # Prime the history with two quiet readings, then inject a spike.
    for gw in (2500.0, 2500.0):
        allocate(
            _inp(hour=14, pv_w=0, house_w=2500, grid_w=gw, bat_k_soc=60.0, bat_f_soc=60.0),
            cfg,
            state,
        )
    # Spike cycle — sorted history = [2500, 2500, 12900] → median = 2500.
    spike_inp = _inp(
        hour=14,
        pv_w=0,
        house_w=2500,
        grid_w=12900.0,
        bat_k_soc=60.0,
        bat_f_soc=60.0,
    )
    spike_result = allocate(spike_inp, cfg, state)
    assert state.grid_history_w == [2500.0, 2500.0, 12900.0]
    # bat_discharge should reflect the 2500 W MEDIAN, not the 12900 W spike.
    # With gain=0.7: discharge ≈ 0.7 × 2500 = 1750 W.
    assert spike_result.bat_discharge_w == int(2500.0 * 0.7)


# -------------------------------------------------------------------
# PLAT-1754: verify cfg.bat_aggressive_spread_pct passthrough
# -------------------------------------------------------------------


def test_plat1754_allocate_passes_cfg_spread_to_plan_zero_grid() -> None:
    """PLAT-1754: allocate() must forward cfg.bat_aggressive_spread_pct to
    plan_zero_grid() — the function's parameter default (5.0) must never
    silently override the BudgetConfig value (user invariant: 1.0 = 1 pp).

    Verification: patch plan_zero_grid inside core.budget and assert the
    keyword arg matches the configured value, not the module-level default.
    """
    from unittest.mock import patch

    from core.zero_grid import ZeroGridPlan

    # Deliberately != BudgetConfig default (1.0) AND != plan_zero_grid default (5.0)
    custom_spread = 2.5
    cfg = BudgetConfig(bat_aggressive_spread_pct=custom_spread)
    fake_plan = ZeroGridPlan(
        modes={"kontor": "charge_pv", "forrad": "charge_pv"},
        limits_w={"kontor": 0, "forrad": 0},
        total_target_net_w=0,
        reason="test",
        emergency_recovery=frozenset(),
    )
    with patch("core.budget.plan_zero_grid", return_value=fake_plan) as mock_pgz:
        allocate(_inp(hour=10), cfg, BudgetState())

    mock_pgz.assert_called_once()
    forwarded = mock_pgz.call_args.kwargs.get("spread_aggressive_pct")
    assert forwarded == custom_spread, (
        f"allocate() forwarded spread={forwarded!r} but expected cfg value {custom_spread!r}. "
        "Remove the keyword arg from the plan_zero_grid call and the default (5.0) silently wins."
    )


# -------------------------------------------------------------------
# PLAT-1752: Grid-tuner step 3 — mode-change-guard via 5-min rolling avg.
#
# Post-process zero_grid.modes after grid_tuner: when grid_tuner is
# enabled and |rolling_avg| < mode_change_stability_w (50 W default),
# suppress charge ↔ discharge mode flips.  Standby transitions always
# pass through.  When tuner is disabled, guard is a no-op.
# -------------------------------------------------------------------


def _make_cfg_with_guard(
    *,
    stability_w: float = 50.0,
) -> "BudgetConfig":
    """BudgetConfig with grid_tuner enabled and the guard active."""
    from core.grid_tuner import GridTunerConfig

    return BudgetConfig(
        grid_tuner=GridTunerConfig(
            enabled=True,
            tiers_w=(50.0, 75.0, 100.0),
            corrections_w=(100, 300, 500),
            rolling_window_s=300,
            mode_change_stability_w=stability_w,
        ),
    )


def _seed_rolling(state: "BudgetState", values: list[float]) -> None:
    """Directly seed BudgetState.grid_rolling with specific grid readings."""
    import time

    ts = time.monotonic()
    for i, v in enumerate(values):
        state.grid_rolling.add(ts + float(i), v, 300)


def test_plat1752_guard_disabled_does_not_block() -> None:
    """grid_tuner.enabled=False → guard never fires, mode changes proceed."""
    from core.grid_tuner import GridTunerConfig

    cfg = BudgetConfig(
        grid_tuner=GridTunerConfig(enabled=False, mode_change_stability_w=50.0),
    )
    # Rolling avg ≈ 0 → would trigger guard IF enabled (300 zeros dilute any reading)
    state = BudgetState()
    _seed_rolling(state, [0.0] * 300)

    # Current mode = discharge_pv, zero_grid will plan charge_battery
    # (big export forces charge). Guard disabled → mode change emitted.
    inp = _inp(
        hour=12,
        pv_w=8000,
        house_w=500,
        grid_w=-3000.0,  # big export → zero_grid plans charge_battery
        bat_k_soc=50,
        bat_f_soc=50,
        bat_k_mode="discharge_pv",
        bat_f_mode="discharge_pv",
    )
    result = allocate(inp, cfg, state)
    mode_cmds = [c for c in result.commands if c.command_type.name == "SET_EMS_MODE"]
    # At least one battery should get a SET_EMS_MODE (discharge→charge)
    assert mode_cmds, "PLAT-1752: guard disabled — SET_EMS_MODE must be emitted when mode changes"


def test_plat1752_guard_blocks_charge_to_discharge_when_stable() -> None:
    """stable rolling avg (|avg| < 50 W) → charge→discharge flip blocked.

    300 zero-samples dilute the current big import reading so the rolling
    avg stays within ±stability_w even when zero_grid plans discharge.
    """
    cfg = _make_cfg_with_guard(stability_w=50.0)
    state = BudgetState()
    # 300 zeros simulate 5 min of balanced grid — avg stays near 0
    # even after the current +3000 W cycle is added (avg ≈ +10 W < 50 W).
    _seed_rolling(state, [0.0] * 300)

    # Current mode = charge_battery, big import drives zero_grid to discharge
    inp = _inp(
        hour=12,
        pv_w=2000,
        house_w=500,
        grid_w=3000.0,  # big import → zero_grid plans discharge_pv
        bat_k_soc=80,
        bat_f_soc=80,
        bat_k_mode="charge_battery",
        bat_f_mode="charge_battery",
    )
    result = allocate(inp, cfg, state)
    mode_cmds = [c for c in result.commands if c.command_type.name == "SET_EMS_MODE"]
    assert (
        not mode_cmds
    ), "PLAT-1752: rolling avg stable → charge→discharge mode flip must be blocked"


def test_plat1752_guard_blocks_discharge_to_charge_when_stable() -> None:
    """stable rolling avg → discharge→charge flip blocked.

    300 zero-samples dilute the current big export reading so the rolling
    avg stays within ±stability_w even when zero_grid plans charge.
    """
    cfg = _make_cfg_with_guard(stability_w=50.0)
    state = BudgetState()
    # 300 zeros → rolling avg ≈ -10 W after adding current -3000 W → guard fires
    _seed_rolling(state, [0.0] * 300)

    # Current mode = discharge_pv, big export drives zero_grid to charge
    inp = _inp(
        hour=12,
        pv_w=8000,
        house_w=500,
        grid_w=-3000.0,  # big export → zero_grid plans charge_battery
        bat_k_soc=50,
        bat_f_soc=50,
        bat_k_mode="discharge_pv",
        bat_f_mode="discharge_pv",
    )
    result = allocate(inp, cfg, state)
    mode_cmds = [c for c in result.commands if c.command_type.name == "SET_EMS_MODE"]
    assert (
        not mode_cmds
    ), "PLAT-1752: rolling avg stable → discharge→charge mode flip must be blocked"


def test_plat1752_guard_allows_change_when_trend_is_real() -> None:
    """|rolling_avg| >= stability_w → trend is real, mode change is allowed.

    Seed with sustained high import readings so the rolling avg stays well
    above the stability threshold even after the current cycle is added.
    """
    cfg = _make_cfg_with_guard(stability_w=50.0)
    state = BudgetState()
    # 300 samples at +200 W → avg stays ≈ +200 W after adding +3000 W.
    # |avg| >> 50 W → guard does NOT fire → mode change passes through.
    _seed_rolling(state, [200.0] * 300)

    # Current mode = charge_battery, sustained import → zero_grid plans discharge
    inp = _inp(
        hour=12,
        pv_w=2000,
        house_w=500,
        grid_w=3000.0,  # big import → discharge planned
        bat_k_soc=80,
        bat_f_soc=80,
        bat_k_mode="charge_battery",
        bat_f_mode="charge_battery",
    )
    result = allocate(inp, cfg, state)
    mode_cmds = [c for c in result.commands if c.command_type.name == "SET_EMS_MODE"]
    assert mode_cmds, "PLAT-1752: |avg|≈200W > 50W — trend is real, mode change must be emitted"


def test_plat1752_guard_allows_transition_to_standby() -> None:
    """Transition INTO battery_standby always passes through guard.

    Even with a stable rolling avg (guard fires), going to standby must
    be emitted — standby is a safety state, never suppressed.
    """
    cfg = _make_cfg_with_guard(stability_w=50.0)
    state = BudgetState()
    # 300 zeros → guard fires (avg ≈ -1 W after -300 W added → |avg| < 50 W)
    _seed_rolling(state, [0.0] * 300)

    # Scenario: bat SoC at charge stop → zero_grid will plan battery_standby
    # Current mode = charge_battery → guard must NOT block this standby transition
    inp = _inp(
        hour=12,
        pv_w=5000,
        house_w=500,
        grid_w=-300.0,
        bat_k_soc=cfg.bat_charge_stop_soc_pct,  # at charge stop → standby
        bat_f_soc=cfg.bat_charge_stop_soc_pct,
        bat_k_mode="charge_battery",
        bat_f_mode="charge_battery",
    )
    result = allocate(inp, cfg, state)
    mode_cmds = [c for c in result.commands if c.command_type.name == "SET_EMS_MODE"]
    # At least one bat must receive standby command (charge_battery → battery_standby)
    standby_cmds = [c for c in mode_cmds if c.value == "battery_standby"]
    assert standby_cmds, (
        "PLAT-1752: transition TO battery_standby must always be emitted "
        "even when rolling avg is stable"
    )


def test_plat1752_guard_allows_transition_from_standby() -> None:
    """Transition OUT OF battery_standby always passes through guard.

    300 zero-samples keep the rolling avg stable, but the guard must let
    standby→charge pass regardless, because standby is always a safe exit.
    """
    cfg = _make_cfg_with_guard(stability_w=50.0)
    state = BudgetState()
    # 300 zeros → avg ≈ -10 W after adding -3000 W → guard fires
    _seed_rolling(state, [0.0] * 300)

    # Current mode = battery_standby. Big export → zero_grid plans charge_battery.
    # Guard must allow standby→charge even though avg is stable.
    inp = _inp(
        hour=12,
        pv_w=8000,
        house_w=500,
        grid_w=-3000.0,  # export → charge
        bat_k_soc=50,
        bat_f_soc=50,
        bat_k_mode="battery_standby",
        bat_f_mode="battery_standby",
    )
    result = allocate(inp, cfg, state)
    mode_cmds = [c for c in result.commands if c.command_type.name == "SET_EMS_MODE"]
    assert mode_cmds, (
        "PLAT-1752: transition FROM battery_standby must always be emitted "
        "even when rolling avg is stable"
    )


def test_plat1752_oscillation_suppressed_below_1_per_5min() -> None:
    """AC: oscillation charge↔discharge < 1/5 min.

    300 alternating ±10 W samples keep the rolling avg near 0.  Each
    new large cycle reading (±3 000 W) is diluted by 300 existing samples,
    so avg stays within ±stability_w and the guard fires every cycle.
    Result: no mode-change commands are emitted despite alternating import/
    export signals — the inverter stays in one mode.
    """
    cfg = _make_cfg_with_guard(stability_w=50.0)
    state = BudgetState()

    # 300 alternating ±10 W → avg = 0 W.  Adding ±3000 W: avg = ±3000/301 ≈ ±10 W.
    # |avg| < 50 W → guard fires on EVERY cycle.
    alternating = [10.0 if i % 2 == 0 else -10.0 for i in range(300)]
    _seed_rolling(state, alternating)

    # Cycle A: big import → zero_grid plans discharge_pv. Guard suppresses.
    inp_import = _inp(
        hour=12,
        pv_w=2000,
        house_w=500,
        grid_w=3000.0,
        bat_k_soc=70,
        bat_f_soc=70,
        bat_k_mode="charge_battery",
        bat_f_mode="charge_battery",
    )
    result_a = allocate(inp_import, cfg, state)
    mode_cmds_a = [c for c in result_a.commands if c.command_type.name == "SET_EMS_MODE"]
    assert (
        not mode_cmds_a
    ), "PLAT-1752 oscillation: alternating avg≈0 → charge→discharge must be suppressed"

    # Cycle B: big export → zero_grid plans charge_battery.
    # Current mode is still charge_battery (guard held it) → idempotent, no cmd.
    inp_export = _inp(
        hour=12,
        pv_w=8000,
        house_w=500,
        grid_w=-3000.0,
        bat_k_soc=70,
        bat_f_soc=70,
        bat_k_mode="charge_battery",
        bat_f_mode="charge_battery",
    )
    result_b = allocate(inp_export, cfg, state)
    mode_cmds_b = [c for c in result_b.commands if c.command_type.name == "SET_EMS_MODE"]
    assert (
        not mode_cmds_b
    ), "PLAT-1752 oscillation: stayed in charge_battery → no mode cmd on export cycle"


def test_plat1752_guard_no_op_when_mode_already_matches() -> None:
    """Guard must not interfere when plan mode == current mode (idempotent)."""
    cfg = _make_cfg_with_guard(stability_w=50.0)
    state = BudgetState()
    _seed_rolling(state, [0.0] * 300)

    # Current = charge_battery, plan will also be charge_battery (export → charge)
    inp = _inp(
        hour=12,
        pv_w=8000,
        house_w=500,
        grid_w=-300.0,
        bat_k_soc=50,
        bat_f_soc=50,
        bat_k_mode="charge_battery",
        bat_f_mode="charge_battery",
    )
    result = allocate(inp, cfg, state)
    mode_cmds = [c for c in result.commands if c.command_type.name == "SET_EMS_MODE"]
    assert (
        not mode_cmds
    ), "PLAT-1752: plan==current → no SET_EMS_MODE (idempotency, guard must not break this)"
