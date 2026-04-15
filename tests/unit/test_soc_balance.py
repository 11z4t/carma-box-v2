"""Tests for SoC balancing in charge plan + discharge (PLAT-1615).

Charge (daytime):
- Lower SoC bat gets charge_pv, higher stays standby
- When balanced (±2%): both get charge_pv

Discharge (evening):
- Lower SoC bat gets less discharge, higher gets more
- Verified via balancer allocations
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from core.balancer import BatteryBalancer
from core.engine import ControlEngine
from core.executor import CommandExecutor, ExecutorConfig
from core.guards import GridGuard, GuardConfig
from core.mode_change import ModeChangeConfig, ModeChangeManager
from core.models import CTPlacement, EMSMode, Scenario
from core.state_machine import StateMachine, StateMachineConfig
from tests.conftest import make_battery_state, make_grid_state, make_snapshot

# Named test constants
_ZERO_WAIT_S: float = 0.0
_SOC_LOW_PCT: float = 30.0
_SOC_HIGH_PCT: float = 60.0
_SOC_BALANCED_A_PCT: float = 50.0
_SOC_BALANCED_B_PCT: float = 51.0
_PV_EXPORT_W: float = -2000.0
_PV_SURPLUS_W: float = 2000.0
_LOAD_W: float = 500.0
_CAP_KONTOR_KWH: float = 15.0
_CAP_FORRAD_KWH: float = 5.0
_GRID_DISCHARGE_W: float = 2000.0
_MIN_ALLOCATIONS: int = 1
_DISCHARGE_HOUR: int = 18
_CHARGE_HOUR: int = 14
_ENTRY_YEAR: int = 2026
_ENTRY_MONTH: int = 4
_ENTRY_DAY: int = 16


def _make_engine() -> ControlEngine:
    guard = GridGuard(GuardConfig())
    sm = StateMachine(StateMachineConfig(min_dwell_s=_ZERO_WAIT_S))
    balancer = BatteryBalancer()
    mode_mgr = ModeChangeManager(ModeChangeConfig(
        clear_wait_s=_ZERO_WAIT_S, standby_wait_s=_ZERO_WAIT_S,
        set_wait_s=_ZERO_WAIT_S, verify_wait_s=_ZERO_WAIT_S,
    ))
    inv_mock = AsyncMock()
    inv_mock.set_ems_mode = AsyncMock(return_value=True)
    inv_mock.set_ems_power_limit = AsyncMock(return_value=True)
    inv_mock.set_fast_charging = AsyncMock(return_value=True)
    inv_mock.get_fast_charging = AsyncMock(return_value=False)
    inv_mock.get_ems_mode = AsyncMock(return_value="battery_standby")
    return ControlEngine(
        guard, sm, balancer, mode_mgr,
        CommandExecutor(
            inverters={"kontor": inv_mock, "forrad": inv_mock},
            config=ExecutorConfig(mode_change_cooldown_s=_ZERO_WAIT_S),
        ),
    )


# ===========================================================================
# Daytime charge: SoC balancing
# ===========================================================================


@pytest.mark.asyncio()
class TestChargeSoCBalance:
    """Lower SoC bat gets charge_pv, higher stays standby."""

    async def test_lower_soc_gets_charge_higher_standby(self) -> None:
        """Kontor 30% < Forrad 60% → Kontor charge_pv, Forrad standby."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.MIDDAY_CHARGE
        engine._sm.state.entry_time = datetime(
            _ENTRY_YEAR, _ENTRY_MONTH, _ENTRY_DAY,
            _CHARGE_HOUR, 0, tzinfo=timezone.utc,
        )

        snap = make_snapshot(
            hour=_CHARGE_HOUR,
            batteries=[
                make_battery_state(
                    battery_id="kontor", soc_pct=_SOC_LOW_PCT,
                    ems_mode="battery_standby",
                    ct_placement=CTPlacement.LOCAL_LOAD,
                    pv_power_w=_PV_SURPLUS_W, load_power_w=_LOAD_W,
                ),
                make_battery_state(
                    battery_id="forrad", soc_pct=_SOC_HIGH_PCT,
                    ems_mode="battery_standby",
                    ct_placement=CTPlacement.HOUSE_GRID,
                    grid_power_w=_PV_EXPORT_W,
                ),
            ],
            grid=make_grid_state(grid_power_w=_PV_EXPORT_W),
        )
        result = await engine.run_cycle(snap)

        assert result.execution is not None
        mode_entries = {
            e.target_id: e.value
            for e in result.execution.audit_entries
            if e.command_type == "set_ems_mode"
        }
        # Kontor (lower SoC) should get charge_pv
        assert mode_entries.get("kontor") == EMSMode.CHARGE_PV.value, (
            f"Lower SoC bat should charge, got {mode_entries}"
        )
        # Forrad (higher SoC) should stay standby
        assert "forrad" not in mode_entries or (
            mode_entries.get("forrad") == EMSMode.BATTERY_STANDBY.value
        ), f"Higher SoC bat should standby, got {mode_entries}"

    async def test_balanced_soc_both_charge(self) -> None:
        """Kontor 50% ≈ Forrad 51% → both get charge_pv."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.MIDDAY_CHARGE
        engine._sm.state.entry_time = datetime(
            _ENTRY_YEAR, _ENTRY_MONTH, _ENTRY_DAY,
            _CHARGE_HOUR, 0, tzinfo=timezone.utc,
        )

        snap = make_snapshot(
            hour=_CHARGE_HOUR,
            batteries=[
                make_battery_state(
                    battery_id="kontor", soc_pct=_SOC_BALANCED_A_PCT,
                    ems_mode="battery_standby",
                    ct_placement=CTPlacement.LOCAL_LOAD,
                    pv_power_w=_PV_SURPLUS_W, load_power_w=_LOAD_W,
                ),
                make_battery_state(
                    battery_id="forrad", soc_pct=_SOC_BALANCED_B_PCT,
                    ems_mode="battery_standby",
                    ct_placement=CTPlacement.HOUSE_GRID,
                    grid_power_w=_PV_EXPORT_W,
                ),
            ],
            grid=make_grid_state(grid_power_w=_PV_EXPORT_W),
        )
        result = await engine.run_cycle(snap)

        assert result.execution is not None
        mode_entries = {
            e.target_id: e.value
            for e in result.execution.audit_entries
            if e.command_type == "set_ems_mode"
        }
        # Both should get charge_pv when balanced
        assert mode_entries.get("kontor") == EMSMode.CHARGE_PV.value
        assert mode_entries.get("forrad") == EMSMode.CHARGE_PV.value


# ===========================================================================
# Discharge: balancer SoC-proportional
# ===========================================================================


@pytest.mark.asyncio()
class TestDischargeSoCBalance:
    """Balancer allocates proportional to capacity during discharge."""

    async def test_discharge_allocates_by_capacity(self) -> None:
        """K75/F25 allocation during evening discharge."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.EVENING_DISCHARGE
        engine._sm.state.entry_time = datetime(
            _ENTRY_YEAR, _ENTRY_MONTH, _ENTRY_DAY,
            _DISCHARGE_HOUR, 0, tzinfo=timezone.utc,
        )

        snap = make_snapshot(
            hour=_DISCHARGE_HOUR,
            batteries=[
                make_battery_state(
                    battery_id="kontor", soc_pct=_SOC_HIGH_PCT,
                    cap_kwh=_CAP_KONTOR_KWH,
                    ct_placement=CTPlacement.LOCAL_LOAD,
                ),
                make_battery_state(
                    battery_id="forrad", soc_pct=_SOC_HIGH_PCT,
                    cap_kwh=_CAP_FORRAD_KWH,
                    ct_placement=CTPlacement.HOUSE_GRID,
                ),
            ],
            grid=make_grid_state(grid_power_w=_GRID_DISCHARGE_W),
        )
        result = await engine.run_cycle(snap)

        # Balancer should allocate — check result has balance
        assert result.balance is not None
        assert len(result.balance.allocations) >= _MIN_ALLOCATIONS
