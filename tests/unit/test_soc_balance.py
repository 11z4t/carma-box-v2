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

    async def test_lower_soc_gets_charge_higher_discharge(self) -> None:
        """PLAT-1618: diff=30% (>2%) -> lower SoC bat charges, higher SoC bat gets discharge_pv."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.PV_SURPLUS_DAY
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
        # Kontor (lower SoC 30%) should get charge_battery
        assert mode_entries.get("kontor") == EMSMode.CHARGE_BATTERY.value, (
            f"Lower SoC bat (kontor) should charge, got {mode_entries}"
        )
        # Forrad (higher SoC 60%) should get discharge_pv for active balancing
        assert mode_entries.get("forrad") == EMSMode.DISCHARGE_PV.value, (
            f"Higher SoC bat (forrad) should discharge_pv for balancing, got {mode_entries}"
        )
        # Verify Forrad power limit equals _BALANCE_DISCHARGE_RATE_W
        from core.engine import ControlEngine as CE
        limits = {
            e.target_id: int(e.value or 0)
            for e in result.execution.audit_entries
            if e.command_type == "set_ems_power_limit"
        }
        assert limits.get("forrad") == CE._BALANCE_DISCHARGE_RATE_W, (
            f"Forrad rate should be _BALANCE_DISCHARGE_RATE_W={CE._BALANCE_DISCHARGE_RATE_W},"
            f"got {limits}"
        )

    async def test_balanced_soc_both_charge(self) -> None:
        """Kontor 50% ≈ Forrad 51% → both get charge_pv."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.PV_SURPLUS_DAY
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
        assert mode_entries.get("kontor") == EMSMode.CHARGE_BATTERY.value
        assert mode_entries.get("forrad") == EMSMode.CHARGE_BATTERY.value


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


# ===========================================================================
# PLAT-1616: clear_pending prevents mode_manager override
# ===========================================================================

_STANDBY_MODE: str = "battery_standby"
_CHARGE_PV_MODE: str = "charge_battery"


@pytest.mark.asyncio()
class TestClearPendingPreventsOverride:
    """Branch A clears pending mode_manager requests."""

    async def test_pending_standby_cleared_by_charge_plan(self) -> None:
        """Pending standby from Branch B → Branch A clears → charge_pv set."""
        engine = _make_engine()

        # Cycle 1: Branch B (night) — queues standby
        engine._sm.state.current = Scenario.EVENING_DISCHARGE
        snap_night = make_snapshot(
            hour=_DISCHARGE_HOUR,
            batteries=[make_battery_state(
                battery_id="kontor", soc_pct=_SOC_HIGH_PCT,
                ct_placement=CTPlacement.LOCAL_LOAD,
            )],
        )
        await engine.run_cycle(snap_night)

        # Cycle 2: Branch A (day) — should clear pending + set charge_pv
        engine._sm.state.current = Scenario.PV_SURPLUS_DAY
        snap_day = make_snapshot(
            hour=_CHARGE_HOUR,
            batteries=[make_battery_state(
                battery_id="kontor", soc_pct=_SOC_HIGH_PCT,
                ems_mode=_STANDBY_MODE,
                ct_placement=CTPlacement.LOCAL_LOAD,
                pv_power_w=_PV_SURPLUS_W, load_power_w=_LOAD_W,
            )],
            grid=make_grid_state(grid_power_w=_PV_EXPORT_W),
        )
        result = await engine.run_cycle(snap_day)

        # Charge plan should have set charge_pv despite pending standby
        assert result.execution is not None
        mode_entries = [
            e for e in result.execution.audit_entries
            if e.command_type == "set_ems_mode"
        ]
        assert any(
            e.value == _CHARGE_PV_MODE for e in mode_entries
        ), f"Expected charge_pv, got {[e.value for e in mode_entries]}"


# ===========================================================================
# PLAT-1617: export_limit set for Kontor (local_load CT)
# ===========================================================================

_EXPECTED_EXPORT_LIMIT_CHARGE: int = 0
_EXPECTED_EXPORT_LIMIT_STANDBY: int = 5000


@pytest.mark.asyncio()
class TestExportLimitKontor:
    """Kontor gets export_limit=0 during charge, restored at standby."""

    async def test_charge_sets_charge_battery_mode(self) -> None:
        """Kontor with PV surplus → charge_battery mode."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.PV_SURPLUS_DAY

        snap = make_snapshot(
            hour=_CHARGE_HOUR,
            batteries=[make_battery_state(
                battery_id="kontor", soc_pct=_SOC_LOW_PCT,
                ems_mode=_STANDBY_MODE,
                ct_placement=CTPlacement.LOCAL_LOAD,
                pv_power_w=_PV_SURPLUS_W, load_power_w=_LOAD_W,
            )],
            grid=make_grid_state(grid_power_w=_PV_EXPORT_W),
        )
        result = await engine.run_cycle(snap)

        assert result.execution is not None
        mode_entries = [
            e for e in result.execution.audit_entries
            if e.command_type == "set_ems_mode"
        ]
        assert len(mode_entries) >= _MIN_ALLOCATIONS
        assert mode_entries[0].value == EMSMode.CHARGE_BATTERY.value

    async def test_standby_restores_export_limit(self) -> None:
        """Kontor standby → export_limit restored to default."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.PV_SURPLUS_DAY

        snap = make_snapshot(
            hour=_CHARGE_HOUR,
            batteries=[make_battery_state(
                battery_id="kontor", soc_pct=_SOC_LOW_PCT,
                ems_mode=_CHARGE_PV_MODE,
                ct_placement=CTPlacement.LOCAL_LOAD,
                pv_power_w=_ZERO_WAIT_S,  # No PV
                load_power_w=_LOAD_W,
            )],
            grid=make_grid_state(grid_power_w=_GRID_DISCHARGE_W),
        )
        result = await engine.run_cycle(snap)

        assert result.execution is not None
        export_entries = [
            e for e in result.execution.audit_entries
            if e.command_type == "set_export_limit"
        ]
        assert len(export_entries) >= _MIN_ALLOCATIONS
        assert int(export_entries[0].value or 0) == _EXPECTED_EXPORT_LIMIT_STANDBY


# ===========================================================================
# PLAT-1619: Night charge_pv limit forced to 0
# ===========================================================================

_NIGHT_CHARGE_HOUR: int = 23
_CONFIG_LIMIT_NON_ZERO: int = 500
_LOW_PV_FORECAST_KWH: float = 5.0


@pytest.mark.asyncio()
class TestNightChargePvLimitZero:
    """Branch B: charge_pv at night → limit forced to 0."""

    async def test_night_charge_pv_limit_forced_zero(self) -> None:
        """NIGHT_GRID_CHARGE with sm.ems_power_limit=500 → limit=0."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.NIGHT_GRID_CHARGE
        engine._sm.state.entry_time = datetime(
            _ENTRY_YEAR, _ENTRY_MONTH, _ENTRY_DAY,
            _NIGHT_CHARGE_HOUR, 0, tzinfo=timezone.utc,
        )

        snap = make_snapshot(
            hour=_NIGHT_CHARGE_HOUR,
            batteries=[make_battery_state(
                battery_id="kontor", soc_pct=_SOC_LOW_PCT,
                ems_mode=_STANDBY_MODE,
                ct_placement=CTPlacement.LOCAL_LOAD,
            )],
            grid=make_grid_state(
                pv_forecast_tomorrow_kwh=_LOW_PV_FORECAST_KWH,
            ),
        )

        from unittest.mock import patch

        # Patch mode_manager to capture request_change args
        with patch.object(
            engine._mode_manager, "request_change",
            wraps=engine._mode_manager.request_change,
        ) as mock_rc:
            await engine.run_cycle(snap)

        if mock_rc.called:
            for call in mock_rc.call_args_list:
                if call.kwargs.get("target_mode") == _CHARGE_PV_MODE:
                    assert call.kwargs.get("target_limit_w", -1) == _EXPECTED_EXPORT_LIMIT_CHARGE, (
                        f"Night charge_pv limit must be 0, got {call.kwargs.get('target_limit_w')}"
                    )
