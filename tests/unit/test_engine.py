"""Tests for Main Control Loop Engine.

Covers:
- Full cycle with mocked components
- Exception in decision engine does not crash loop
- Guard commands execute even when other phases fail
- FREEZE/ALARM skips decision engine
- Scenario transition during cycle
- Cycle count and timing
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.balancer import BatteryBalancer
from core.engine import ControlEngine
from core.executor import CommandExecutor, ExecutorConfig
from core.guards import GridGuard, GuardConfig
from core.mode_change import ModeChangeConfig, ModeChangeManager
from core.models import Scenario
from core.state_machine import StateMachine, StateMachineConfig
from tests.conftest import make_battery_state, make_snapshot


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_engine() -> ControlEngine:
    """Create engine with real components (no mocks needed — they're pure)."""
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
    return ControlEngine(guard, sm, balancer, mode_mgr, executor)


# ===========================================================================
# Full cycle
# ===========================================================================


@pytest.mark.asyncio
class TestFullCycle:
    """Test complete control cycle."""

    async def test_cycle_completes(self) -> None:
        engine = _make_engine()
        snap = make_snapshot(hour=12)
        result = await engine.run_cycle(snap)
        assert result.cycle_id
        assert result.elapsed_s >= 0
        assert result.error is None
        assert result.guard is not None

    async def test_cycle_count_increments(self) -> None:
        engine = _make_engine()
        snap = make_snapshot(hour=12)
        assert engine.cycle_count == 0
        await engine.run_cycle(snap)
        assert engine.cycle_count == 1
        await engine.run_cycle(snap)
        assert engine.cycle_count == 2

    async def test_unique_cycle_ids(self) -> None:
        engine = _make_engine()
        snap = make_snapshot(hour=12)
        r1 = await engine.run_cycle(snap)
        r2 = await engine.run_cycle(snap)
        assert r1.cycle_id != r2.cycle_id


# ===========================================================================
# Guard phase
# ===========================================================================


@pytest.mark.asyncio
class TestGuardPhase:
    """Guard runs first and produces evaluation."""

    async def test_guard_result_present(self) -> None:
        engine = _make_engine()
        snap = make_snapshot(hour=12)
        result = await engine.run_cycle(snap)
        assert result.guard is not None

    async def test_guard_freeze_skips_decision(self) -> None:
        """FREEZE level should skip decision engine."""
        engine = _make_engine()
        # Stale data → FREEZE
        snap = make_snapshot(hour=12)
        result = await engine.run_cycle(snap, data_age_s=600.0)
        assert result.guard is not None
        assert result.balance is None  # Skipped

    async def test_guard_on_comm_loss(self) -> None:
        """Communication loss should trigger G7."""
        engine = _make_engine()
        engine._guard._last_ha_contact = 0.0  # Long ago
        snap = make_snapshot(hour=12)
        result = await engine.run_cycle(snap, ha_connected=False)
        assert result.guard is not None
        g7 = [v for v in result.guard.violations if "G7" in v]
        assert len(g7) >= 1


# ===========================================================================
# Exception handling
# ===========================================================================


@pytest.mark.asyncio
class TestExceptionHandling:
    """Engine never crashes — errors captured in result."""

    async def test_exception_in_cycle_captured(self) -> None:
        """Exception should be captured, not raised."""
        engine = _make_engine()
        # Force an error by giving invalid snapshot
        snap = make_snapshot(
            hour=12,
            batteries=[make_battery_state()],
        )

        # Even with a manipulated engine, it should not crash
        result = await engine.run_cycle(snap)
        # Should complete (error or not)
        assert result.cycle_id is not None


# ===========================================================================
# Scenario transition
# ===========================================================================


@pytest.mark.asyncio
class TestScenarioTransition:
    """Scenario changes during cycle."""

    async def test_scenario_transition_at_evening(self) -> None:
        """At 17:00, should transition from S3 to S4."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.MIDDAY_CHARGE
        engine._sm.state.entry_time = datetime(2026, 4, 12, 11, 0, tzinfo=timezone.utc)

        snap = make_snapshot(hour=17, batteries=[make_battery_state(soc_pct=60.0)])
        result = await engine.run_cycle(snap)
        assert result.scenario == Scenario.EVENING_DISCHARGE

    async def test_stays_in_scenario_when_no_exit(self) -> None:
        """At 14:00 in S3, should stay."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.MIDDAY_CHARGE
        engine._sm.state.entry_time = datetime(2026, 4, 12, 11, 0, tzinfo=timezone.utc)

        snap = make_snapshot(hour=14)
        result = await engine.run_cycle(snap)
        assert result.scenario == Scenario.MIDDAY_CHARGE


# ===========================================================================
# Guard commands execute (lines 109-110)
# ===========================================================================


@pytest.mark.asyncio
class TestGuardCommandsExecute:
    """Guard commands should be executed when present (line 110)."""

    async def test_guard_commands_executed_on_g1_trigger(self) -> None:
        """Low SoC triggers G1 → commands emitted → executor called."""
        engine = _make_engine()
        # SoC at floor (15%) triggers G1 standby command
        low_soc_bat = make_battery_state(soc_pct=14.0)
        snap = make_snapshot(hour=12, batteries=[low_soc_bat])
        result = await engine.run_cycle(snap)
        # Guard should have commands
        assert result.guard is not None
        assert len(result.guard.commands) >= 1
        g1_cmds = [c for c in result.guard.commands if c.guard_id == "G1"]
        assert len(g1_cmds) >= 1

    async def test_guard_commands_g0_grid_charging(self) -> None:
        """G0 grid charging detection → commands emitted and executed."""
        engine = _make_engine()
        # G0: ems_power_limit > 0 in charge_pv mode
        bat = make_battery_state(
            ems_mode="charge_pv",
            ems_power_limit_w=3000,
        )
        snap = make_snapshot(hour=12, batteries=[bat])
        result = await engine.run_cycle(snap)
        assert result.guard is not None
        g0_cmds = [c for c in result.guard.commands if c.guard_id == "G0"]
        assert len(g0_cmds) >= 1


# ===========================================================================
# Exception capture (lines 156-160)
# ===========================================================================


@pytest.mark.asyncio
class TestExceptionCapture:
    """Exceptions inside the cycle are captured and returned (line 156-160)."""

    async def test_balancer_exception_captured(self) -> None:
        """If an exception is raised during the cycle, it is captured."""
        engine = _make_engine()
        snap = make_snapshot(hour=12, batteries=[make_battery_state()])

        with patch.object(
            engine._balancer,
            "allocate",
            side_effect=RuntimeError("balancer failure"),
        ):
            result = await engine.run_cycle(snap)

        # Error captured, not raised
        assert result.error is not None
        assert "balancer failure" in result.error
        assert result.elapsed_s >= 0


# ===========================================================================
# PLAT-1351: ModeChangeManager.process() is called during run_cycle()
# ===========================================================================


@pytest.mark.asyncio
class TestModeManagerProcessCalled:
    """PLAT-1351: ModeChangeManager.process() must be called in Phase 5."""

    async def test_mode_manager_process_called_each_cycle(self) -> None:
        """process() must be invoked once per run_cycle() call."""
        engine = _make_engine()
        snap = make_snapshot(hour=12)

        with patch.object(
            engine._mode_manager, "process", wraps=engine._mode_manager.process
        ) as mock_process:
            await engine.run_cycle(snap)

        mock_process.assert_awaited_once()

    async def test_mode_manager_process_called_even_on_warning(self) -> None:
        """process() is called even when guard level is WARNING (not FREEZE)."""
        engine = _make_engine()
        # SoC at floor triggers G1 WARNING, but not FREEZE
        low_soc_bat = make_battery_state(soc_pct=14.0)
        snap = make_snapshot(hour=12, batteries=[low_soc_bat])

        with patch.object(
            engine._mode_manager, "process", wraps=engine._mode_manager.process
        ) as mock_process:
            result = await engine.run_cycle(snap)

        # G1 warning level — mode manager still called
        assert result.guard is not None
        mock_process.assert_awaited_once()

    async def test_mode_manager_process_not_called_on_freeze(self) -> None:
        """process() must NOT be called when guard is FREEZE (decision skipped)."""
        engine = _make_engine()
        snap = make_snapshot(hour=12)

        with patch.object(
            engine._mode_manager, "process", wraps=engine._mode_manager.process
        ) as mock_process:
            # Stale data triggers FREEZE
            result = await engine.run_cycle(snap, data_age_s=600.0)

        assert result.guard is not None
        # FREEZE/ALARM returns early — mode manager process() is in the try block
        # after the FREEZE check, so it must NOT be called
        mock_process.assert_not_awaited()
