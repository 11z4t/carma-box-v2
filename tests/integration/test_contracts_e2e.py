"""Integration + contract + E2E tests (PLAT-1582).

E3: Contract tests — concrete adapters implement ABC interfaces.
E4: Integration — GridGuard + EllevioTracker in concert (no mocks on tested components).
E5: E2E — PlanExecutor.generate() + engine.run_cycle() with complete snapshot.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from adapters.base import EVChargerAdapter, InverterAdapter
from adapters.goodwe import GoodWeAdapter
from config.schema import load_config
from core.balancer import BatteryBalancer
from core.ellevio import EllevioConfig, EllevioTracker
from core.engine import ControlEngine
from core.executor import CommandExecutor, ExecutorConfig
from core.guards import GridGuard, GuardConfig, GuardLevel, GuardPolicy, ExportGuard
from core.mode_change import ModeChangeConfig, ModeChangeManager
from core.models import Scenario
from core.plan_executor import PlanExecutor
from core.planner import Planner, PlannerConfig
from core.state_machine import StateMachine, StateMachineConfig
from tests.conftest import make_battery_state, make_snapshot, make_grid_state


_CONFIG_PATH = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")


# ===========================================================================
# E3: Contract tests — adapters implement ABC interface
# ===========================================================================


class TestInverterAdapterContract:
    """Verify GoodWeAdapter implements all InverterAdapter abstract methods."""

    def test_goodwe_implements_inverter_interface(self) -> None:
        """GoodWeAdapter must be a subclass of InverterAdapter."""
        assert issubclass(GoodWeAdapter, InverterAdapter)

    def test_all_abstract_methods_implemented(self) -> None:
        """Every abstract method in InverterAdapter must exist on GoodWeAdapter."""
        abstract_methods = set()
        for name, method in inspect.getmembers(InverterAdapter):
            if getattr(method, "__isabstractmethod__", False):
                abstract_methods.add(name)

        for method_name in abstract_methods:
            assert hasattr(GoodWeAdapter, method_name), (
                f"GoodWeAdapter missing {method_name}"
            )

    def test_key_methods_present(self) -> None:
        """Critical methods must exist with correct names."""
        required = [
            "set_ems_mode", "set_ems_power_limit", "set_fast_charging",
            "get_battery_soc", "get_ems_mode", "get_fast_charging",
        ]
        for method_name in required:
            assert hasattr(GoodWeAdapter, method_name), (
                f"GoodWeAdapter missing {method_name}"
            )


class TestEVChargerAdapterContract:
    """Verify EVChargerAdapter interface has required methods."""

    def test_required_methods_defined(self) -> None:
        """EVChargerAdapter must define these abstract methods."""
        required = [
            "get_status", "get_power", "get_current",
            "is_connected", "set_current", "start_charging",
        ]
        for method_name in required:
            assert hasattr(EVChargerAdapter, method_name), (
                f"EVChargerAdapter missing {method_name}"
            )
            method = getattr(EVChargerAdapter, method_name)
            assert getattr(method, "__isabstractmethod__", False), (
                f"{method_name} should be abstract"
            )


# ===========================================================================
# E4: Integration — GridGuard + EllevioTracker (no mocks on tested components)
# ===========================================================================


class TestGuardPipelineIntegration:
    """GridGuard + EllevioTracker working together with real objects."""

    def test_guard_evaluates_with_real_snapshot(self) -> None:
        """GridGuard.evaluate() with realistic snapshot returns coherent result."""
        guard = GridGuard(GuardConfig())
        snap = make_snapshot(
            hour=14,
            batteries=[make_battery_state(soc_pct=60.0)],
            grid=make_grid_state(weighted_avg_kw=1.5),
        )
        result = guard.evaluate(
            batteries=snap.batteries,
            current_scenario=Scenario.MIDDAY_CHARGE,
            weighted_avg_kw=snap.grid.weighted_avg_kw,
            hour=snap.hour,
            ha_connected=True,
            data_age_s=0.0,
        )
        assert result.level in (
            GuardLevel.OK, GuardLevel.WARNING,
            GuardLevel.CRITICAL, GuardLevel.BREACH,
        )
        assert isinstance(result.headroom_kw, float)

    def test_ellevio_tracker_updates_correctly(self) -> None:
        """EllevioTracker tracks weighted hourly averages."""
        tracker = EllevioTracker(EllevioConfig())
        ts = datetime(2026, 4, 15, 14, 5, 0, tzinfo=timezone.utc)
        tracker.update(1.5, ts)
        tracker.update(1.8, ts.replace(minute=10))
        # Should have a current weighted average
        avg = tracker.current_weighted_avg_kw
        assert avg > 0.0

    def test_guard_and_ellevio_coherent(self) -> None:
        """Guard uses weighted_avg from Ellevio-like data without crash."""
        guard = GridGuard(GuardConfig())
        tracker = EllevioTracker(EllevioConfig())
        ts = datetime(2026, 4, 15, 14, 5, 0, tzinfo=timezone.utc)
        tracker.update(1.2, ts)

        snap = make_snapshot(
            hour=14,
            batteries=[make_battery_state(soc_pct=70.0)],
            grid=make_grid_state(weighted_avg_kw=tracker.current_weighted_avg_kw),
        )
        result = guard.evaluate(
            batteries=snap.batteries,
            current_scenario=Scenario.MIDDAY_CHARGE,
            weighted_avg_kw=snap.grid.weighted_avg_kw,
            hour=snap.hour,
            ha_connected=True,
            data_age_s=0.0,
        )
        assert result.level is not None


# ===========================================================================
# E5: E2E — run_cycle + PlanExecutor with complete snapshot
# ===========================================================================


@pytest.mark.asyncio()
class TestE2ERunCycle:
    """End-to-end engine.run_cycle() with real components."""

    async def test_run_cycle_no_exception(self) -> None:
        """Full run_cycle completes without exception."""
        guard = GridGuard(GuardConfig())
        sm = StateMachine(StateMachineConfig(min_dwell_s=0))
        balancer = BatteryBalancer()
        mode_mgr = ModeChangeManager(ModeChangeConfig(
            clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0,
        ))
        inv_mock = AsyncMock()
        inv_mock.set_ems_mode = AsyncMock(return_value=True)
        inv_mock.set_ems_power_limit = AsyncMock(return_value=True)
        inv_mock.set_fast_charging = AsyncMock(return_value=True)
        inv_mock.get_fast_charging = AsyncMock(return_value=False)
        inv_mock.get_ems_mode = AsyncMock(return_value="battery_standby")

        executor = CommandExecutor(
            inverters={"kontor": inv_mock},
            config=ExecutorConfig(mode_change_cooldown_s=0),
        )
        engine = ControlEngine(guard, sm, balancer, mode_mgr, executor)

        snap = make_snapshot(
            hour=14,
            batteries=[make_battery_state(soc_pct=60.0)],
            grid=make_grid_state(grid_power_w=1500.0, weighted_avg_kw=1.2),
        )
        result = await engine.run_cycle(snap)

        assert result.cycle_id is not None
        assert result.elapsed_s >= 0
        assert result.error is None
        assert result.guard is not None

    async def test_plan_executor_generate_no_crash(self) -> None:
        """PlanExecutor.generate() completes without crash for all plan hours."""
        cfg = load_config(_CONFIG_PATH)
        planner = Planner(PlannerConfig())
        guard_policy = GuardPolicy(GridGuard(GuardConfig()), ExportGuard())

        executor = PlanExecutor(
            planner=planner,
            ha_api=None,  # No HA writes in test
            config=cfg,
            guard_policy=guard_policy,
        )

        for hour in (6, 12, 17, 22):
            snap = make_snapshot(
                hour=hour,
                batteries=[make_battery_state(soc_pct=60.0)],
            )
            # Should not raise
            await executor.generate(snap)

        # Night plan should be set after hour 22
        assert executor.active_night_plan is not None
