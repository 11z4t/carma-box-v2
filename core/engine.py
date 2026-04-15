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
    CTPlacement,
    Command,
    CommandType,
    EMSMode,
    Scenario,
    SystemSnapshot,
)
from core.state_machine import StateMachine
from storage.session_tracker import EV_EVENT_START, EV_EVENT_STOP, EnergySessionTracker

logger = logging.getLogger(__name__)

# Grid power threshold below which the system is considered balanced (0W).
# Used to detect near-zero import/export and trigger BATTERY_STANDBY.
NEAR_ZERO_KW: float = 0.05

# Watts-to-kilowatts conversion factor.
_W_TO_KW: float = 1000.0

# GoodWe firmware: charge_pv with limit>0 means grid-import is allowed.
# MUST be 0 for PV-only charging. See PLAT-1613 RCA.
_CHARGE_PV_EMS_LIMIT_W: int = 0

# Safe conservative fallback for max charge/discharge power (W)
# when battery config is unavailable.
_SAFE_BAT_FALLBACK_W: float = 5000.0


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


class _ScenarioMode:
    """EMS mode + power limit for a scenario."""

    __slots__ = ("mode", "ems_power_limit")

    def __init__(self, mode: EMSMode, ems_power_limit: int = 0) -> None:
        self.mode = mode
        self.ems_power_limit = ems_power_limit


class ControlEngine:
    """Main 30-second control loop engine.

    Coordinates all components: guards, state machine, balancer,
    mode change manager, and command executor.
    """

    _SCENARIO_MODES: dict[Scenario, _ScenarioMode] = {
        Scenario.MORNING_DISCHARGE: _ScenarioMode(EMSMode.DISCHARGE_PV),
        Scenario.FORENOON_PV_EV: _ScenarioMode(EMSMode.CHARGE_PV, ems_power_limit=0),
        Scenario.MIDDAY_CHARGE: _ScenarioMode(EMSMode.CHARGE_PV),
        Scenario.EVENING_DISCHARGE: _ScenarioMode(EMSMode.DISCHARGE_PV),
        Scenario.NIGHT_HIGH_PV: _ScenarioMode(EMSMode.DISCHARGE_PV),
        Scenario.NIGHT_LOW_PV: _ScenarioMode(EMSMode.BATTERY_STANDBY),
        Scenario.NIGHT_GRID_CHARGE: _ScenarioMode(EMSMode.CHARGE_PV),
        Scenario.PV_SURPLUS: _ScenarioMode(EMSMode.CHARGE_PV),
    }

    def __init__(
        self,
        guard: GridGuard,
        state_machine: StateMachine,
        balancer: BatteryBalancer,
        mode_manager: ModeChangeManager,
        executor: CommandExecutor,
        battery_configs: Optional[dict[str, BatteryConfig]] = None,
        session_tracker: Optional[EnergySessionTracker] = None,
    ) -> None:
        self._guard = guard
        self._sm = state_machine
        self._balancer = balancer
        self._mode_manager = mode_manager
        self._executor = executor
        # H2: per-battery capacity limits sourced from config (not hardcoded)
        self._battery_configs: dict[str, BatteryConfig] = battery_configs or {}
        self._session_tracker = session_tracker
        self._last_ev_charging: Optional[bool] = None
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
                    "Cycle %s: FREEZE/ALARM — skipping decision engine",
                    cycle_id,
                )
                result.elapsed_s = time.monotonic() - start
                return result

            # Phase 3: SCENARIO — evaluate state machine
            new_scenario = self._sm.evaluate(snapshot)
            if new_scenario is not None:
                self._sm.transition_to(new_scenario)
                result.scenario = new_scenario

            # Determine active scenario and target mode
            active_scenario = self._sm.state.current
            sm = self._SCENARIO_MODES.get(
                active_scenario,
                _ScenarioMode(EMSMode.BATTERY_STANDBY),
            )
            is_daytime_charge = (
                sm.mode == EMSMode.CHARGE_PV and not snapshot.is_night
            )

            # ============================================================
            # BRANCH: Daytime PV charging vs everything else
            # These are fundamentally different control problems.
            # ONE code path per branch — no conflicting writers.
            # ============================================================

            if is_daytime_charge:
                # ----- BRANCH A: Daytime PV surplus charging -----
                # SOLE OWNER of mode + ems_power_limit + export_limit.
                # No balancer, no mode_enforce, no mode_manager limits.
                await self._compute_charge_plan(snapshot)

            else:
                # ----- BRANCH B: Discharge / standby / night -----
                # Mode enforcement + balancer as before.
                base_mode = sm.mode.value
                for bat in snapshot.batteries:
                    if not self._mode_manager.is_in_progress(bat.battery_id):
                        if bat.soc_pct >= MAX_SOC_PCT and base_mode in (
                            EMSMode.CHARGE_PV.value,
                        ):
                            target_mode = EMSMode.BATTERY_STANDBY.value
                        else:
                            target_mode = base_mode
                        if bat.ems_mode.value != target_mode:
                            self._mode_manager.request_change(
                                battery_id=bat.battery_id,
                                target_mode=target_mode,
                                target_limit_w=sm.ems_power_limit,
                                reason=f"Scenario {active_scenario.value}",
                            )

                # Near-zero grid in discharge → standby
                grid_kw = abs(snapshot.grid.grid_power_w) / _W_TO_KW
                if (
                    grid_kw < NEAR_ZERO_KW
                    and sm.mode == EMSMode.DISCHARGE_PV
                ):
                    for bat in snapshot.batteries:
                        if not self._mode_manager.is_in_progress(
                            bat.battery_id,
                        ):
                            self._mode_manager.request_change(
                                battery_id=bat.battery_id,
                                target_mode=EMSMode.BATTERY_STANDBY.value,
                                reason="Near-zero grid — balanced",
                            )

                # Balancer — discharge/standby allocation
                if snapshot.batteries:
                    bat_infos = [
                        BatteryInfo(
                            battery_id=b.battery_id,
                            soc_pct=b.soc_pct,
                            cap_kwh=b.cap_kwh,
                            cell_temp_c=b.cell_temp_c,
                            soh_pct=b.soh_pct,
                            max_discharge_w=(
                                self._battery_configs[b.battery_id].max_discharge_kw
                                * _W_TO_KW
                                if b.battery_id in self._battery_configs
                                else _SAFE_BAT_FALLBACK_W
                            ),
                            max_charge_w=(
                                self._battery_configs[b.battery_id].max_charge_kw
                                * _W_TO_KW
                                if b.battery_id in self._battery_configs
                                else _SAFE_BAT_FALLBACK_W
                            ),
                            ct_placement=b.ct_placement,
                            local_load_w=b.load_power_w,
                            pv_power_w=b.pv_power_w,
                        )
                        for b in snapshot.batteries
                    ]
                    total_w = abs(snapshot.grid.grid_power_w)
                    is_charging = self._sm.state.current in (
                        Scenario.NIGHT_GRID_CHARGE,
                    )
                    balance = self._balancer.allocate(
                        bat_infos, total_w, is_charging,
                    )
                    result.balance = balance

                    if balance.allocations:
                        limit_cmds = [
                            Command(
                                command_type=CommandType.SET_EMS_POWER_LIMIT,
                                target_id=alloc.battery_id,
                                value=alloc.watts,
                                rule_id="BALANCE",
                                reason=(
                                    f"Balance: {alloc.share_pct:.0f}% share,"
                                    f" {alloc.watts}W"
                                ),
                            )
                            for alloc in balance.allocations
                            if alloc.watts > 0
                        ]
                        if limit_cmds:
                            exec_result = await self._executor.execute(
                                limit_cmds,
                            )
                            result.execution = exec_result

            # Phase 5: MODE CHANGE MANAGER — process pending changes
            # (only relevant for Branch B — Branch A writes directly)
            await self._mode_manager.process(self._executor)

            # Phase 6: SESSION TRACKING — record energy sessions (PLAT-1534)
            if self._session_tracker is not None:
                for bat in snapshot.batteries:
                    await self._session_tracker.on_battery_mode_change(
                        bat.battery_id, bat.ems_mode.value, snapshot
                    )
                # Detect EV state change and emit event to tracker
                ev_charging_now = snapshot.ev.charging
                if ev_charging_now != self._last_ev_charging:
                    if self._last_ev_charging is not None:
                        ev_event = EV_EVENT_START if ev_charging_now else EV_EVENT_STOP
                        await self._session_tracker.on_ev_event(ev_event, snapshot)
                self._last_ev_charging = ev_charging_now
                await self._session_tracker.update_pv_daily(snapshot)

            # Phase 7: PERSIST — write scenario to HA sensor (handled by caller)

        except Exception as exc:
            logger.error(
                "Cycle %s error: %s",
                cycle_id,
                exc,
                exc_info=True,
            )
            result.error = str(exc)

        result.elapsed_s = time.monotonic() - start
        logger.debug(
            "Cycle %s complete in %.3fs (scenario=%s, guard=%s)",
            cycle_id,
            result.elapsed_s,
            result.scenario.value,
            result.guard.level.value if result.guard else "none",
        )
        return result

    # ------------------------------------------------------------------
    # PV Surplus Regulator
    # ------------------------------------------------------------------

    _SOC_BALANCE_THRESHOLD_PCT: float = 2.0  # SoC diff below this = balanced
    _GRID_HYSTERESIS_W: float = 100.0  # Accept <100W import/export
    _PV_SURPLUS_MARGIN_W: int = 100  # Undersize limit by 100W to avoid grid import

    async def _compute_charge_plan(self, snapshot: SystemSnapshot) -> None:
        """SOLE OWNER of mode + ems_power_limit + export_limit during daytime.

        Computes PV surplus, allocates to batteries by SoC balance,
        writes all commands directly via executor. No other code path
        writes these values during daytime charge.

        CT-aware per battery:
        - Kontor (local_load): set export_limit + ems_limit
        - Förråd (house_grid): set ems_limit only

        SoC balancing: lower SoC bat gets all charging until SoC matches,
        then proportional by capacity (K75/F25).
        """
        if not snapshot.batteries:
            return

        # Find house_grid battery for total PV surplus
        house_grid_bat = None
        for bat in snapshot.batteries:
            if bat.ct_placement == CTPlacement.HOUSE_GRID:
                house_grid_bat = bat
            else:
                pass  # local_load batteries handled via allocations

        # Total PV surplus = house grid export (negative = export)
        total_pv_surplus_w = max(
            0, int(-house_grid_bat.grid_power_w) if house_grid_bat else 0,
        )

        # Add back what batteries are currently charging (to get true surplus)
        for bat in snapshot.batteries:
            if bat.power_w < 0:  # Negative = charging
                total_pv_surplus_w += int(abs(bat.power_w))

        # Subtract margin to stay safely below grid import threshold
        total_pv_surplus_w = max(0, total_pv_surplus_w - self._PV_SURPLUS_MARGIN_W)

        if total_pv_surplus_w < self._GRID_HYSTERESIS_W:
            # No meaningful PV surplus — set all to standby
            for bat in snapshot.batteries:
                if bat.ems_mode.value != EMSMode.BATTERY_STANDBY.value:
                    await self._executor.execute([Command(
                        command_type=CommandType.SET_EMS_MODE,
                        target_id=bat.battery_id,
                        value=EMSMode.BATTERY_STANDBY.value,
                        rule_id="PV_CHARGE_PLAN",
                        reason="No PV surplus → standby",
                    )])
            return

        # SoC balancing: allocate PV surplus between batteries
        bat_socs = {bat.battery_id: bat.soc_pct for bat in snapshot.batteries}
        bat_caps = {bat.battery_id: bat.cap_kwh for bat in snapshot.batteries}
        total_cap = sum(bat_caps.values()) or 1.0

        allocations: dict[str, int] = {}

        if len(snapshot.batteries) == 2:
            ids = list(bat_socs.keys())
            soc_diff = abs(bat_socs[ids[0]] - bat_socs[ids[1]])

            if soc_diff > self._SOC_BALANCE_THRESHOLD_PCT:
                # Unbalanced — all power to lower SoC battery
                lower_id = ids[0] if bat_socs[ids[0]] < bat_socs[ids[1]] else ids[1]
                higher_id = ids[1] if lower_id == ids[0] else ids[0]
                allocations[lower_id] = total_pv_surplus_w
                allocations[higher_id] = 0
            else:
                # Balanced — proportional by capacity
                for bid in ids:
                    share = bat_caps[bid] / total_cap
                    allocations[bid] = int(total_pv_surplus_w * share)
        else:
            # Single battery
            for bat in snapshot.batteries:
                allocations[bat.battery_id] = total_pv_surplus_w

        # Apply limits per battery
        cmds: list[Command] = []
        for bat in snapshot.batteries:
            limit_w = allocations.get(bat.battery_id, 0)

            # Set ems_power_limit
            cmds.append(Command(
                command_type=CommandType.SET_EMS_POWER_LIMIT,
                target_id=bat.battery_id,
                value=limit_w,
                rule_id="PV_CHARGE_PLAN",
                reason=(
                    f"PV surplus: {limit_w}W"
                    f" (total={total_pv_surplus_w}W,"
                    f" soc={bat.soc_pct:.0f}%)"
                ),
            ))

            # Kontor (local_load): also set export_limit
            if bat.ct_placement == CTPlacement.LOCAL_LOAD:
                cmds.append(Command(
                    command_type=CommandType.SET_EXPORT_LIMIT,
                    target_id=bat.battery_id,
                    value=limit_w,
                    rule_id="PV_CHARGE_PLAN",
                    reason=f"export_limit={limit_w}W (match ems_limit)",
                ))

            # Set charge_pv mode if limit > 0 and bat in standby
            if limit_w > 0 and bat.ems_mode.value == EMSMode.BATTERY_STANDBY.value:
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bat.battery_id,
                    value=EMSMode.CHARGE_PV.value,
                    rule_id="PV_CHARGE_PLAN",
                    reason=f"PV surplus {limit_w}W → charge_pv",
                ))
            # Set standby if limit = 0 and bat in charge_pv
            elif limit_w == 0 and bat.ems_mode.value == EMSMode.CHARGE_PV.value:
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bat.battery_id,
                    value=EMSMode.BATTERY_STANDBY.value,
                    rule_id="PV_CHARGE_PLAN",
                    reason="No PV allocation → standby",
                ))

        if cmds:
            await self._executor.execute(cmds)
            logger.info(
                "PV CHARGE PLAN: surplus=%dW, allocations=%s",
                total_pv_surplus_w,
                {k: f"{v}W" for k, v in allocations.items()},
            )

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
