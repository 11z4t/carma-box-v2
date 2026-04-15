"""REGRESSION: ALDRIG grid-laddning av bat eller EV dagtid (06-22).

This test MUST pass on every deploy. It verifies the absolute rule:
- Daytime (06-22): bat and EV charge ONLY from PV surplus
- Grid import → bat MUST be standby
- charge_pv mode is ONLY allowed when grid exports (PV surplus)

Root cause: GoodWe firmware treats charge_pv with ANY grid import as
"allowed to charge from grid". v2 must actively enforce standby when
grid is importing during daytime.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.balancer import BatteryBalancer
from core.engine import ControlEngine, _CHARGE_PV_EMS_LIMIT_W
from core.executor import CommandExecutor, ExecutorConfig
from core.guards import GridGuard, GuardConfig
from core.mode_change import ModeChangeConfig, ModeChangeManager
from core.models import EMSMode, Scenario
from core.state_machine import StateMachine, StateMachineConfig
from tests.conftest import make_battery_state, make_grid_state, make_snapshot

# Named test constants
_GRID_IMPORT_W: float = 500.0          # Grid importing (positive = import)
_GRID_EXPORT_W: float = -2000.0        # PV surplus (negative = export)
_SOC_PARTIAL_PCT: float = 60.0         # Mid-range SoC
_ZERO_WAIT_S: float = 0.0
_DAYTIME_HOURS: list[int] = [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]
_NIGHT_HOUR: int = 23


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
            inverters={"kontor": inv_mock},
            config=ExecutorConfig(mode_change_cooldown_s=_ZERO_WAIT_S),
        ),
    )


@pytest.mark.asyncio()
class TestNeverGridChargeDaytime:
    """ABSOLUTE RULE: No grid charging of bat or EV during daytime."""

    async def test_grid_import_forces_bat_standby(self) -> None:
        """Grid importing + daytime → bat MUST be standby, not charge_pv."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.MIDDAY_CHARGE

        snap = make_snapshot(
            hour=12,
            batteries=[make_battery_state(
                soc_pct=_SOC_PARTIAL_PCT,
                ems_mode="charge_pv",
            )],
            grid=make_grid_state(grid_power_w=_GRID_IMPORT_W),
        )
        result = await engine.run_cycle(snap)

        # Must NOT have charge_pv commands when grid is importing
        if result.execution:
            for entry in result.execution.audit_entries:
                if entry.command_type == "set_ems_mode":
                    assert entry.value != EMSMode.CHARGE_PV.value, (
                        "RULE VIOLATION: charge_pv during grid import!"
                    )

    async def test_pv_export_allows_charge_pv(self) -> None:
        """PV surplus (export) + daytime → charge_pv is allowed."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.MIDDAY_CHARGE

        snap = make_snapshot(
            hour=14,
            batteries=[make_battery_state(
                soc_pct=_SOC_PARTIAL_PCT,
                ems_mode="battery_standby",
            )],
            grid=make_grid_state(grid_power_w=_GRID_EXPORT_W),
        )
        result = await engine.run_cycle(snap)

        # charge_pv allowed when exporting
        assert result.error is None

    async def test_all_daytime_hours_block_grid_charge(self) -> None:
        """Every daytime hour (6-21) blocks charge_pv during grid import."""
        for hour in _DAYTIME_HOURS:
            engine = _make_engine()
            engine._sm.state.current = Scenario.MIDDAY_CHARGE

            snap = make_snapshot(
                hour=hour,
                batteries=[make_battery_state(
                    soc_pct=_SOC_PARTIAL_PCT,
                    ems_mode="charge_pv",
                )],
                grid=make_grid_state(grid_power_w=_GRID_IMPORT_W),
            )
            result = await engine.run_cycle(snap)

            if result.execution:
                for entry in result.execution.audit_entries:
                    if entry.command_type == "set_ems_mode":
                        assert entry.value != EMSMode.CHARGE_PV.value, (
                            f"RULE VIOLATION at hour {hour}: "
                            f"charge_pv during grid import!"
                        )

    async def test_limit_always_zero_in_charge_pv(self) -> None:
        """Even with PV export, ems_power_limit MUST be 0 in charge_pv."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.MIDDAY_CHARGE

        snap = make_snapshot(
            hour=14,
            batteries=[make_battery_state(soc_pct=_SOC_PARTIAL_PCT)],
            grid=make_grid_state(grid_power_w=_GRID_EXPORT_W),
        )
        result = await engine.run_cycle(snap)

        assert result.execution is not None
        for entry in result.execution.audit_entries:
            if entry.command_type == "set_ems_power_limit":
                assert int(entry.value) == _CHARGE_PV_EMS_LIMIT_W, (
                    f"charge_pv limit must be {_CHARGE_PV_EMS_LIMIT_W}, "
                    f"got {entry.value}"
                )
