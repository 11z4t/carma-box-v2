"""Mode Change Protocol (5-Step) for GoodWe ET inverters.

GoodWe firmware has 30-60s latency on mode changes. Direct transitions
(charge→discharge) can cause hangs (B1, B2). This module implements
the safe 5-step protocol:

  1. PREPARE:      Log intent, verify current mode, skip if idempotent
  2. CLEAR LIMITS: ems_power_limit=0, fast_charging=OFF (B7, B9, B15)
  3. STANDBY:      battery_standby for 5 min (B1: intermediate step)
  4. SET TARGET:   target mode + limits, verify fast_charging (B7)
  5. VERIFY:       Read back mode, retry up to 3× on mismatch

Only ONE mode change per battery at a time.
Multiple batteries can change simultaneously.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol for the executor (avoid circular import)
# ---------------------------------------------------------------------------


class ModeChangeExecutor(Protocol):
    """Minimal interface for executing mode changes on hardware."""

    async def set_ems_mode(self, battery_id: str, mode: str) -> bool: ...
    async def set_ems_power_limit(self, battery_id: str, watts: int) -> bool: ...
    async def set_fast_charging(self, battery_id: str, on: bool) -> bool: ...
    async def get_ems_mode(self, battery_id: str) -> str: ...
    async def get_fast_charging(self, battery_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


@unique
class ModeChangeState(Enum):
    """States in the 5-step mode change protocol."""

    IDLE = "idle"
    CLEARING = "clearing"
    STANDBY_WAIT = "standby_wait"
    SETTING_TARGET = "setting_target"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class ModeChangeRequest:
    """A pending or in-progress mode change request."""

    battery_id: str
    target_mode: str
    target_limit_w: int = 0
    target_fast_charging: bool = False
    reason: str = ""
    state: ModeChangeState = ModeChangeState.IDLE
    step_started_at: float = 0.0  # monotonic timestamp
    retry_count: int = 0
    emergency: bool = False  # True = skip standby wait


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeChangeConfig:
    """Timing configuration for mode changes."""

    clear_wait_s: float = 60.0       # Wait after clearing limits (2 cycles)
    standby_wait_s: float = 300.0    # Wait in standby (5 min)
    set_wait_s: float = 60.0         # Wait after setting target mode
    verify_wait_s: float = 30.0      # Wait before verification
    max_retries: int = 3


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ModeChangeManager:
    """Manages the 5-step mode change protocol per battery.

    Call `process()` every control cycle (30s). The manager advances
    each pending request through the state machine as timers expire.
    """

    def __init__(self, config: Optional[ModeChangeConfig] = None) -> None:
        self._config = config or ModeChangeConfig()
        self._requests: dict[str, ModeChangeRequest] = {}

    def request_change(
        self,
        battery_id: str,
        target_mode: str,
        target_limit_w: int = 0,
        target_fast_charging: bool = False,
        reason: str = "",
    ) -> bool:
        """Request a mode change. Returns False if one is already in progress.

        If the same target_mode is already in progress, returns False (idempotent).
        """
        if battery_id in self._requests:
            current = self._requests[battery_id]
            if current.state not in (
                ModeChangeState.IDLE,
                ModeChangeState.COMPLETE,
                ModeChangeState.FAILED,
            ):
                if current.target_mode == target_mode:
                    return False  # Same target, already in progress
                logger.warning(
                    "Mode change in progress for %s (%s→%s), rejecting new request (%s)",
                    battery_id, current.target_mode, current.state.value, target_mode,
                )
                return False

        self._requests[battery_id] = ModeChangeRequest(
            battery_id=battery_id,
            target_mode=target_mode,
            target_limit_w=target_limit_w,
            target_fast_charging=target_fast_charging,
            reason=reason,
        )
        logger.info(
            "Mode change requested: %s → %s (limit=%dW, reason=%s)",
            battery_id, target_mode, target_limit_w, reason,
        )
        return True

    def emergency_mode_change(
        self,
        battery_id: str,
        target_mode: str,
        reason: str = "",
    ) -> bool:
        """Emergency mode change — skips standby wait (for guards).

        Overrides any in-progress request.
        """
        logger.warning(
            "EMERGENCY mode change: %s → %s (reason=%s)",
            battery_id, target_mode, reason,
        )
        self._requests[battery_id] = ModeChangeRequest(
            battery_id=battery_id,
            target_mode=target_mode,
            target_limit_w=0,
            target_fast_charging=False,
            reason=f"EMERGENCY: {reason}",
            emergency=True,
        )
        return True

    async def process(self, executor: ModeChangeExecutor) -> None:
        """Process all pending mode changes (call every cycle).

        Advances each request through the state machine as timers expire.
        """
        for battery_id in list(self._requests.keys()):
            req = self._requests[battery_id]
            if req.state in (ModeChangeState.COMPLETE, ModeChangeState.FAILED):
                continue
            await self._process_request(req, executor)

    def is_in_progress(self, battery_id: str) -> bool:
        """Is a mode change currently in progress for this battery?"""
        if battery_id not in self._requests:
            return False
        req = self._requests[battery_id]
        return req.state not in (
            ModeChangeState.IDLE,
            ModeChangeState.COMPLETE,
            ModeChangeState.FAILED,
        )

    def cancel(self, battery_id: str) -> None:
        """Cancel any in-progress mode change for this battery."""
        if battery_id in self._requests:
            req = self._requests[battery_id]
            if req.state not in (ModeChangeState.COMPLETE, ModeChangeState.FAILED):
                logger.info("Mode change cancelled for %s", battery_id)
            del self._requests[battery_id]

    def get_state(self, battery_id: str) -> ModeChangeState:
        """Get the current state for a battery, or IDLE if none."""
        if battery_id not in self._requests:
            return ModeChangeState.IDLE
        return self._requests[battery_id].state

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    async def _process_request(
        self, req: ModeChangeRequest, executor: ModeChangeExecutor
    ) -> None:
        """Advance a single request through the 5-step protocol."""
        now = time.monotonic()

        if req.state == ModeChangeState.IDLE:
            # STEP 1: PREPARE — enter CLEARING
            req.state = ModeChangeState.CLEARING
            req.step_started_at = now
            logger.info(
                "Step 1 PREPARE: %s → %s (reason: %s)",
                req.battery_id, req.target_mode, req.reason,
            )
            # STEP 2: CLEAR LIMITS — execute immediately
            await executor.set_ems_power_limit(req.battery_id, 0)
            await executor.set_fast_charging(req.battery_id, False)

        elif req.state == ModeChangeState.CLEARING:
            # Wait for clear_wait_s
            elapsed = now - req.step_started_at
            if elapsed >= self._config.clear_wait_s:
                # Move to STANDBY
                if req.emergency:
                    # Emergency: skip standby, go directly to SET TARGET
                    req.state = ModeChangeState.SETTING_TARGET
                    req.step_started_at = now
                    logger.info(
                        "Step 4 SET TARGET (emergency skip standby): %s → %s",
                        req.battery_id, req.target_mode,
                    )
                    await self._execute_set_target(req, executor)
                else:
                    req.state = ModeChangeState.STANDBY_WAIT
                    req.step_started_at = now
                    logger.info(
                        "Step 3 STANDBY: %s entering battery_standby",
                        req.battery_id,
                    )
                    await executor.set_ems_mode(req.battery_id, "battery_standby")

        elif req.state == ModeChangeState.STANDBY_WAIT:
            # Wait for standby_wait_s
            elapsed = now - req.step_started_at
            if elapsed >= self._config.standby_wait_s:
                # Move to SET TARGET
                req.state = ModeChangeState.SETTING_TARGET
                req.step_started_at = now
                logger.info(
                    "Step 4 SET TARGET: %s → %s",
                    req.battery_id, req.target_mode,
                )
                await self._execute_set_target(req, executor)

        elif req.state == ModeChangeState.SETTING_TARGET:
            # Wait for set_wait_s, then verify
            elapsed = now - req.step_started_at
            if elapsed >= self._config.set_wait_s:
                req.state = ModeChangeState.VERIFYING
                req.step_started_at = now

        elif req.state == ModeChangeState.VERIFYING:
            # STEP 5: VERIFY
            elapsed = now - req.step_started_at
            if elapsed >= self._config.verify_wait_s:
                actual_mode = await executor.get_ems_mode(req.battery_id)
                if actual_mode == req.target_mode:
                    # B7: If target is discharge, verify fast_charging is OFF
                    if req.target_mode == "discharge_pv":
                        fc = await executor.get_fast_charging(req.battery_id)
                        if fc:
                            logger.error(
                                "VERIFY FAILED: %s fast_charging still ON in discharge_pv",
                                req.battery_id,
                            )
                            await executor.set_fast_charging(req.battery_id, False)
                    req.state = ModeChangeState.COMPLETE
                    logger.info(
                        "Step 5 VERIFY OK: %s now in %s",
                        req.battery_id, actual_mode,
                    )
                else:
                    req.retry_count += 1
                    if req.retry_count >= self._config.max_retries:
                        req.state = ModeChangeState.FAILED
                        logger.error(
                            "Mode change FAILED: %s expected=%s actual=%s after %d retries",
                            req.battery_id, req.target_mode,
                            actual_mode, req.retry_count,
                        )
                    else:
                        logger.warning(
                            "VERIFY MISMATCH: %s expected=%s actual=%s (retry %d/%d)",
                            req.battery_id, req.target_mode,
                            actual_mode, req.retry_count,
                            self._config.max_retries,
                        )
                        # Retry: go back to SETTING_TARGET
                        req.state = ModeChangeState.SETTING_TARGET
                        req.step_started_at = now
                        await self._execute_set_target(req, executor)

    async def _execute_set_target(
        self, req: ModeChangeRequest, executor: ModeChangeExecutor
    ) -> None:
        """Execute step 4: set target mode + limits.

        B7: If target is discharge_pv, verify fast_charging=OFF first.
        """
        # B7: ALWAYS verify fast_charging OFF before discharge
        if req.target_mode == "discharge_pv":
            fc = await executor.get_fast_charging(req.battery_id)
            if fc:
                logger.warning(
                    "B7: fast_charging still ON before discharge_pv, forcing OFF for %s",
                    req.battery_id,
                )
                await executor.set_fast_charging(req.battery_id, False)

        await executor.set_ems_mode(req.battery_id, req.target_mode)
        if req.target_limit_w > 0:
            await executor.set_ems_power_limit(req.battery_id, req.target_limit_w)
