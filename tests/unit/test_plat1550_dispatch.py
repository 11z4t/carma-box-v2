"""Tests for PLAT-1550: Per-battery mode dispatch + SoC-100 standby guard.

CRITICAL BUG (fixed): engine was sending mode change to generic 'scenario'
battery_id instead of iterating snapshot.batteries → Förråd inverter never
received commands.

Guard tests — fail on pre-fix code, pass on fixed code.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine
from unittest.mock import AsyncMock, patch

from core.balancer import BatteryBalancer
from core.engine import ControlEngine
from core.executor import CommandExecutor, ExecutorConfig
from core.guards import GridGuard, GuardConfig
from core.mode_change import ModeChangeConfig, ModeChangeManager
from core.models import CTPlacement, EMSMode, Scenario
from core.state_machine import StateMachine, StateMachineConfig
from tests.conftest import make_battery_state, make_snapshot


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_engine(
    mode_mgr: ModeChangeManager,
    start_scenario: Scenario = Scenario.NIGHT_LOW_PV,
) -> ControlEngine:
    """Engine with real components but injected mode manager.

    Starts SM in NIGHT_LOW_PV by default so that a daytime snapshot
    triggers a scenario transition → request_change is called.
    """
    guard = GridGuard(GuardConfig())
    sm = StateMachine(StateMachineConfig(min_dwell_s=0, start_scenario=start_scenario))
    balancer = BatteryBalancer()
    inv_mock = AsyncMock()
    inv_mock.set_ems_mode = AsyncMock(return_value=True)
    inv_mock.set_ems_power_limit = AsyncMock(return_value=True)
    inv_mock.set_fast_charging = AsyncMock(return_value=True)
    inv_mock.get_fast_charging = AsyncMock(return_value=False)
    inv_mock.get_ems_mode = AsyncMock(return_value="battery_standby")
    executor = CommandExecutor(
        inverters={"kontor": inv_mock, "forrad": inv_mock},
        config=ExecutorConfig(mode_change_cooldown_s=0),
    )
    return ControlEngine(guard, sm, balancer, mode_mgr, executor)


# ===========================================================================
# Per-battery dispatch — core guard tests
# ===========================================================================


class TestPerBatteryDispatch:
    """Mode change requests must be issued for EVERY battery in snapshot."""

    def test_both_batteries_queued_for_mode_change(self) -> None:
        """PLAT-1550: request_change must be called for kontor AND forrad."""
        mode_mgr = ModeChangeManager(
            ModeChangeConfig(
                clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0
            )
        )
        engine = _make_engine(mode_mgr)

        bat_kontor = make_battery_state(
            battery_id="kontor", soc_pct=60.0, ct_placement=CTPlacement.LOCAL_LOAD
        )
        bat_forrad = make_battery_state(
            battery_id="forrad", soc_pct=60.0, ct_placement=CTPlacement.HOUSE_GRID
        )
        snap = make_snapshot(
            batteries=[bat_kontor, bat_forrad],
            current_scenario=Scenario.MIDDAY_CHARGE,
            hour=12,
        )

        with patch.object(mode_mgr, "request_change") as mock_request:
            _run(engine.run_cycle(snap))

        called_battery_ids = {c.kwargs["battery_id"] for c in mock_request.call_args_list}
        assert "kontor" in called_battery_ids, "Kontor must receive mode change request"
        assert "forrad" in called_battery_ids, (
            "Förråd must receive mode change request (PLAT-1550 regression)"
        )

    def test_forrad_queued_for_discharge_scenario(self) -> None:
        """Förråd must be queued with discharge_pv during discharge scenario."""
        mode_mgr = ModeChangeManager(
            ModeChangeConfig(
                clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0
            )
        )
        engine = _make_engine(mode_mgr)

        bat_kontor = make_battery_state(
            battery_id="kontor", soc_pct=60.0, ct_placement=CTPlacement.LOCAL_LOAD
        )
        bat_forrad = make_battery_state(
            battery_id="forrad", soc_pct=60.0, ct_placement=CTPlacement.HOUSE_GRID
        )
        snap = make_snapshot(
            batteries=[bat_kontor, bat_forrad],
            current_scenario=Scenario.MORNING_DISCHARGE,
            hour=7,
        )

        with patch.object(mode_mgr, "request_change") as mock_request:
            _run(engine.run_cycle(snap))

        forrad_calls = [
            c for c in mock_request.call_args_list if c.kwargs.get("battery_id") == "forrad"
        ]
        assert len(forrad_calls) >= 1, "Förråd must be dispatched during discharge scenario"
        assert forrad_calls[0].kwargs["target_mode"] == EMSMode.DISCHARGE_PV.value


# ===========================================================================
# SoC 100% → standby guard
# ===========================================================================


class TestSoC100Standby:
    """Battery at 100% SoC must receive standby, not charge_pv."""

    def test_full_battery_gets_standby_not_charge(self) -> None:
        """PLAT-1550: SoC 100% + charge scenario → standby to prevent autonomous grid-charge."""
        mode_mgr = ModeChangeManager(
            ModeChangeConfig(
                clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0
            )
        )
        engine = _make_engine(mode_mgr)

        bat_full = make_battery_state(
            battery_id="kontor",
            soc_pct=100.0,
            ct_placement=CTPlacement.LOCAL_LOAD,
        )
        snap = make_snapshot(
            batteries=[bat_full],
            current_scenario=Scenario.MIDDAY_CHARGE,
            hour=12,
        )

        with patch.object(mode_mgr, "request_change") as mock_request:
            _run(engine.run_cycle(snap))

        assert mock_request.called, "request_change must be called even at 100% SoC"
        target_mode = mock_request.call_args.kwargs["target_mode"]
        assert target_mode == EMSMode.BATTERY_STANDBY.value, (
            f"Full battery must get standby, not {target_mode}"
        )

    def test_partial_battery_gets_charge_mode(self) -> None:
        """Battery at 60% SoC must receive charge_pv during charge scenario."""
        mode_mgr = ModeChangeManager(
            ModeChangeConfig(
                clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0
            )
        )
        engine = _make_engine(mode_mgr)

        bat_partial = make_battery_state(
            battery_id="kontor",
            soc_pct=60.0,
            ct_placement=CTPlacement.LOCAL_LOAD,
        )
        snap = make_snapshot(
            batteries=[bat_partial],
            current_scenario=Scenario.MIDDAY_CHARGE,
            hour=12,
        )

        with patch.object(mode_mgr, "request_change") as mock_request:
            _run(engine.run_cycle(snap))

        assert mock_request.called
        target_mode = mock_request.call_args.kwargs["target_mode"]
        assert target_mode == EMSMode.CHARGE_PV.value, (
            f"60% SoC battery must get charge_pv, got {target_mode}"
        )


# ===========================================================================
# Regression: single-battery site
# ===========================================================================


class TestSingleBatteryRegression:
    """Single-battery sites must continue working after per-battery dispatch fix."""

    def test_single_battery_queued_for_mode_change(self) -> None:
        """Single battery site: request_change must be called for that battery."""
        mode_mgr = ModeChangeManager(
            ModeChangeConfig(
                clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0
            )
        )
        engine = _make_engine(mode_mgr)

        snap = make_snapshot(
            batteries=[make_battery_state(battery_id="kontor", soc_pct=60.0)],
            current_scenario=Scenario.MIDDAY_CHARGE,
            hour=12,
        )

        with patch.object(mode_mgr, "request_change") as mock_request:
            result = _run(engine.run_cycle(snap))

        assert result.error is None
        called_ids = {c.kwargs["battery_id"] for c in mock_request.call_args_list}
        assert "kontor" in called_ids
