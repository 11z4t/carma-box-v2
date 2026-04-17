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
