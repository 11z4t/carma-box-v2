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

from config.schema import BatteryConfig
from core.balancer import BalanceResult, BatteryBalancer, BatteryInfo
from core.executor import CommandExecutor, ExecutionResult
from core.guards import GridGuard, GuardEvaluation, GuardLevel
from core.mode_change import ModeChangeManager
from core.models import (
    MAX_SOC_PCT,
    Command,
    CommandType,
    EMSMode,
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
        battery_configs: Optional[dict[str, BatteryConfig]] = None,
    ) -> None:
        self._guard = guard
        self._sm = state_machine
        self._balancer = balancer
        self._mode_manager = mode_manager
        self._executor = executor
        # H2: per-battery capacity limits sourced from config (not hardcoded)
        self._battery_configs: dict[str, BatteryConfig] = battery_configs or {}
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
                # Route through ModeChangeManager for 5-step standby
                # (prevents B1/B2 firmware hangs from direct transitions)
                # Map scenario to target EMS mode
                S = Scenario
                scenario_modes: dict[Scenario, EMSMode] = {
                    S.MORNING_DISCHARGE: EMSMode.DISCHARGE_PV,
                    S.FORENOON_PV_EV: EMSMode.CHARGE_PV,
                    S.MIDDAY_CHARGE: EMSMode.CHARGE_PV,
                    S.EVENING_DISCHARGE: EMSMode.DISCHARGE_PV,
                    S.NIGHT_HIGH_PV: EMSMode.DISCHARGE_PV,
                    S.NIGHT_LOW_PV: EMSMode.BATTERY_STANDBY,
                    S.NIGHT_GRID_CHARGE: EMSMode.CHARGE_PV,
                    S.PV_SURPLUS: EMSMode.CHARGE_PV,
                }
                base_mode = scenario_modes.get(
                    new_scenario, EMSMode.BATTERY_STANDBY
                ).value
                # Set mode on EACH battery — adjust for SoC
                for bat in snapshot.batteries:
                    if not self._mode_manager.is_in_progress(bat.battery_id):
                        # At 100% SoC: standby (don't charge a full battery)
                        if bat.soc_pct >= MAX_SOC_PCT and base_mode in (
                            EMSMode.CHARGE_PV.value,
                        ):
                            target_mode = EMSMode.BATTERY_STANDBY.value
                        else:
                            target_mode = base_mode
                        self._mode_manager.request_change(
                            battery_id=bat.battery_id,
                            target_mode=target_mode,
                            reason=f"Scenario {new_scenario.value}",
                        )
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
                        # H2: read limits from config (kW → W); fall back to a
                        # safe conservative 5000 W if config is unavailable.
                        # (cap_kwh is energy, not power — kWh ≠ W)
                        max_discharge_w=(
                            self._battery_configs[b.battery_id].max_discharge_kw * 1000.0
                            if b.battery_id in self._battery_configs
                            else 5000.0
                        ),
                        max_charge_w=(
                            self._battery_configs[b.battery_id].max_charge_kw * 1000.0
                            if b.battery_id in self._battery_configs
                            else 5000.0
                        ),
                        ct_placement=b.ct_placement,
                        local_load_w=b.load_power_w,
                        pv_power_w=b.pv_power_w,
                    )
                    for b in snapshot.batteries
                ]
                # Determine charging direction: use scenario as primary signal,
                # but also check actual battery power direction as fallback.
                # Negative battery power = charging (power flowing into battery).
                scenario_charging = self._sm.state.current in (
                    Scenario.MIDDAY_CHARGE,
                    Scenario.NIGHT_GRID_CHARGE,
                    Scenario.PV_SURPLUS,
                    Scenario.FORENOON_PV_EV,
                )
                actual_charging = bool(snapshot.batteries) and all(
                    b.power_w < 0 for b in snapshot.batteries
                )
                is_charging = scenario_charging or actual_charging
                total_w = abs(snapshot.grid.grid_power_w)
                balance = self._balancer.allocate(bat_infos, total_w, is_charging)
                result.balance = balance

                # H1: Turn allocations into SET_EMS_POWER_LIMIT commands and execute.
                # Skip batteries that are at the floor (zero allocation) to avoid
                # writing 0 and inadvertently waking GoodWe's autonomous grid-charge.
                if balance.allocations:
                    limit_cmds: list[Command] = [
                        Command(
                            command_type=CommandType.SET_EMS_POWER_LIMIT,
                            target_id=alloc.battery_id,
                            value=alloc.watts,
                            rule_id="BALANCE",
                            reason=(
                                f"Balance: {alloc.share_pct:.0f}% share, "
                                f"{alloc.watts}W of {balance.total_requested_w:.0f}W total"
                            ),
                        )
                        for alloc in balance.allocations
                        if alloc.watts > 0
                    ]
                    if limit_cmds:
                        exec_result = await self._executor.execute(limit_cmds)
                        result.execution = exec_result
                        logger.debug(
                            "Cycle %s: balance → %d EMS limit commands (%d ok, %d fail)",
                            cycle_id, len(limit_cmds),
                            exec_result.commands_succeeded,
                            exec_result.commands_failed,
                        )

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

    @property
    def current_scenario(self) -> Scenario:
        """Current active scenario (public accessor — avoids traversing private attrs)."""
        return self._sm.state.current

    def set_manual_override(self, scenario: Optional[Scenario]) -> None:
        """Set or clear manual scenario override on the state machine."""
        self._sm.set_manual_override(scenario)
