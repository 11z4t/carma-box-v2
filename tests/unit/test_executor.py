"""Tests for Command Executor.

Covers:
- Battery command routes through ModeChangeManager
- Guard command uses emergency path
- Rate limit blocks rapid mode changes
- Audit trail written for every command
- Failed command returns success=False
- EV and consumer dispatch
- Regressions: B7 (discharge checks fast_charging), B14 (discharge_pv only)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.executor import CommandExecutor, ExecutorConfig
from core.guards import GuardCommand
from core.mode_change import ModeChangeManager, ModeChangeConfig
from core.models import Command, CommandType

# Domain constants used in tests — avoids naked literals (magic numbers)
_EXPORT_LIMIT_DISABLED_W: int = 0  # SET_EXPORT_LIMIT: disabled = 0 W export cap
_EMS_POWER_LIMIT_ZERO_W: int = 0   # SET_EMS_POWER_LIMIT: guard clamp / grid import blocked (B9)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_inverter(
    ems_mode: str = "battery_standby",
    fast_charging: bool = False,
    ems_power_limit: int = 0,
) -> AsyncMock:
    mock = AsyncMock()
    mock.set_ems_mode = AsyncMock(return_value=True)
    mock.set_ems_power_limit = AsyncMock(return_value=True)
    mock.set_fast_charging = AsyncMock(return_value=True)
    mock.get_fast_charging = AsyncMock(return_value=fast_charging)
    mock.get_ems_mode = AsyncMock(return_value=ems_mode)
    mock.get_ems_power_limit = AsyncMock(return_value=ems_power_limit)
    return mock


def _make_ev() -> AsyncMock:
    mock = AsyncMock()
    mock.set_current = AsyncMock(return_value=True)
    mock.start_charging = AsyncMock(return_value=True)
    mock.stop_charging = AsyncMock(return_value=True)
    return mock


def _make_consumer() -> AsyncMock:
    mock = AsyncMock()
    mock.turn_on = AsyncMock(return_value=True)
    mock.turn_off = AsyncMock(return_value=True)
    return mock


@pytest.fixture()
def inverters() -> dict[str, AsyncMock]:
    return {
        "kontor": _make_inverter(),
        "forrad": _make_inverter(),
    }


@pytest.fixture()
def executor(inverters: dict[str, AsyncMock]) -> CommandExecutor:
    return CommandExecutor(
        inverters=inverters,  # type: ignore[arg-type]
        ev_charger=_make_ev(),
        consumers={"miner": _make_consumer()},
        mode_manager=ModeChangeManager(ModeChangeConfig(
            clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0,
        )),
        config=ExecutorConfig(mode_change_cooldown_s=0),  # No cooldown in tests
    )


# ===========================================================================
# Battery commands → ModeChangeManager
# ===========================================================================


@pytest.mark.asyncio
class TestBatteryModeChange:
    """Battery mode changes route through ModeChangeManager."""

    async def test_mode_change_accepted(
        self, executor: CommandExecutor
    ) -> None:
        cmd = Command(
            command_type=CommandType.SET_EMS_MODE,
            target_id="kontor",
            value="discharge_pv",
            rule_id="S4",
            reason="Evening discharge",
        )
        result = await executor.execute([cmd])
        assert result.commands_succeeded == 1
        # Request accepted — state is IDLE (waiting for process() to advance)
        from core.mode_change import ModeChangeState
        state = executor._mode_manager.get_state("kontor")
        assert state == ModeChangeState.IDLE

    async def test_ems_power_limit_direct(
        self, executor: CommandExecutor, inverters: dict[str, AsyncMock]
    ) -> None:
        """Power limit goes directly to adapter (not via mode manager)."""
        cmd = Command(
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id="kontor",
            value=2500,
        )
        result = await executor.execute([cmd])
        assert result.commands_succeeded == 1
        inverters["kontor"].set_ems_power_limit.assert_awaited_with(2500)

    async def test_fast_charging_direct(
        self, executor: CommandExecutor, inverters: dict[str, AsyncMock]
    ) -> None:
        cmd = Command(
            command_type=CommandType.SET_FAST_CHARGING,
            target_id="forrad",
            value=False,
        )
        result = await executor.execute([cmd])
        assert result.commands_succeeded == 1
        inverters["forrad"].set_fast_charging.assert_awaited_with(False)


# ===========================================================================
# Guard commands → emergency path
# ===========================================================================


@pytest.mark.asyncio
class TestGuardEmergency:
    """Guard commands use emergency bypass."""

    async def test_guard_mode_change_is_emergency(
        self, executor: CommandExecutor
    ) -> None:
        gcmd = GuardCommand(
            guard_id="G0",
            command_type=CommandType.SET_EMS_MODE,
            target_id="kontor",
            value="battery_standby",
            reason="G0: grid charging detected",
        )
        result = await executor.execute_guard_commands([gcmd])
        assert result.commands_succeeded == 1

    async def test_guard_power_limit_direct(
        self, executor: CommandExecutor, inverters: dict[str, AsyncMock]
    ) -> None:
        gcmd = GuardCommand(
            guard_id="G0",
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id="kontor",
            value=_EMS_POWER_LIMIT_ZERO_W,
            reason="G0: zero limit",
        )
        result = await executor.execute_guard_commands([gcmd])
        assert result.commands_succeeded == 1
        inverters["kontor"].set_ems_power_limit.assert_awaited_with(_EMS_POWER_LIMIT_ZERO_W)


# ===========================================================================
# Rate limiting
# ===========================================================================


@pytest.mark.asyncio
class TestRateLimiting:
    """Mode change rate limiting."""

    async def test_rate_limit_blocks_rapid_changes(self) -> None:
        """Two mode changes within cooldown should block the second."""
        inv = {"kontor": _make_inverter()}
        executor = CommandExecutor(
            inverters=inv,  # type: ignore[arg-type]
            config=ExecutorConfig(mode_change_cooldown_s=300.0),
        )

        cmd = Command(
            command_type=CommandType.SET_EMS_MODE,
            target_id="kontor",
            value="discharge_pv",
        )
        # First succeeds
        r1 = await executor.execute([cmd])
        assert r1.commands_succeeded == 1

        # Second blocked by rate limit
        cmd2 = Command(
            command_type=CommandType.SET_EMS_MODE,
            target_id="kontor",
            value="charge_pv",
        )
        r2 = await executor.execute([cmd2])
        assert r2.commands_rate_limited == 1
        assert r2.commands_succeeded == 0

    async def test_different_batteries_not_rate_limited(self) -> None:
        """Rate limit is per-battery, not global."""
        inverters = {"kontor": _make_inverter(), "forrad": _make_inverter()}
        executor = CommandExecutor(
            inverters=inverters,  # type: ignore[arg-type]
            config=ExecutorConfig(mode_change_cooldown_s=300.0),
        )

        c1 = Command(
            command_type=CommandType.SET_EMS_MODE,
            target_id="kontor",
            value="discharge_pv",
        )
        c2 = Command(
            command_type=CommandType.SET_EMS_MODE,
            target_id="forrad",
            value="discharge_pv",
        )
        result = await executor.execute([c1, c2])
        assert result.commands_succeeded == 2


# ===========================================================================
# Audit trail
# ===========================================================================


@pytest.mark.asyncio
class TestAuditTrail:
    """Every command should produce an audit entry."""

    async def test_audit_entry_on_success(
        self, executor: CommandExecutor
    ) -> None:
        cmd = Command(
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id="kontor",
            value=2000,
            rule_id="S3",
            reason="Midday charge",
        )
        result = await executor.execute([cmd])
        assert len(result.audit_entries) == 1
        entry = result.audit_entries[0]
        assert entry.success
        assert entry.command_type == "set_ems_power_limit"
        assert entry.rule_id == "S3"

    async def test_audit_entry_on_failure(self) -> None:
        """Failed commands should also be audit-logged."""
        inv = {"kontor": _make_inverter()}
        inv["kontor"].set_ems_power_limit = AsyncMock(return_value=False)
        executor = CommandExecutor(inverters=inv)  # type: ignore[arg-type]

        cmd = Command(
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id="kontor",
            value=_EMS_POWER_LIMIT_ZERO_W,
        )
        result = await executor.execute([cmd])
        assert result.commands_failed == 1
        assert len(result.audit_entries) == 1
        assert not result.audit_entries[0].success


# ===========================================================================
# EV commands
# ===========================================================================


@pytest.mark.asyncio
class TestEVCommands:
    """EV charger command dispatch."""

    async def test_set_ev_current(self, executor: CommandExecutor) -> None:
        cmd = Command(
            command_type=CommandType.SET_EV_CURRENT,
            target_id="ev",
            value=8,
        )
        result = await executor.execute([cmd])
        assert result.commands_succeeded == 1

    async def test_start_ev(self, executor: CommandExecutor) -> None:
        cmd = Command(
            command_type=CommandType.START_EV_CHARGING,
            target_id="ev",
        )
        result = await executor.execute([cmd])
        assert result.commands_succeeded == 1

    async def test_stop_ev(self, executor: CommandExecutor) -> None:
        cmd = Command(
            command_type=CommandType.STOP_EV_CHARGING,
            target_id="ev",
        )
        result = await executor.execute([cmd])
        assert result.commands_succeeded == 1

    async def test_no_ev_charger_fails(self) -> None:
        executor = CommandExecutor(inverters={})
        cmd = Command(
            command_type=CommandType.SET_EV_CURRENT,
            target_id="ev",
            value=6,
        )
        result = await executor.execute([cmd])
        assert result.commands_failed == 1


# ===========================================================================
# Consumer commands
# ===========================================================================


@pytest.mark.asyncio
class TestConsumerCommands:
    """Consumer dispatch."""

    async def test_turn_on(self, executor: CommandExecutor) -> None:
        cmd = Command(
            command_type=CommandType.TURN_ON_CONSUMER,
            target_id="miner",
        )
        result = await executor.execute([cmd])
        assert result.commands_succeeded == 1

    async def test_turn_off(self, executor: CommandExecutor) -> None:
        cmd = Command(
            command_type=CommandType.TURN_OFF_CONSUMER,
            target_id="miner",
        )
        result = await executor.execute([cmd])
        assert result.commands_succeeded == 1

    async def test_unknown_consumer_fails(self, executor: CommandExecutor) -> None:
        cmd = Command(
            command_type=CommandType.TURN_ON_CONSUMER,
            target_id="nonexistent",
        )
        result = await executor.execute([cmd])
        assert result.commands_failed == 1


# ===========================================================================
# REGRESSION: B7 — discharge checks fast_charging
# ===========================================================================


@pytest.mark.asyncio
class TestB7DischargeCheck:
    """B7 regression: discharge_pv must verify fast_charging=OFF."""

    async def test_fast_charging_on_forced_off_before_discharge(self) -> None:
        """If fast_charging is ON when requesting discharge_pv, force OFF first."""
        inv = _make_inverter(fast_charging=True)
        executor = CommandExecutor(
            inverters={"kontor": inv},
            config=ExecutorConfig(mode_change_cooldown_s=0),
        )

        cmd = Command(
            command_type=CommandType.SET_EMS_MODE,
            target_id="kontor",
            value="discharge_pv",
        )
        await executor.execute([cmd])

        # fast_charging should have been forced OFF
        inv.set_fast_charging.assert_awaited_with(False)

    async def test_fast_charging_off_no_extra_call(self) -> None:
        """If fast_charging already OFF, no extra call needed."""
        inv = _make_inverter(fast_charging=False)
        executor = CommandExecutor(
            inverters={"kontor": inv},
            config=ExecutorConfig(mode_change_cooldown_s=0),
        )

        cmd = Command(
            command_type=CommandType.SET_EMS_MODE,
            target_id="kontor",
            value="discharge_pv",
        )
        await executor.execute([cmd])

        # set_fast_charging should NOT have been called
        inv.set_fast_charging.assert_not_awaited()


# ===========================================================================
# NO_OP skipped
# ===========================================================================


@pytest.mark.asyncio
class TestNoOp:
    """NO_OP commands should be skipped."""

    async def test_noop_skipped(self, executor: CommandExecutor) -> None:
        cmd = Command(
            command_type=CommandType.NO_OP,
            target_id="",
            reason="nothing",
        )
        result = await executor.execute([cmd])
        assert result.commands_succeeded == 0
        assert result.commands_failed == 0


# ===========================================================================
# ModeChangeExecutor protocol methods
# ===========================================================================


@pytest.mark.asyncio
class TestModeChangeProtocol:
    """Direct tests for ModeChangeExecutor protocol methods."""

    async def test_set_ems_mode_delegates(self, executor: CommandExecutor) -> None:
        result = await executor.set_ems_mode("kontor", "discharge_pv")
        assert result is True

    async def test_set_ems_mode_missing_inverter(self) -> None:
        exec_ = CommandExecutor(inverters={})
        result = await exec_.set_ems_mode("nonexistent", "discharge_pv")
        assert result is False

    async def test_set_ems_power_limit_delegates(
        self, executor: CommandExecutor, inverters: dict[str, AsyncMock]
    ) -> None:
        result = await executor.set_ems_power_limit("kontor", 2000)
        assert result is True
        inverters["kontor"].set_ems_power_limit.assert_awaited_with(2000)

    async def test_set_ems_power_limit_missing(self) -> None:
        exec_ = CommandExecutor(inverters={})
        result = await exec_.set_ems_power_limit("x", 0)
        assert result is False

    async def test_set_fast_charging_delegates(self, executor: CommandExecutor) -> None:
        result = await executor.set_fast_charging("kontor", False)
        assert result is True

    async def test_set_fast_charging_missing(self) -> None:
        exec_ = CommandExecutor(inverters={})
        result = await exec_.set_fast_charging("x", False)
        assert result is False

    async def test_get_ems_mode_delegates(self, executor: CommandExecutor) -> None:
        result = await executor.get_ems_mode("kontor")
        assert result == "battery_standby"

    async def test_get_ems_mode_missing(self) -> None:
        exec_ = CommandExecutor(inverters={})
        result = await exec_.get_ems_mode("x")
        assert result == "battery_standby"

    async def test_get_fast_charging_delegates(self, executor: CommandExecutor) -> None:
        result = await executor.get_fast_charging("kontor")
        assert result is False

    async def test_get_fast_charging_missing(self) -> None:
        exec_ = CommandExecutor(inverters={})
        result = await exec_.get_fast_charging("x")
        assert result is False


# ===========================================================================
# Missing inverter/consumer error paths
# ===========================================================================


@pytest.mark.asyncio
class TestMissingAdapterErrors:
    """Commands to nonexistent adapters should fail gracefully."""

    async def test_set_limit_missing_inverter(self, executor: CommandExecutor) -> None:
        cmd = Command(
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id="nonexistent",
            value=_EMS_POWER_LIMIT_ZERO_W,
        )
        result = await executor.execute([cmd])
        assert result.commands_failed == 1

    async def test_set_fast_charging_missing_inverter(self, executor: CommandExecutor) -> None:
        cmd = Command(
            command_type=CommandType.SET_FAST_CHARGING,
            target_id="nonexistent",
            value=False,
        )
        result = await executor.execute([cmd])
        assert result.commands_failed == 1

    async def test_consumer_off_missing(self, executor: CommandExecutor) -> None:
        cmd = Command(
            command_type=CommandType.TURN_OFF_CONSUMER,
            target_id="nonexistent",
        )
        result = await executor.execute([cmd])
        assert result.commands_failed == 1


# ===========================================================================
# Coverage: uncovered branches
# ===========================================================================


@pytest.mark.asyncio
class TestCoverageBranches:
    """Tests targeting specific uncovered code paths."""

    async def test_all_succeeded_property_true(
        self, executor: CommandExecutor
    ) -> None:
        """all_succeeded is True when no failures (line 88)."""
        cmd = Command(
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id="kontor",
            value=1000,
        )
        result = await executor.execute([cmd])
        assert result.all_succeeded is True

    async def test_all_succeeded_property_false_on_failure(self) -> None:
        """all_succeeded is False when there are failures."""
        inv = {"kontor": _make_inverter()}
        inv["kontor"].set_ems_power_limit = AsyncMock(return_value=False)
        exec_ = CommandExecutor(inverters=inv)  # type: ignore[arg-type]
        cmd = Command(
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id="kontor",
            value=_EMS_POWER_LIMIT_ZERO_W,
        )
        result = await exec_.execute([cmd])
        assert result.all_succeeded is False

    async def test_guard_non_mode_command_dispatched(
        self, executor: CommandExecutor, inverters: dict[str, AsyncMock]
    ) -> None:
        """Guard non-mode commands go through _dispatch (line 199)."""
        gcmd = GuardCommand(
            guard_id="G0",
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id="kontor",
            value=_EMS_POWER_LIMIT_ZERO_W,
            reason="G0: zero limit",
        )
        result = await executor.execute_guard_commands([gcmd])
        assert result.commands_succeeded == 1
        inverters["kontor"].set_ems_power_limit.assert_awaited_with(_EMS_POWER_LIMIT_ZERO_W)

    async def test_no_op_dispatch_returns_true(
        self, executor: CommandExecutor
    ) -> None:
        """NO_OP is registered in the dispatch table (_exec_no_op → True).

        PLAT-1592: dispatch table covers every CommandType; NO_OP handler
        returns True (vacuously successful).  execute() still pre-filters
        NO_OP before reaching _dispatch in normal flow.
        """
        result = await executor._dispatch(
            Command(command_type=CommandType.NO_OP, target_id="x")
        )
        assert result is True

    async def test_start_ev_no_charger_returns_false(self) -> None:
        """_exec_start_ev with no EV charger → False (line 298)."""
        exec_ = CommandExecutor(inverters={})
        cmd = Command(
            command_type=CommandType.START_EV_CHARGING,
            target_id="ev",
        )
        result = await exec_.execute([cmd])
        assert result.commands_failed == 1

    async def test_stop_ev_no_charger_returns_false(self) -> None:
        """_exec_stop_ev with no EV charger → False (line 303)."""
        exec_ = CommandExecutor(inverters={})
        cmd = Command(
            command_type=CommandType.STOP_EV_CHARGING,
            target_id="ev",
        )
        result = await exec_.execute([cmd])
        assert result.commands_failed == 1

    async def test_guard_command_failure_increments_failed(self) -> None:
        """Guard non-mode command that fails increments commands_failed (line 199)."""
        inv: dict[str, AsyncMock] = {"kontor": _make_inverter()}
        inv["kontor"].set_ems_power_limit = AsyncMock(return_value=False)
        exec_ = CommandExecutor(inverters=inv)  # type: ignore[arg-type]
        gcmd = GuardCommand(
            guard_id="G0",
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id="kontor",
            value=_EMS_POWER_LIMIT_ZERO_W,
            reason="G0: zero limit",
        )
        result = await exec_.execute_guard_commands([gcmd])
        assert result.commands_failed == 1

    async def test_dispatch_exception_returns_false(self) -> None:
        """Adapter raising exception in _dispatch returns False (lines 235-240)."""
        inv: dict[str, AsyncMock] = {"kontor": _make_inverter()}
        inv["kontor"].set_ems_power_limit = AsyncMock(side_effect=RuntimeError("adapter crash"))
        exec_ = CommandExecutor(inverters=inv)  # type: ignore[arg-type]
        cmd = Command(
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id="kontor",
            value=_EMS_POWER_LIMIT_ZERO_W,
        )
        result = await exec_.execute([cmd])
        assert result.commands_failed == 1

    async def test_rate_limit_passes_after_cooldown(self) -> None:
        """_check_rate_limit returns True after cooldown elapsed (line 340)."""
        inv = {"kontor": _make_inverter()}
        executor = CommandExecutor(
            inverters=inv,  # type: ignore[arg-type]
            config=ExecutorConfig(mode_change_cooldown_s=0.001),  # 1ms cooldown
        )
        # First mode change
        cmd = Command(
            command_type=CommandType.SET_EMS_MODE,
            target_id="kontor",
            value="discharge_pv",
        )
        await executor.execute([cmd])

        # Wait for cooldown to pass
        import asyncio
        await asyncio.sleep(0.01)

        # Second mode change should succeed (cooldown passed)
        cmd2 = Command(
            command_type=CommandType.SET_EMS_MODE,
            target_id="kontor",
            value="charge_pv",
        )
        r2 = await executor.execute([cmd2])
        assert r2.commands_succeeded == 1
        assert r2.commands_rate_limited == 0


# ===========================================================================
# PLAT-1373: Climate commands
# ===========================================================================


@pytest.mark.asyncio()
class TestClimateCommands:
    """PLAT-1373: Climate set_temperature and set_hvac_mode via HA API."""

    async def test_climate_set_temp(self) -> None:
        mock_api = AsyncMock()
        mock_api.call_service = AsyncMock(return_value=True)
        executor = CommandExecutor(
            inverters={}, ha_api=mock_api,
        )
        cmd = Command(
            command_type=CommandType.CLIMATE_SET_TEMP,
            target_id="climate.vp_kontor",
            value=22.0,
        )
        result = await executor.execute([cmd])
        assert result.commands_succeeded == 1
        mock_api.call_service.assert_called_once_with(
            "climate", "set_temperature",
            {"entity_id": "climate.vp_kontor", "temperature": 22.0},
        )

    async def test_climate_set_mode(self) -> None:
        mock_api = AsyncMock()
        mock_api.call_service = AsyncMock(return_value=True)
        executor = CommandExecutor(
            inverters={}, ha_api=mock_api,
        )
        cmd = Command(
            command_type=CommandType.CLIMATE_SET_MODE,
            target_id="climate.vp_kontor",
            value="heat",
        )
        result = await executor.execute([cmd])
        assert result.commands_succeeded == 1
        mock_api.call_service.assert_called_once_with(
            "climate", "set_hvac_mode",
            {"entity_id": "climate.vp_kontor", "hvac_mode": "heat"},
        )

    async def test_climate_no_ha_api_returns_false(self) -> None:
        executor = CommandExecutor(inverters={})
        cmd = Command(
            command_type=CommandType.CLIMATE_SET_TEMP,
            target_id="climate.vp_kontor",
            value=20.0,
        )
        result = await executor.execute([cmd])
        assert result.commands_failed == 1


# ===========================================================================
# PLAT-1592 — Dispatch table guard tests
# ===========================================================================


class TestDispatchTable:
    """Guard tests for PLAT-1592 dispatch table refactor (sync)."""

    def test_dispatch_table_contains_all_command_types(self) -> None:
        """Every CommandType must have an entry in the dispatch table.

        Ensures that adding a new CommandType to the enum forces a matching
        handler to be registered (the test will fail if the table is incomplete).
        NO_OP is included — it is registered via _exec_no_op.
        """
        executor = CommandExecutor(inverters={})
        for ct in CommandType:
            assert ct in executor._handlers, (
                f"CommandType.{ct.name} has no handler in _handlers dispatch table"
            )

    def test_no_if_elif_chain_in_execute(self) -> None:
        """_dispatch must not contain an elif command_type == chain (PLAT-1592 C1)."""
        import pathlib
        source = pathlib.Path("core/executor.py").read_text()
        assert "elif command_type ==" not in source, (
            "Found 'elif command_type ==' in core/executor.py — "
            "dispatch table must replace the if/elif chain (PLAT-1592 C1)"
        )


@pytest.mark.asyncio()
class TestDispatchTableAsync:
    """Guard tests for PLAT-1592 dispatch table refactor (async)."""

    async def test_set_export_limit_unknown_target_returns_false(self) -> None:
        """SET_EXPORT_LIMIT with no matching inverter returns False (PLAT-1699)."""
        executor = CommandExecutor(inverters={})
        cmd = Command(
            command_type=CommandType.SET_EXPORT_LIMIT,
            target_id="inverter_kontor",
            value=_EXPORT_LIMIT_DISABLED_W,
        )
        result = await executor.execute([cmd])
        assert result.commands_failed == 1

    async def test_set_export_limit_calls_adapter_on_success(self) -> None:
        """PLAT-1699 success path: SET_EXPORT_LIMIT calls inverter.set_export_limit
        with the requested watts value and returns success.
        """
        inverter = _make_inverter()
        inverter.set_export_limit = AsyncMock(return_value=True)
        executor = CommandExecutor(inverters={"kontor": inverter})
        cmd = Command(
            command_type=CommandType.SET_EXPORT_LIMIT,
            target_id="kontor",
            value=2500,
        )
        result = await executor.execute([cmd])
        assert result.commands_failed == 0
        inverter.set_export_limit.assert_awaited_once_with(2500)

    async def test_set_export_limit_adapter_failure_returns_false(self) -> None:
        """PLAT-1699: when the adapter raises, executor returns False + logs."""
        inverter = _make_inverter()
        inverter.set_export_limit = AsyncMock(side_effect=RuntimeError("HA 500"))
        executor = CommandExecutor(inverters={"kontor": inverter})
        cmd = Command(
            command_type=CommandType.SET_EXPORT_LIMIT,
            target_id="kontor",
            value=0,
        )
        result = await executor.execute([cmd])
        assert result.commands_failed == 1
