"""Main Control Loop Engine for CARMA Box.

Orchestrates the 6-phase pipeline every 30 seconds:
  1. COLLECT  — read all sensor states from HA
  2. GUARD    — evaluate safety guards (VETO layer)
  3. SCENARIO — evaluate state machine transitions
  4. BALANCE  — run K/F balancer
  5. EXECUTE  — send commands via adapters
  6. PERSIST  — write state to HA sensor + storage

NEVER crashes — all exceptions caught, logged, and cycle continues.
Guard commands execute even when decision engine errors.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.balancer import BalanceResult, BatteryBalancer, BatteryInfo
from core.executor import CommandExecutor, ExecutionResult
from core.guards import GridGuard, GuardEvaluation, GuardLevel
from core.mode_change import ModeChangeManager
from core.models import (
    Scenario,
    SystemSnapshot,
)
from core.state_machine import StateMachine

logger = logging.getLogger(__name__)


@dataclass
class CycleResult:
    """Result of a single control cycle."""

    cycle_id: str
    timestamp: datetime
    elapsed_s: float
    scenario: Scenario
    guard: Optional[GuardEvaluation] = None
    balance: Optional[BalanceResult] = None
    execution: Optional[ExecutionResult] = None
    error: Optional[str] = None


class ControlEngine:
    """Main 30-second control loop engine.

    Coordinates all components: guards, state machine, balancer,
    mode change manager, and command executor.
    """

    def __init__(
        self,
        guard: GridGuard,
        state_machine: StateMachine,
        balancer: BatteryBalancer,
        mode_manager: ModeChangeManager,
        executor: CommandExecutor,
    ) -> None:
        self._guard = guard
        self._sm = state_machine
        self._balancer = balancer
        self._mode_manager = mode_manager
        self._executor = executor
        self._cycle_count = 0
        self._last_plan_time = 0.0

    async def run_cycle(
        self,
        snapshot: SystemSnapshot,
        ha_connected: bool = True,
        data_age_s: float = 0.0,
    ) -> CycleResult:
        """Execute one control cycle.

        NEVER raises — all errors caught and returned in CycleResult.
        Guard commands execute even if other phases fail.
        """
        cycle_id = str(uuid.uuid4())[:8]
        start = time.monotonic()
        self._cycle_count += 1

        result = CycleResult(
            cycle_id=cycle_id,
            timestamp=datetime.now(tz=timezone.utc),
            elapsed_s=0.0,
            scenario=self._sm.state.current,
        )

        try:
            # Phase 1: GUARD (always runs first — VETO layer)
            guard_eval = self._guard.evaluate(
                batteries=snapshot.batteries,
                current_scenario=self._sm.state.current,
                weighted_avg_kw=snapshot.grid.weighted_avg_kw,
                hour=snapshot.hour,
                ha_connected=ha_connected,
                data_age_s=data_age_s,
            )
            result.guard = guard_eval

            # Execute guard commands immediately (emergency path)
            if guard_eval.commands:
                await self._executor.execute_guard_commands(guard_eval.commands)

            # Phase 2: Check for FREEZE — skip decision engine
            if guard_eval.level in (GuardLevel.FREEZE, GuardLevel.ALARM):
                logger.warning(
                    "Cycle %s: FREEZE/ALARM — skipping decision engine", cycle_id,
                )
                result.elapsed_s = time.monotonic() - start
                return result

            # Phase 3: SCENARIO — evaluate state machine
            new_scenario = self._sm.evaluate(snapshot)
            if new_scenario is not None:
                self._sm.transition_to(new_scenario)
                result.scenario = new_scenario

            # Phase 4: BALANCE — K/F allocation
            if snapshot.batteries:
                bat_infos = [
                    BatteryInfo(
                        battery_id=b.battery_id,
                        soc_pct=b.soc_pct,
                        cap_kwh=b.cap_kwh,
                        cell_temp_c=b.cell_temp_c,
                        soh_pct=b.soh_pct,
                        max_discharge_w=5000.0,  # From config in real impl
                        max_charge_w=5000.0,
                        ct_placement=b.ct_placement,
                        local_load_w=b.load_power_w,
                        pv_power_w=b.pv_power_w,
                    )
                    for b in snapshot.batteries
                ]
                is_charging = self._sm.state.current in (
                    Scenario.MIDDAY_CHARGE,
                    Scenario.NIGHT_GRID_CHARGE,
                )
                total_w = abs(snapshot.grid.grid_power_w)
                balance = self._balancer.allocate(bat_infos, total_w, is_charging)
                result.balance = balance

            # Phase 5: MODE CHANGE MANAGER — process pending changes
            await self._mode_manager.process(self._executor)

            # Phase 6: PERSIST — write scenario to HA sensor (handled by caller)

        except Exception as exc:
            logger.error(
                "Cycle %s error: %s", cycle_id, exc, exc_info=True,
            )
            result.error = str(exc)

        result.elapsed_s = time.monotonic() - start
        logger.debug(
            "Cycle %s complete in %.3fs (scenario=%s, guard=%s)",
            cycle_id, result.elapsed_s,
            result.scenario.value,
            result.guard.level.value if result.guard else "none",
        )
        return result

    @property
    def cycle_count(self) -> int:
        return self._cycle_count
