"""Command Executor for CARMA Box.

Translates CycleDecision commands into HA REST API calls via adapters.
Enforces:
- Mode change protocol (5-step via ModeChangeManager)
- Guard emergency bypass
- Rate limits (1 mode change per battery per 5 min)
- INV-3 fast_charging check before discharge (B7)
- Audit logging for every command
- Failed commands never crash the executor
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.guards import GuardCommand
from core.mode_change import ModeChangeManager
from core.models import Command, CommandType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols (avoid circular imports)
# ---------------------------------------------------------------------------


class InverterPort(Protocol):
    """Minimal inverter interface needed by executor."""

    async def set_ems_mode(self, mode: str) -> bool: ...
    async def set_ems_power_limit(self, watts: int) -> bool: ...
    async def set_fast_charging(self, on: bool) -> bool: ...
    async def get_fast_charging(self) -> bool: ...
    async def get_ems_mode(self) -> str: ...


class EVChargerPort(Protocol):
    """Minimal EV charger interface needed by executor."""

    async def set_current(self, amps: int) -> bool: ...
    async def start_charging(self) -> bool: ...
    async def stop_charging(self) -> bool: ...


class LoadPort(Protocol):
    """Minimal load interface needed by executor."""

    async def turn_on(self) -> bool: ...
    async def turn_off(self) -> bool: ...


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEntry:
    """Audit trail entry for a single command execution."""

    timestamp: float         # monotonic
    command_type: str
    target_id: str
    value: Any
    rule_id: str
    reason: str
    success: bool
    error: str = ""


@dataclass
class ExecutionResult:
    """Result of executing a CycleDecision."""

    commands_total: int = 0
    commands_succeeded: int = 0
    commands_failed: int = 0
    commands_rate_limited: int = 0
    audit_entries: list[AuditEntry] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        return self.commands_failed == 0 and self.commands_total > 0


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutorConfig:
    """Executor configuration — all from site.yaml."""

    mode_change_cooldown_s: float = 300.0  # 5 min between mode changes per battery
    audit_log_enabled: bool = True


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class CommandExecutor:
    """Executes commands via adapters with safety checks and audit trail.

    Battery mode changes go through ModeChangeManager (5-step protocol).
    Guard commands use emergency bypass.
    All commands are audit-logged regardless of success.
    """

    def __init__(
        self,
        inverters: dict[str, InverterPort],
        ev_charger: EVChargerPort | None = None,
        consumers: dict[str, LoadPort] | None = None,
        mode_manager: ModeChangeManager | None = None,
        config: ExecutorConfig | None = None,
    ) -> None:
        self._inverters = inverters
        self._ev = ev_charger
        self._consumers = consumers or {}
        self._mode_manager = mode_manager or ModeChangeManager()
        self._config = config or ExecutorConfig()
        # Rate limiting: last mode change time per battery_id
        self._last_mode_change: dict[str, float] = {}
        # Audit trail (in-memory, flushed to DB by caller)
        self._audit: list[AuditEntry] = []

    async def execute(self, commands: list[Command]) -> ExecutionResult:
        """Execute a list of commands from a CycleDecision.

        Each command is dispatched to the appropriate adapter.
        Failed commands are logged but do not stop execution.
        """
        result = ExecutionResult(commands_total=len(commands))

        for cmd in commands:
            if cmd.command_type == CommandType.NO_OP:
                continue

            # Rate limit check for mode changes
            if cmd.command_type == CommandType.SET_EMS_MODE:
                if not self._check_rate_limit(cmd.target_id):
                    result.commands_rate_limited += 1
                    self._log_audit(cmd, success=False, error="rate_limited")
                    continue

            success = await self._dispatch(cmd)
            if success:
                result.commands_succeeded += 1
            else:
                result.commands_failed += 1

            self._log_audit(cmd, success=success)

        result.audit_entries = list(self._audit[-len(commands):])
        return result

    async def execute_guard_commands(
        self, commands: list[GuardCommand]
    ) -> ExecutionResult:
        """Execute guard commands via emergency path (bypasses rate limits).

        Guard commands skip the 5-step mode change protocol because
        they are safety-critical and must execute immediately.
        """
        result = ExecutionResult(commands_total=len(commands))

        for gcmd in commands:
            cmd = Command(
                command_type=gcmd.command_type,
                target_id=gcmd.target_id,
                value=gcmd.value,
                rule_id=gcmd.guard_id,
                reason=gcmd.reason,
            )

            # Guards execute IMMEDIATELY — bypass mode change protocol
            if gcmd.command_type == CommandType.SET_EMS_MODE:
                # Direct inverter call — no 5-step, no delay
                inverter = self._inverters.get(gcmd.target_id)
                if inverter:
                    success = await inverter.set_ems_mode(str(gcmd.value))
                else:
                    logger.error("Guard: no inverter for %s", gcmd.target_id)
                    success = False
            else:
                success = await self._dispatch(cmd)

            if success:
                result.commands_succeeded += 1
            else:
                result.commands_failed += 1

            self._log_audit(cmd, success=success)

        result.audit_entries = list(self._audit[-len(commands):])
        return result

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, cmd: Command) -> bool:
        """Route a command to the appropriate adapter.

        Returns True on success, False on failure. Never raises.
        """
        try:
            if cmd.command_type == CommandType.SET_EMS_MODE:
                return await self._exec_set_ems_mode(cmd)
            elif cmd.command_type == CommandType.SET_EMS_POWER_LIMIT:
                return await self._exec_set_ems_power_limit(cmd)
            elif cmd.command_type == CommandType.SET_FAST_CHARGING:
                return await self._exec_set_fast_charging(cmd)
            elif cmd.command_type == CommandType.SET_EV_CURRENT:
                return await self._exec_set_ev_current(cmd)
            elif cmd.command_type == CommandType.START_EV_CHARGING:
                return await self._exec_start_ev(cmd)
            elif cmd.command_type == CommandType.STOP_EV_CHARGING:
                return await self._exec_stop_ev(cmd)
            elif cmd.command_type == CommandType.TURN_ON_CONSUMER:
                return await self._exec_consumer_on(cmd)
            elif cmd.command_type == CommandType.TURN_OFF_CONSUMER:
                return await self._exec_consumer_off(cmd)
            else:
                logger.warning("Unknown command type: %s", cmd.command_type)
                return False
        except Exception as exc:
            logger.error(
                "Command execution failed: %s %s → %s",
                cmd.command_type.value, cmd.target_id, exc,
            )
            return False

    # ------------------------------------------------------------------
    # Battery commands
    # ------------------------------------------------------------------

    async def _exec_set_ems_mode(self, cmd: Command) -> bool:
        """Route mode change through ModeChangeManager (5-step protocol)."""
        target_mode = str(cmd.value)

        # B7/B14: All discharge paths must verify fast_charging OFF
        if target_mode == "discharge_pv":
            inverter = self._inverters.get(cmd.target_id)
            if inverter:
                fc = await inverter.get_fast_charging()
                if fc:
                    logger.warning(
                        "B7: fast_charging ON before discharge_pv on %s, forcing OFF",
                        cmd.target_id,
                    )
                    await inverter.set_fast_charging(False)

        # Route through mode change protocol
        accepted = self._mode_manager.request_change(
            battery_id=cmd.target_id,
            target_mode=target_mode,
            reason=cmd.reason,
        )
        if accepted:
            self._last_mode_change[cmd.target_id] = time.monotonic()
        return accepted

    async def _exec_set_ems_power_limit(self, cmd: Command) -> bool:
        inverter = self._inverters.get(cmd.target_id)
        if not inverter:
            logger.error("No inverter for %s", cmd.target_id)
            return False
        return await inverter.set_ems_power_limit(int(cmd.value or 0))

    async def _exec_set_fast_charging(self, cmd: Command) -> bool:
        inverter = self._inverters.get(cmd.target_id)
        if not inverter:
            logger.error("No inverter for %s", cmd.target_id)
            return False
        return await inverter.set_fast_charging(bool(cmd.value))

    # ------------------------------------------------------------------
    # EV commands
    # ------------------------------------------------------------------

    async def _exec_set_ev_current(self, cmd: Command) -> bool:
        if not self._ev:
            logger.error("No EV charger configured")
            return False
        return await self._ev.set_current(int(cmd.value or 6))

    async def _exec_start_ev(self, cmd: Command) -> bool:
        if not self._ev:
            return False
        return await self._ev.start_charging()

    async def _exec_stop_ev(self, cmd: Command) -> bool:
        if not self._ev:
            return False
        return await self._ev.stop_charging()

    # ------------------------------------------------------------------
    # Consumer commands
    # ------------------------------------------------------------------

    async def _exec_consumer_on(self, cmd: Command) -> bool:
        consumer = self._consumers.get(cmd.target_id)
        if not consumer:
            logger.error("No consumer for %s", cmd.target_id)
            return False
        return await consumer.turn_on()

    async def _exec_consumer_off(self, cmd: Command) -> bool:
        consumer = self._consumers.get(cmd.target_id)
        if not consumer:
            logger.error("No consumer for %s", cmd.target_id)
            return False
        return await consumer.turn_off()

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _check_rate_limit(self, battery_id: str) -> bool:
        """Check if a mode change is allowed (1 per cooldown period)."""
        last = self._last_mode_change.get(battery_id)
        if last is None:
            return True
        elapsed = time.monotonic() - last
        if elapsed < self._config.mode_change_cooldown_s:
            logger.info(
                "Rate limited: %s last mode change %.0fs ago (cooldown=%.0fs)",
                battery_id, elapsed, self._config.mode_change_cooldown_s,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    def _log_audit(
        self, cmd: Command, success: bool, error: str = ""
    ) -> None:
        """Log every command execution for audit trail."""
        entry = AuditEntry(
            timestamp=time.monotonic(),
            command_type=cmd.command_type.value,
            target_id=cmd.target_id,
            value=cmd.value,
            rule_id=cmd.rule_id,
            reason=cmd.reason,
            success=success,
            error=error,
        )
        self._audit.append(entry)
        if success:
            logger.info(
                "EXEC OK: %s %s=%s (rule=%s, reason=%s)",
                cmd.command_type.value, cmd.target_id,
                cmd.value, cmd.rule_id, cmd.reason,
            )
        else:
            logger.warning(
                "EXEC FAIL: %s %s=%s (rule=%s, error=%s)",
                cmd.command_type.value, cmd.target_id,
                cmd.value, cmd.rule_id, error or "adapter_failure",
            )

    # ------------------------------------------------------------------
    # ModeChangeExecutor protocol implementation
    # ------------------------------------------------------------------

    async def set_ems_mode(self, battery_id: str, mode: str) -> bool:
        """ModeChangeExecutor protocol: set mode on inverter."""
        inverter = self._inverters.get(battery_id)
        if not inverter:
            return False
        return await inverter.set_ems_mode(mode)

    async def set_ems_power_limit(self, battery_id: str, watts: int) -> bool:
        """ModeChangeExecutor protocol: set power limit."""
        inverter = self._inverters.get(battery_id)
        if not inverter:
            return False
        return await inverter.set_ems_power_limit(watts)

    async def set_fast_charging(self, battery_id: str, on: bool) -> bool:
        """ModeChangeExecutor protocol: set fast charging."""
        inverter = self._inverters.get(battery_id)
        if not inverter:
            return False
        return await inverter.set_fast_charging(on)

    async def get_ems_mode(self, battery_id: str) -> str:
        """ModeChangeExecutor protocol: read current mode."""
        inverter = self._inverters.get(battery_id)
        if not inverter:
            return "battery_standby"
        return await inverter.get_ems_mode()

    async def get_fast_charging(self, battery_id: str) -> bool:
        """ModeChangeExecutor protocol: read fast charging state."""
        inverter = self._inverters.get(battery_id)
        if not inverter:
            return False
        return await inverter.get_fast_charging()
