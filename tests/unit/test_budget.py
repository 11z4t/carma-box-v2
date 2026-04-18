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
) -> BudgetInput:
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
        bat_modes={"kontor": "battery_standby", "forrad": "battery_standby"},
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
        hour=9, pv_w=6000, house_w=500,
        ev_connected=True, ev_soc=50, grid_w=-2000,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps > 0


def test_fm_ev_no_start_without_surplus(cfg: BudgetConfig) -> None:
    """FM + EV connected but no surplus → EV stays off."""
    inp = _inp(
        hour=9, pv_w=500, house_w=500,
        ev_connected=True, ev_soc=50,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 0


# -------------------------------------------------------------------
# EM priority: bat → EV
# -------------------------------------------------------------------

def test_em_bat_priority(cfg: BudgetConfig) -> None:
    """EM + bat < 100% → all surplus to bat, EV off."""
    inp = _inp(hour=14, pv_w=3000, house_w=500, ev_connected=True, bat_k_soc=80)
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 0
    assert sum(result.bat_allocations.values()) > 0


def test_em_ev_after_bat_full(cfg: BudgetConfig) -> None:
    """EM + bat 100% + EV connected → EV gets surplus."""
    inp = _inp(
        hour=14, pv_w=6000, house_w=500,
        ev_connected=True, ev_soc=50, grid_w=-2000,
        bat_k_soc=100, bat_f_soc=100,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps > 0


# -------------------------------------------------------------------
# EV ramp ±1A
# -------------------------------------------------------------------

def test_ev_ramp_up_on_export(cfg: BudgetConfig) -> None:
    """Grid exporting 2 consecutive cycles → ramp +1A (tröghet)."""
    inp = _inp(
        hour=9, pv_w=6000, house_w=500,
        ev_connected=True, ev_charging=True, ev_amps=7,
        grid_w=-500,
    )
    state = BudgetState(consecutive_export_cycles=2, ev_current_amps=7)
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 8


def test_ev_ramp_down_on_import(cfg: BudgetConfig) -> None:
    """Grid importing 1 cycle → ramp -1A (snabb ner)."""
    inp = _inp(
        hour=9, pv_w=6000, house_w=500,
        ev_connected=True, ev_charging=True, ev_amps=9,
        grid_w=500,
    )
    state = BudgetState(consecutive_import_cycles=1, ev_current_amps=9)
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 8


def test_ev_hold_in_band(cfg: BudgetConfig) -> None:
    """Grid within ±100W → hold."""
    inp = _inp(
        hour=9, pv_w=6000, house_w=500,
        ev_connected=True, ev_charging=True, ev_amps=8,
        grid_w=50,
    )
    state = BudgetState(ev_current_amps=8)
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 8


def test_ev_max_capped(cfg: BudgetConfig) -> None:
    """At max amps (16) → no ramp up."""
    inp = _inp(
        hour=9, pv_w=15000, house_w=500,
        ev_connected=True, ev_charging=True, ev_amps=16,
        grid_w=-2000,
    )
    state = BudgetState(consecutive_export_cycles=3, ev_current_amps=16)
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 16


def test_ev_min_floored(cfg: BudgetConfig) -> None:
    """At min amps + import → stays at min (not 0, but stops if no surplus)."""
    inp = _inp(
        hour=9, pv_w=6000, house_w=500,
        ev_connected=True, ev_charging=True, ev_amps=6,
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
    """Same SoC → proportional by capacity (75/25)."""
    inp = _inp(pv_w=3000, house_w=500, bat_k_soc=70, bat_f_soc=70)
    state = BudgetState()
    result = allocate(inp, cfg, state)
    k = result.bat_allocations["kontor"]
    f = result.bat_allocations["forrad"]
    assert k > f  # 75% > 25%
    assert 0.7 < k / (k + f) < 0.8


def test_bat_unbalanced_lower_gets_more(cfg: BudgetConfig) -> None:
    """Spread > 1pp → lower gets 80%."""
    inp = _inp(pv_w=3000, house_w=500, bat_k_soc=90, bat_f_soc=95)
    state = BudgetState()
    result = allocate(inp, cfg, state)
    k = result.bat_allocations["kontor"]  # lower
    f = result.bat_allocations["forrad"]  # higher
    assert k > f


# -------------------------------------------------------------------
# EVENING_DISCHARGE: bat covers house load, grid target 0W
# -------------------------------------------------------------------

_EVENING_DISCHARGE_HOUR: int = 18
_EVENING_HOUSE_LOAD_W: float = 2000.0
_EVENING_GRID_IMPORT_W: float = 2000.0
_EVENING_BAT_SOC: float = 80.0
_EVENING_BAT_LOW_SOC: float = 15.0  # exactly at min_soc floor


def test_evening_discharge_covers_house_load(cfg: BudgetConfig) -> None:
    """17-20: bat discharge = house_load, targeting grid 0W."""
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR, pv_w=0, house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=_EVENING_BAT_SOC, bat_f_soc=_EVENING_BAT_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.bat_discharge_w == int(_EVENING_GRID_IMPORT_W)
    assert "EVENING_DISCHARGE" in result.reason


def test_evening_discharge_proportional(cfg: BudgetConfig) -> None:
    """Evening discharge proportional by available energy (like FM/EM)."""
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR, pv_w=0, house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=_EVENING_BAT_SOC, bat_f_soc=_EVENING_BAT_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    k = result.bat_allocations.get("kontor", 0)
    f = result.bat_allocations.get("forrad", 0)
    assert k > f, "Kontor (15kWh) should get more than Förråd (5kWh)"


def test_evening_discharge_discharge_pv_commands(cfg: BudgetConfig) -> None:
    """Evening discharge emits discharge_pv mode + limit commands."""
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR, pv_w=0, house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=_EVENING_BAT_SOC, bat_f_soc=_EVENING_BAT_SOC,
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
        hour=_EVENING_DISCHARGE_HOUR, pv_w=0, house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        ev_connected=True, ev_soc=50,
        bat_k_soc=_EVENING_BAT_SOC, bat_f_soc=_EVENING_BAT_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert result.ev_target_amps == 0


def test_evening_discharge_skipped_bat_low(cfg: BudgetConfig) -> None:
    """Evening discharge: bat at min_soc → no discharge, falls through to standby."""
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR, pv_w=0, house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=_EVENING_BAT_LOW_SOC, bat_f_soc=_EVENING_BAT_LOW_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    # Should NOT be evening discharge (bat too low)
    assert "EVENING_DISCHARGE" not in result.reason


def test_evening_discharge_bat_full_still_discharges(cfg: BudgetConfig) -> None:
    """Evening discharge: bat at 100% → STILL discharge to cover house load."""
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR, pv_w=0, house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=100.0, bat_f_soc=100.0,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert "EVENING_DISCHARGE" in result.reason
    assert result.bat_discharge_w > 0


def test_evening_discharge_grid_responsive(cfg: BudgetConfig) -> None:
    """Grid exporting (negative) → no discharge needed (bat already covers)."""
    inp = _inp(
        hour=_EVENING_DISCHARGE_HOUR, pv_w=0, house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=-500.0,  # exporting = bat gives too much
        bat_k_soc=_EVENING_BAT_SOC, bat_f_soc=_EVENING_BAT_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    # grid_w=-500 (export) → target = max(0, -500) = 0 → no discharge
    assert result.bat_discharge_w == 0


def test_evening_20h_no_discharge(cfg: BudgetConfig) -> None:
    """After 20:00 → evening standby, not discharge."""
    inp = _inp(
        hour=20, pv_w=0, house_w=_EVENING_HOUSE_LOAD_W,
        grid_w=_EVENING_GRID_IMPORT_W,
        bat_k_soc=_EVENING_BAT_SOC, bat_f_soc=_EVENING_BAT_SOC,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    assert "EVENING_DISCHARGE" not in result.reason
    assert result.bat_discharge_w == 0


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
        hour=9, pv_w=8000, house_w=500,
        ev_connected=True, ev_charging=False, ev_amps=0,
        grid_w=-4000,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    cmd_types = [c.command_type for c in result.commands]
    if result.ev_target_amps > 0:
        assert CommandType.START_EV_CHARGING in cmd_types


def test_stop_ev_command(cfg: BudgetConfig) -> None:
    """EV charging + no surplus → STOP command."""
    inp = _inp(
        hour=14, pv_w=500, house_w=500,
        ev_connected=True, ev_charging=True, ev_amps=6,
        bat_k_soc=80,
    )
    state = BudgetState()
    result = allocate(inp, cfg, state)
    cmd_types = [c.command_type for c in result.commands]
    assert CommandType.STOP_EV_CHARGING in cmd_types


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
    soc_k: float, soc_f: float, stop_pct: float, expect_bat_cmds: bool,
) -> None:
    """PLAT-1695: Stop charging at bat_charge_stop_soc_pct (config-driven).

    Must match state_machine.surplus_entry_soc_pct to eliminate dead zone.
    """
    cfg = BudgetConfig(bat_charge_stop_soc_pct=stop_pct)
    inp = _inp(
        hour=14,            # EM path (bat-prio)
        pv_w=5000.0,        # plenty of PV
        house_w=500.0,
        grid_w=-4000.0,     # exporting
        bat_k_soc=soc_k,
        bat_f_soc=soc_f,
    )

    result = allocate(inp, cfg)

    bat_charge_cmds = [
        c for c in result.commands
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
        bat_k_soc=94.0,   # below threshold
        bat_f_soc=96.0,   # above threshold
    )

    result = allocate(inp, cfg)

    charge_targets = {
        c.target_id for c in result.commands
        if c.command_type == CommandType.SET_EMS_MODE and c.value == "charge_battery"
    }
    standby_targets = {
        c.target_id for c in result.commands
        if c.command_type == CommandType.SET_EMS_MODE
        and c.value == "battery_standby"
    }

    assert "kontor" in charge_targets, (
        f"kontor (SoC 94%) should charge. Charge targets: {charge_targets}"
    )
    assert "forrad" in standby_targets, (
        f"forrad (SoC 96%) should be standby. Standby targets: {standby_targets}"
    )


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
        hour=14,           # EM
        pv_w=5000.0,
        house_w=500.0,
        grid_w=-4000.0,
        bat_k_soc=50.0,
        bat_f_soc=50.0,
    )

    result = allocate(inp, cfg)
    mode_cmds = [c for c in result.commands
                 if c.command_type == CommandType.SET_EMS_MODE]
    charge_pv_cmds = [c for c in mode_cmds if c.value == "charge_pv"]
    charge_bat_cmds = [c for c in mode_cmds if c.value == "charge_battery"]

    assert not charge_pv_cmds, (
        f"PLAT-1714: Budget must NOT emit charge_pv (uncontrollable in peak_shaving). "
        f"Offending cmds: {[(c.target_id, c.reason) for c in charge_pv_cmds]}"
    )
    assert charge_bat_cmds, (
        "PLAT-1714: Budget must emit charge_battery for PV-surplus absorption"
    )


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
    modes = {c.target_id: c.value for c in result.commands
             if c.command_type == CommandType.SET_EMS_MODE}
    limits = {c.target_id: c.value for c in result.commands
              if c.command_type == CommandType.SET_EMS_POWER_LIMIT}

    for bid, mode in modes.items():
        if mode == "charge_battery":
            assert bid in limits, (
                f"PLAT-1714: bat {bid} in charge_battery mode but no "
                f"SET_EMS_POWER_LIMIT emitted"
            )
            assert limits[bid] > 0, (
                f"PLAT-1714: bat {bid} charge_battery limit must be > 0, "
                f"got {limits[bid]}"
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
        bat_k_soc=96.0,   # above threshold → standby
        bat_f_soc=97.0,
    )

    result = allocate(inp, cfg)
    standby_targets = {c.target_id for c in result.commands
                       if c.command_type == CommandType.SET_EMS_MODE
                       and c.value == "battery_standby"}
    limit_zero_targets = {c.target_id for c in result.commands
                          if c.command_type == CommandType.SET_EMS_POWER_LIMIT
                          and c.value == 0}

    assert standby_targets == limit_zero_targets, (
        f"PLAT-1714: Every standby bat must also emit limit=0. "
        f"standby={standby_targets}, limit_zero={limit_zero_targets}"
    )
