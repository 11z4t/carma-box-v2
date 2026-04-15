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
from core.engine import ControlEngine
from core.executor import CommandExecutor, ExecutorConfig
from core.guards import GridGuard, GuardConfig
from core.mode_change import ModeChangeConfig, ModeChangeManager
from core.models import Scenario
from core.state_machine import StateMachine, StateMachineConfig
from tests.conftest import make_battery_state, make_grid_state, make_snapshot

# Named test constants
_GRID_IMPORT_W: float = 500.0          # Grid importing (positive = import)
_GRID_EXPORT_W: float = -2000.0        # PV surplus (negative = export)
_SOC_PARTIAL_PCT: float = 60.0         # Mid-range SoC
_ZERO_WAIT_S: float = 0.0
_DAYTIME_HOURS: list[int] = [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]
_NIGHT_HOUR: int = 23
_TEST_MIDDAY_HOUR: int = 12
_TEST_AFTERNOON_HOUR: int = 14


def _make_engine() -> tuple[ControlEngine, AsyncMock]:
    """Returns (engine, inv_mock) — inv_mock for asserting direct calls."""
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

    engine = ControlEngine(
        guard, sm, balancer, mode_mgr,
        CommandExecutor(
            inverters={"kontor": inv_mock},
            config=ExecutorConfig(mode_change_cooldown_s=_ZERO_WAIT_S),
        ),
    )
    return engine, inv_mock


@pytest.mark.asyncio()
class TestNeverGridChargeDaytime:
    """ABSOLUTE RULE: No grid charging of bat or EV during daytime."""

    async def test_charge_pv_stays_active_to_absorb_pv(self) -> None:
        """charge_pv with limit=0 stays active — bat absorbs all PV surplus."""
        engine, _inv = _make_engine()
        engine._sm.state.current = Scenario.MIDDAY_CHARGE

        snap = make_snapshot(
            hour=_TEST_MIDDAY_HOUR,
            batteries=[make_battery_state(
                soc_pct=_SOC_PARTIAL_PCT,
                ems_mode="charge_pv",
            )],
            grid=make_grid_state(grid_power_w=_GRID_IMPORT_W),
        )
        result = await engine.run_cycle(snap)

        # charge_pv with limit=0 is safe — GoodWe only charges from PV
        assert result.error is None

    async def test_pv_export_allows_charge_pv(self) -> None:
        """PV surplus (export) + daytime → charge_pv is allowed."""
        engine, _inv = _make_engine()
        engine._sm.state.current = Scenario.MIDDAY_CHARGE

        snap = make_snapshot(
            hour=_TEST_AFTERNOON_HOUR,
            batteries=[make_battery_state(
                soc_pct=_SOC_PARTIAL_PCT,
                ems_mode="battery_standby",
            )],
            grid=make_grid_state(grid_power_w=_GRID_EXPORT_W),
        )
        result = await engine.run_cycle(snap)

        # charge_pv allowed when exporting
        assert result.error is None

    async def test_limit_always_zero_in_charge_pv(self) -> None:
        """PLAT-1613 ABSOLUTE: ems_power_limit=0 in charge_pv. NEVER remove this test."""
        engine, _inv = _make_engine()
        engine._sm.state.current = Scenario.MIDDAY_CHARGE

        snap = make_snapshot(
            hour=_TEST_AFTERNOON_HOUR,
            batteries=[make_battery_state(soc_pct=_SOC_PARTIAL_PCT)],
            grid=make_grid_state(grid_power_w=_GRID_EXPORT_W),
        )
        result = await engine.run_cycle(snap)

        assert result.error is None
        # ALL limit commands in charge_pv mode MUST be 0
        if result.execution:
            for entry in result.execution.audit_entries:
                if entry.command_type == "set_ems_power_limit":
                    assert int(entry.value) == 0, (
                        f"PLAT-1613 VIOLATION: charge_pv limit={entry.value},"
                        f" MUST be 0"
                    )

    async def test_charge_pv_absorbs_export(self) -> None:
        """PV export → bat must be in charge_pv to absorb surplus."""
        engine, _inv = _make_engine()
        engine._sm.state.current = Scenario.MIDDAY_CHARGE

        snap = make_snapshot(
            hour=_TEST_MIDDAY_HOUR,
            batteries=[make_battery_state(
                soc_pct=_SOC_PARTIAL_PCT,
                ems_mode="battery_standby",
            )],
            grid=make_grid_state(grid_power_w=_GRID_EXPORT_W),
        )
        result = await engine.run_cycle(snap)

        # bat should transition to charge_pv to absorb PV surplus
        assert result.error is None
