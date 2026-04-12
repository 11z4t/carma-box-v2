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

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_inverter(
    ems_mode: str = "battery_standby",
    fast_charging: bool = False,
) -> AsyncMock:
    mock = AsyncMock()
    mock.set_ems_mode = AsyncMock(return_value=True)
    mock.set_ems_power_limit = AsyncMock(return_value=True)
    mock.set_fast_charging = AsyncMock(return_value=True)
    mock.get_fast_charging = AsyncMock(return_value=fast_charging)
    mock.get_ems_mode = AsyncMock(return_value=ems_mode)
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
        inverters=inverters,
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
            value=0,
            reason="G0: zero limit",
        )
        result = await executor.execute_guard_commands([gcmd])
        assert result.commands_succeeded == 1
        inverters["kontor"].set_ems_power_limit.assert_awaited_with(0)


# ===========================================================================
# Rate limiting
# ===========================================================================


class TestRateLimiting:
    """Mode change rate limiting."""

    async def test_rate_limit_blocks_rapid_changes(self) -> None:
        """Two mode changes within cooldown should block the second."""
        inv = {"kontor": _make_inverter()}
        executor = CommandExecutor(
            inverters=inv,
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
            inverters=inverters,
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
        executor = CommandExecutor(inverters=inv)

        cmd = Command(
            command_type=CommandType.SET_EMS_POWER_LIMIT,
            target_id="kontor",
            value=0,
        )
        result = await executor.execute([cmd])
        assert result.commands_failed == 1
        assert len(result.audit_entries) == 1
        assert not result.audit_entries[0].success


# ===========================================================================
# EV commands
# ===========================================================================


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
