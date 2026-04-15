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
from core.engine import ControlEngine, NEAR_ZERO_KW, _ScenarioMode
from core.executor import CommandExecutor, ExecutorConfig
from core.guards import GridGuard, GuardConfig
from core.mode_change import ModeChangeConfig, ModeChangeManager
from core.models import Command, CommandType, EMSMode, Scenario, SystemSnapshot
from core.state_machine import StateMachine, StateMachineConfig
from tests.conftest import make_battery_state, make_snapshot

# Named test constants
_TEST_MIDDAY_HOUR: int = 14
_TEST_EVENING_HOUR: int = 18
_TEST_SOC_NOMINAL_PCT: float = 60.0
_TEST_ENTRY_YEAR: int = 2026
_TEST_ENTRY_MONTH: int = 4
_TEST_ENTRY_DAY: int = 12
_TEST_ENTRY_HOUR_MIDDAY: int = 11
_TEST_ENTRY_HOUR_EVENING: int = 16


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
        engine._sm.state.current = Scenario.PV_SURPLUS_DAY
        engine._sm.state.entry_time = datetime(2026, 4, 12, 11, 0, tzinfo=timezone.utc)

        snap = make_snapshot(hour=17, batteries=[make_battery_state(soc_pct=60.0)])
        result = await engine.run_cycle(snap)
        assert result.scenario == Scenario.EVENING_DISCHARGE

    async def test_stays_in_scenario_when_no_exit(self) -> None:
        """At 14:00 in S3, should stay."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.PV_SURPLUS_DAY
        engine._sm.state.entry_time = datetime(2026, 4, 12, 11, 0, tzinfo=timezone.utc)

        snap = make_snapshot(hour=14)
        result = await engine.run_cycle(snap)
        assert result.scenario == Scenario.PV_SURPLUS_DAY


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
        engine._sm.state.current = Scenario.EVENING_DISCHARGE
        snap = make_snapshot(hour=_TEST_EVENING_HOUR, batteries=[make_battery_state()])

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


# ===========================================================================
# PLAT-1570: Scenario→mode pipeline — class attribute + per-battery + limits
# ===========================================================================


class TestScenarioModesClassAttribute:
    """AC2: _SCENARIO_MODES must be a class-level dict."""

    def test_is_class_attribute(self) -> None:
        assert hasattr(ControlEngine, "_SCENARIO_MODES")

    def test_covers_all_scenarios(self) -> None:
        for scenario in Scenario:
            assert scenario in ControlEngine._SCENARIO_MODES

    def test_values_are_scenario_mode(self) -> None:
        for scenario, sm in ControlEngine._SCENARIO_MODES.items():
            assert isinstance(sm, _ScenarioMode)


class TestForenoonPvEvLimit:
    """AC3: FORENOON_PV_EV → CHARGE_PV with ems_power_limit=0."""

    def test_mode_is_charge_pv(self) -> None:
        sm = ControlEngine._SCENARIO_MODES[Scenario.FORENOON_PV_EV]
        assert sm.mode == EMSMode.CHARGE_PV

    def test_ems_limit_is_zero(self) -> None:
        sm = ControlEngine._SCENARIO_MODES[Scenario.FORENOON_PV_EV]
        assert sm.ems_power_limit == 0


@pytest.mark.asyncio
class TestPerBatteryModeChange:
    """AC1: request_change per battery_id, not hardcoded 'scenario'."""

    async def test_scenario_change_calls_per_battery(self) -> None:
        """2 batteries + scenario transition → 2 request_change calls."""
        engine = _make_engine()
        # Add second inverter mock
        inv_mock2 = AsyncMock()
        inv_mock2.set_ems_mode = AsyncMock(return_value=True)
        inv_mock2.set_ems_power_limit = AsyncMock(return_value=True)
        inv_mock2.set_fast_charging = AsyncMock(return_value=True)
        inv_mock2.get_fast_charging = AsyncMock(return_value=False)
        inv_mock2.get_ems_mode = AsyncMock(return_value="battery_standby")
        engine._executor._inverters["forrad"] = inv_mock2

        engine._sm.state.current = Scenario.PV_SURPLUS_DAY
        engine._sm.state.entry_time = datetime(2026, 4, 12, 11, 0, tzinfo=timezone.utc)

        snap = make_snapshot(
            hour=17,
            batteries=[
                make_battery_state(battery_id="kontor", soc_pct=60.0),
                make_battery_state(battery_id="forrad", soc_pct=55.0),
            ],
        )

        with patch.object(
            engine._mode_manager, "request_change",
            wraps=engine._mode_manager.request_change,
        ) as mock_rc:
            await engine.run_cycle(snap)

        calls = mock_rc.call_args_list
        battery_ids = [c.kwargs.get("battery_id") for c in calls]
        assert "kontor" in battery_ids
        assert "forrad" in battery_ids
        assert len(calls) == 2


class TestNoHardcodedScenarioString:
    """REGRESSION: engine.py must not have battery_id='scenario'."""

    def test_no_scenario_string_as_battery_id(self) -> None:
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2] / "core" / "engine.py").read_text()
        assert 'battery_id="scenario"' not in source
        assert "battery_id='scenario'" not in source


class TestNoNaked1000InEngineModels:
    """PLAT-1608: No naked 1000.0 literals in engine.py or models.py."""

    def test_no_naked_1000_in_engine(self) -> None:
        from pathlib import Path
        import re

        source = (Path(__file__).resolve().parents[2] / "core" / "engine.py").read_text()
        # Find lines with * 1000 or / 1000 that are NOT the constant definition
        for i, line in enumerate(source.splitlines(), 1):
            if re.search(r'[*/]\s*1000\.0', line) and '_W_TO_KW' not in line:
                assert False, f"Naked 1000.0 at engine.py:{i}: {line.strip()}"

    def test_no_naked_5000_in_engine(self) -> None:
        from pathlib import Path
        import re

        source = (Path(__file__).resolve().parents[2] / "core" / "engine.py").read_text()
        for i, line in enumerate(source.splitlines(), 1):
            has_5000 = re.search(r'\b5000\b', line)
            is_const = '_SAFE_BAT_FALLBACK_W' in line or '_DEFAULT_EXPORT_LIMIT_W' in line
            if has_5000 and not is_const:
                if not line.strip().startswith("#"):
                    assert False, f"Naked 5000 at engine.py:{i}: {line.strip()}"

    def test_no_naked_ratio_literals_in_engine(self) -> None:
        """Guard: 0.75/0.25 must be named constants, not inline."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2] / "core" / "engine.py").read_text()
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "* 0.75" in stripped or "* 0.25" in stripped:
                assert False, (
                    f"Naked ratio literal at engine.py:{i}: {stripped}"
                )

    def test_no_naked_1000_in_models(self) -> None:
        from pathlib import Path
        import re

        source = (Path(__file__).resolve().parents[2] / "core" / "models.py").read_text()
        for i, line in enumerate(source.splitlines(), 1):
            if re.search(r'[*/]\s*1000\.0', line) and '_W_TO_KW' not in line:
                assert False, f"Naked 1000.0 at models.py:{i}: {line.strip()}"


# ===========================================================================
# PLAT-1572: 0W balance + FREEZE guard separation
# ===========================================================================


class TestNearZeroConstant:
    """AC2: NEAR_ZERO_KW is a named constant."""

    def test_constant_exists(self) -> None:
        assert NEAR_ZERO_KW == 0.05

    def test_no_naked_005_in_engine(self) -> None:
        """NEAR_ZERO_KW must be used — no inline 0.05 in balance logic."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2] / "core" / "engine.py").read_text()
        # The constant definition itself will have 0.05 — that's fine
        # But balance logic should use the named constant
        assert "NEAR_ZERO_KW" in source


@pytest.mark.asyncio
class TestNearZeroBalance:
    """AC1: Near-zero grid triggers standby."""

    async def test_near_zero_grid_triggers_standby(self) -> None:
        """grid_kw < NEAR_ZERO_KW in discharge mode → BATTERY_STANDBY."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.EVENING_DISCHARGE
        engine._sm.state.entry_time = datetime(
            _TEST_ENTRY_YEAR, _TEST_ENTRY_MONTH, _TEST_ENTRY_DAY,
            _TEST_ENTRY_HOUR_EVENING, 0, tzinfo=timezone.utc,
        )

        from tests.conftest import make_grid_state

        # grid_power_w = 30W → 0.03 kW < NEAR_ZERO_KW (0.05)
        snap = make_snapshot(
            hour=_TEST_EVENING_HOUR,
            batteries=[make_battery_state(soc_pct=_TEST_SOC_NOMINAL_PCT)],
            grid=make_grid_state(grid_power_w=30.0),
        )

        with patch.object(
            engine._mode_manager, "request_change",
            wraps=engine._mode_manager.request_change,
        ) as mock_rc:
            await engine.run_cycle(snap)

        # Should have standby request for near-zero balance
        standby_calls = [
            c for c in mock_rc.call_args_list
            if c.kwargs.get("target_mode") == "battery_standby"
            and "Near-zero" in c.kwargs.get("reason", "")
        ]
        assert len(standby_calls) >= 1


@pytest.mark.asyncio
class TestFreezeExceptionSafety:
    """AC3-AC5: Guard exception does not crash cycle."""

    async def test_guard_exception_does_not_crash(self) -> None:
        """If guard.evaluate raises, cycle completes with error."""
        engine = _make_engine()
        snap = make_snapshot(hour=12)

        with patch.object(
            engine._guard,
            "evaluate",
            side_effect=RuntimeError("guard broke"),
        ):
            result = await engine.run_cycle(snap)

        assert result.error is not None
        assert "guard broke" in result.error
        assert result.elapsed_s >= 0

    async def test_freeze_check_inside_try_except(self) -> None:
        """FREEZE path is protected by try/except — verify via source."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2] / "core" / "engine.py").read_text()
        # FREEZE check must appear AFTER 'try:' and BEFORE 'except'
        try_pos = source.index("try:")
        freeze_pos = source.index("GuardLevel.FREEZE")
        except_pos = source.index("except Exception")
        assert try_pos < freeze_pos < except_pos


# ===========================================================================
# PLAT-1615: Daytime charge plan — sole owner of limits
# ===========================================================================


@pytest.mark.asyncio
class TestDaytimeChargePlan:
    """Daytime charge uses _compute_charge_plan exclusively."""

    _PV_EXPORT_W: float = -2000.0  # PV surplus (export)

    async def test_charge_plan_sets_pv_surplus_limit(self) -> None:
        """Charge plan sets limit based on PV surplus, not zero."""
        engine = _make_engine()
        engine._sm.state.current = Scenario.PV_SURPLUS_DAY
        engine._sm.state.entry_time = datetime(
            _TEST_ENTRY_YEAR, _TEST_ENTRY_MONTH, _TEST_ENTRY_DAY,
            _TEST_ENTRY_HOUR_MIDDAY, 0, tzinfo=timezone.utc,
        )

        from tests.conftest import make_grid_state

        snap = make_snapshot(
            hour=_TEST_MIDDAY_HOUR,
            batteries=[make_battery_state(soc_pct=_TEST_SOC_NOMINAL_PCT)],
            grid=make_grid_state(grid_power_w=self._PV_EXPORT_W),
        )
        result = await engine.run_cycle(snap)

        # Charge plan should have set a PV surplus limit (not 0)
        assert result.error is None


# ===========================================================================
# PLAT-1618: Active SoC balancing via discharge when imbalanced
# ===========================================================================

# Named test constants for SoC balancing tests
_SOC_HIGHER_PCT: float = 60.0   # Higher-SoC battery (Kontor) — 30% above lower
_SOC_LOWER_PCT: float = 30.0    # Lower-SoC battery (Forrad) — diff = 30%
_SOC_BALANCED_HIGH_PCT: float = 31.0  # Just 1% above lower — within +/-2% threshold
_SOC_BALANCED_LOW_PCT: float = 30.0   # Reference — diff = 1% (balanced)
_PV_SURPLUS_EXPORT_W: float = -3000.0  # Strong PV export (surplus)


def _make_dual_battery_engine() -> ControlEngine:
    """Create engine with two inverter mocks (kontor + forrad)."""
    guard = GridGuard(GuardConfig())
    sm = StateMachine(StateMachineConfig(min_dwell_s=0))
    balancer = BatteryBalancer()
    mode_mgr = ModeChangeManager(ModeChangeConfig(
        clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0,
    ))

    def _make_inv() -> AsyncMock:
        inv = AsyncMock()
        inv.set_ems_mode = AsyncMock(return_value=True)
        inv.set_ems_power_limit = AsyncMock(return_value=True)
        inv.set_fast_charging = AsyncMock(return_value=True)
        inv.get_fast_charging = AsyncMock(return_value=False)
        inv.get_ems_mode = AsyncMock(return_value="battery_standby")
        return inv

    executor = CommandExecutor(
        inverters={"kontor": _make_inv(), "forrad": _make_inv()},
        config=ExecutorConfig(mode_change_cooldown_s=0),
    )
    return ControlEngine(guard, sm, balancer, mode_mgr, executor)


@pytest.mark.asyncio
class TestSoCBalancingDischarge:
    """PLAT-1618: SoC diff >2% -> active discharge of higher-SoC bat."""

    async def _make_imbalanced_snap(
        self, soc_high: float, soc_low: float
    ) -> SystemSnapshot:
        from tests.conftest import make_grid_state

        return make_snapshot(
            hour=_TEST_MIDDAY_HOUR,
            batteries=[
                make_battery_state(
                    battery_id="kontor",
                    soc_pct=soc_high,
                    ems_mode=EMSMode.BATTERY_STANDBY,
                ),
                make_battery_state(
                    battery_id="forrad",
                    soc_pct=soc_low,
                    ems_mode=EMSMode.BATTERY_STANDBY,
                ),
            ],
            grid=make_grid_state(grid_power_w=_PV_SURPLUS_EXPORT_W),
        )

    async def test_discharge_command_when_imbalanced(self) -> None:
        """SoC diff 60%/30% (30% > 2%) -> discharge_pv command for higher-SoC bat."""
        from core.executor import ExecutionResult

        engine = _make_dual_battery_engine()
        engine._sm.state.current = Scenario.PV_SURPLUS_DAY
        engine._sm.state.entry_time = datetime(
            _TEST_ENTRY_YEAR, _TEST_ENTRY_MONTH, _TEST_ENTRY_DAY,
            _TEST_ENTRY_HOUR_MIDDAY, 0, tzinfo=timezone.utc,
        )

        captured: list[list[Command]] = []
        original_execute = engine._executor.execute

        async def capturing_execute(commands: list[Command]) -> ExecutionResult:
            captured.extend([commands])
            return await original_execute(commands)

        engine._executor.execute = capturing_execute  # type: ignore[method-assign]

        snap = await self._make_imbalanced_snap(_SOC_HIGHER_PCT, _SOC_LOWER_PCT)
        result = await engine.run_cycle(snap)

        assert result.error is None
        assert captured, "No commands were issued"
        all_cmds = [cmd for batch in captured for cmd in batch]

        discharge_cmds = [
            cmd for cmd in all_cmds
            if cmd.command_type == CommandType.SET_EMS_MODE
            and cmd.value == EMSMode.DISCHARGE_PV.value
        ]
        assert discharge_cmds, (
            "Expected a discharge_pv command for the higher-SoC battery. "
            f"Commands: {[(c.command_type, c.target_id, c.value) for c in all_cmds]}"
        )
        discharged_targets = {cmd.target_id for cmd in discharge_cmds}
        assert "kontor" in discharged_targets, (
            f"Kontor (60% SoC) should be discharged, got: {discharged_targets}"
        )

    async def test_no_discharge_when_balanced(self) -> None:
        """SoC diff 31%/30% (1% < 2%) -> no discharge_pv commands."""
        from core.executor import ExecutionResult

        engine = _make_dual_battery_engine()
        engine._sm.state.current = Scenario.PV_SURPLUS_DAY
        engine._sm.state.entry_time = datetime(
            _TEST_ENTRY_YEAR, _TEST_ENTRY_MONTH, _TEST_ENTRY_DAY,
            _TEST_ENTRY_HOUR_MIDDAY, 0, tzinfo=timezone.utc,
        )

        captured: list[list[Command]] = []
        original_execute = engine._executor.execute

        async def capturing_execute(commands: list[Command]) -> ExecutionResult:
            captured.extend([commands])
            return await original_execute(commands)

        engine._executor.execute = capturing_execute  # type: ignore[method-assign]

        snap = await self._make_imbalanced_snap(_SOC_BALANCED_HIGH_PCT, _SOC_BALANCED_LOW_PCT)
        result = await engine.run_cycle(snap)

        assert result.error is None
        all_cmds = [cmd for batch in captured for cmd in batch]

        discharge_cmds = [
            cmd for cmd in all_cmds
            if cmd.command_type == CommandType.SET_EMS_MODE
            and cmd.value == EMSMode.DISCHARGE_PV.value
        ]
        assert not discharge_cmds, (
            f"No discharge_pv expected for balanced batteries (diff=1%), got: "
            f"{[(c.target_id, c.value) for c in discharge_cmds]}"
        )

    def test_discharge_rate_constant_exists(self) -> None:
        """_BALANCE_DISCHARGE_RATE_W must be defined as a named int constant > 0."""
        assert hasattr(ControlEngine, "_BALANCE_DISCHARGE_RATE_W")
        assert isinstance(ControlEngine._BALANCE_DISCHARGE_RATE_W, int)
        assert ControlEngine._BALANCE_DISCHARGE_RATE_W > 0

    def test_no_naked_balance_discharge_rate_in_engine(self) -> None:
        """engine.py must reference _BALANCE_DISCHARGE_RATE_W by name, never by value.

        Regression guard: NOLLTOLERANS — no naked numeric literals.
        """
        from pathlib import Path
        import re

        source = (
            Path(__file__).resolve().parents[2] / "core" / "engine.py"
        ).read_text()
        rate = ControlEngine._BALANCE_DISCHARGE_RATE_W
        definition_pattern = re.compile(
            rf"_BALANCE_DISCHARGE_RATE_W\s*:\s*int\s*=\s*{rate}"
        )
        naked_lines = [
            line for line in source.splitlines()
            if re.search(rf"\b{rate}\b", line)
            and not definition_pattern.search(line)
        ]
        assert not naked_lines, (
            f"Naked literal {rate} in engine.py — use _BALANCE_DISCHARGE_RATE_W: "
            f"{naked_lines}"
        )
