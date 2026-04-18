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
from core.bat_support_controller import (
    BatInfo as BatSupportInfo,
    BatSupportConfig,
    BatSupportInput,
    evaluate as bat_support_evaluate,
)
from core.budget import (
    BudgetConfig,
    BudgetInput,
    BudgetState,
    allocate as budget_allocate,
)
from core.ev_night_controller import (
    NightEVConfig,
    NightEVState,
    evaluate as ev_night_evaluate,
)
from core.ev_surplus import EVSurplusController
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
from core.surplus_dispatch import SurplusDispatch
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

# Number of batteries in a dual-battery system.
_DUAL_BATTERY_COUNT: int = 2

# Default export limit when not actively charging (allow normal export).
_DEFAULT_EXPORT_LIMIT_W: int = 5000

# Safe conservative fallback for max charge/discharge power (W)
# when battery config is unavailable.
_SAFE_BAT_FALLBACK_W: float = 5000.0

# PLAT-1674: Min PV surplus for EV FM priority.
# EV @ 6A 3-phase = 6 × 3 × 230 = 4140W. Surplus must cover FULL EV draw.
_EV_MIN_SURPLUS_W: float = 4200.0


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
        Scenario.PV_SURPLUS_DAY: _ScenarioMode(EMSMode.CHARGE_PV),
        Scenario.EVENING_DISCHARGE: _ScenarioMode(EMSMode.DISCHARGE_PV),
        Scenario.NIGHT_HIGH_PV: _ScenarioMode(EMSMode.DISCHARGE_PV),
        Scenario.NIGHT_LOW_PV: _ScenarioMode(EMSMode.BATTERY_STANDBY),
        Scenario.NIGHT_GRID_CHARGE: _ScenarioMode(EMSMode.CHARGE_PV),
        Scenario.PV_SURPLUS: _ScenarioMode(EMSMode.CHARGE_PV),
        # PLAT-1674: NIGHT_EV — bat passively follows bat_support_controller's
        # decisions; mode set to discharge_pv as default for support during EV.
        Scenario.NIGHT_EV: _ScenarioMode(EMSMode.DISCHARGE_PV),
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
        ev_surplus: Optional[EVSurplusController] = None,
        surplus_dispatch: Optional[SurplusDispatch] = None,
        # PLAT-1674: Night EV + bat support controllers (optional — engine
        # falls back to legacy Branch B logic if None).
        night_ev_config: Optional[NightEVConfig] = None,
        bat_support_config: Optional[BatSupportConfig] = None,
    ) -> None:
        self._guard = guard
        self._sm = state_machine
        self._balancer = balancer
        self._mode_manager = mode_manager
        self._executor = executor
        # H2: per-battery capacity limits sourced from config (not hardcoded)
        self._battery_configs: dict[str, BatteryConfig] = battery_configs or {}
        self._session_tracker = session_tracker
        self._ev_surplus = ev_surplus
        self._surplus_dispatch = surplus_dispatch
        self._last_ev_charging: Optional[bool] = None
        self._cycle_count = 0
        self._last_plan_time = 0.0
        # PV charge plan: track mode per battery for dwell hysteresis
        self._charge_plan_mode: dict[str, str] = {}
        self._charge_plan_dwell: dict[str, int] = {}  # cycles in current mode
        # PLAT-1674: night EV controller state + configs
        self._night_ev_config = night_ev_config
        self._bat_support_config = bat_support_config
        self._night_ev_state: Optional[NightEVState] = None
        # PLAT-1686: Unified Budget Allocator
        self._budget_config: Optional[BudgetConfig] = None
        self._budget_state: BudgetState = BudgetState()

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
            # Budget Allocator handles daytime charge + evening discharge
            is_budget_scenario = (
                is_daytime_charge
                or active_scenario == Scenario.EVENING_DISCHARGE
            )

            # ============================================================
            # BRANCH: Budget Allocator vs everything else
            # Budget Allocator owns daytime PV + evening discharge.
            # ONE code path per branch — no conflicting writers.
            # ============================================================

            if is_budget_scenario and self._budget_config is not None:
                # ----- BRANCH A: Budget Allocator -----
                # PLAT-1686: Unified Budget Allocator handles
                # charge, discharge, and EV allocation responsively.
                for bat in snapshot.batteries:
                    self._mode_manager.clear_pending(bat.battery_id)

                result.execution = await self._run_budget_allocator(
                    snapshot,
                )

            elif is_daytime_charge:
                # ----- BRANCH A (legacy): Daytime PV surplus charging -----
                for bat in snapshot.batteries:
                    self._mode_manager.clear_pending(bat.battery_id)

                result.execution = await self._compute_charge_plan(
                    snapshot,
                )

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
                            # PLAT-1619: charge_pv limit must be 0 even at night
                            limit_w = sm.ems_power_limit
                            if target_mode == EMSMode.CHARGE_PV.value:
                                limit_w = _CHARGE_PV_EMS_LIMIT_W
                            self._mode_manager.request_change(
                                battery_id=bat.battery_id,
                                target_mode=target_mode,
                                target_limit_w=limit_w,
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

            # Phase 4b (PLAT-1674): NIGHT_EV — call ev_night + bat_support
            # controllers if configured AND in NIGHT_EV scenario.
            if (
                active_scenario == Scenario.NIGHT_EV
                and self._night_ev_config is not None
            ):
                await self._run_night_ev_controllers(snapshot)

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
    # PLAT-1686: Unified Budget Allocator
    # ------------------------------------------------------------------

    async def _run_budget_allocator(
        self, snapshot: SystemSnapshot,
    ) -> Optional[ExecutionResult]:
        """Run unified budget allocator for daytime PV surplus charging.

        Replaces _compute_charge_plan + EVSurplusController + SurplusDispatch
        with ONE centralized allocation per cycle.
        """
        cfg = self._budget_config
        if cfg is None:
            return None

        # Build BudgetInput from snapshot
        bat_socs = {b.battery_id: b.soc_pct for b in snapshot.batteries}
        bat_caps = {
            b.battery_id: (
                self._battery_configs[b.battery_id].cap_kwh
                if b.battery_id in self._battery_configs
                else cfg.bat_default_cap_kwh
            )
            for b in snapshot.batteries
        }
        bat_powers = {b.battery_id: b.power_w for b in snapshot.batteries}
        bat_modes = {
            b.battery_id: b.ems_mode.value for b in snapshot.batteries
        }

        # Estimate house load from power balance:
        # grid + pv + bat_discharge = house + ev + bat_charge
        # → house = grid + pv + bat_discharge - ev - bat_charge
        # Simplified: house = grid + pv + bat_net - ev
        #   where bat_net = sum(power) (positive=discharge, negative=charge)
        pv_w = snapshot.grid.pv_total_w
        grid_w = snapshot.grid.grid_power_w
        ev_w = snapshot.ev.power_w if snapshot.ev.charging else 0.0
        bat_net_w = sum(b.power_w for b in snapshot.batteries)
        # bat_net > 0 → discharge (adds to supply)
        # bat_net < 0 → charge (subtracts from supply)
        house_w = max(0.0, grid_w + pv_w + bat_net_w - ev_w)

        budget_input = BudgetInput(
            now=snapshot.timestamp,
            grid_power_w=grid_w,
            pv_power_w=pv_w,
            house_load_w=house_w,
            ev_connected=snapshot.ev.connected,
            ev_charging=snapshot.ev.charging,
            ev_current_amps=int(snapshot.ev.current_a),
            ev_soc_pct=snapshot.ev.soc_pct,
            ev_target_soc_pct=snapshot.ev.target_soc_pct,
            bat_socs=bat_socs,
            bat_caps=bat_caps,
            bat_powers=bat_powers,
            bat_modes=bat_modes,
            pv_remaining_kwh=snapshot.grid.pv_forecast_today_kwh,
            consumers=tuple(snapshot.consumers),
        )

        budget_result = budget_allocate(budget_input, cfg, self._budget_state)

        logger.info(
            "BUDGET: %s",
            budget_result.reason,
        )

        if budget_result.commands:
            return await self._executor.execute(budget_result.commands)
        return None

    # ------------------------------------------------------------------
    # PLAT-1674: NIGHT_EV controllers (EV start/ramp + bat support)
    # ------------------------------------------------------------------

    async def _run_night_ev_controllers(
        self, snapshot: SystemSnapshot,
    ) -> None:
        """Drive ev_night_controller + bat_support_controller during NIGHT_EV.

        Pure-function controllers compute commands; this method executes them
        via CommandExecutor (single-writer principle).
        """
        cfg_ev = self._night_ev_config
        if cfg_ev is None:
            return

        # Initialize state if first call
        if self._night_ev_state is None:
            self._night_ev_state = NightEVState()

        # Read EV target SoC — use snapshot.ev.target_soc_pct (set by HA layer)
        target_soc = snapshot.ev.target_soc_pct

        # Use raw grid power as "weighted" input (per 901 ARCH RESPONSE Q3,
        # realtime, not 1h-avg). snapshot.grid.weighted_avg_kw is Ellevio's
        # rolling window — that IS the realtime metric used by guards.
        grid_kw = snapshot.grid.weighted_avg_kw

        decision = ev_night_evaluate(
            now=snapshot.timestamp,
            ev=snapshot.ev,
            grid_weighted_kw=grid_kw,
            target_soc_pct=target_soc,
            state=self._night_ev_state,
            cfg=cfg_ev,
        )

        # Persist new amps + ramp ts
        if decision.new_amps != self._night_ev_state.current_amps:
            self._night_ev_state = NightEVState(
                current_amps=decision.new_amps,
                last_ramp_ts=snapshot.timestamp.timestamp(),
                last_decision_reason=decision.reason,
            )

        # Execute EV commands via single writer
        if decision.commands:
            await self._executor.execute(decision.commands)

        # Bat support — if configured + load is high enough
        cfg_bat = self._bat_support_config
        if cfg_bat is None or not snapshot.batteries:
            return

        ev_kw = snapshot.ev.power_w / _W_TO_KW
        # Estimate baseload from grid power minus known loads
        baseload_kw = max(0.0, abs(snapshot.grid.grid_power_w) / _W_TO_KW - ev_kw)
        total_load_kw = ev_kw + baseload_kw

        bat_infos = [
            BatSupportInfo(
                battery_id=b.battery_id,
                soc_pct=b.soc_pct,
                cap_kwh=b.cap_kwh,
                cell_temp_c=b.cell_temp_c,
                max_discharge_w=(
                    self._battery_configs[b.battery_id].max_discharge_kw * _W_TO_KW
                    if b.battery_id in self._battery_configs
                    else _SAFE_BAT_FALLBACK_W
                ),
                current_mode=b.ems_mode,
            )
            for b in snapshot.batteries
        ]
        bat_decision = bat_support_evaluate(
            BatSupportInput(
                batteries=bat_infos,
                total_load_kw=total_load_kw,
                grid_weighted_kw=grid_kw,
            ),
            cfg_bat,
        )
        if bat_decision.commands:
            await self._executor.execute(bat_decision.commands)

    # ------------------------------------------------------------------
    # PV Surplus Regulator
    # ------------------------------------------------------------------

    # PLAT-1674: max 1pp SoC spread between kontor/forrad
    _SOC_BALANCE_THRESHOLD_PCT: float = 1.0
    _GRID_HYSTERESIS_W: float = 100.0  # Accept <100W import/export
    _SOC_BALANCE_LOWER_RATIO: float = 0.8  # Lower SoC bat gets 80%
    _SOC_BALANCE_HIGHER_RATIO: float = 0.2  # Higher SoC bat gets 20%
    _BALANCE_DISCHARGE_RATE_W: int = 500  # Active balancing discharge rate for higher-SoC bat
    _MIN_MODE_DWELL_CYCLES: int = 2  # Stay in mode at least 2 cycles (60s)

    async def _compute_charge_plan(
        self, snapshot: SystemSnapshot,
    ) -> Optional[ExecutionResult]:
        """SOLE OWNER of mode + ems_power_limit during daytime charge.

        Uses charge_battery mode (mode 11) with ems_power_limit = PV surplus.
        GoodWe firmware respects limit in charge_battery mode (tested live).
        PV has priority — no grid import when limit ≤ PV surplus.

        Regulates every 30s cycle:
        - Read house_grid_power (netto grid import/export)
        - If export > hysteresis: increase limit (absorb more PV)
        - If import > hysteresis: decrease limit (charging too fast)
        - Target: house_grid ≈ 0W (±100W)

        SoC balancing: lower SoC bat gets higher limit.
        When balanced (±2%): proportional by capacity (K75/F25).
        """
        if not snapshot.batteries:
            return None

        # Find house_grid battery for total grid power reading
        house_grid_power_w: float = snapshot.grid.grid_power_w

        # Calculate total PV surplus available for charging.
        # Positive = export (surplus), negative = import (deficit).
        available_surplus_w = max(0, int(-house_grid_power_w))

        # Add back current battery charging power (what bats already absorb)
        for bat in snapshot.batteries:
            if bat.power_w < 0:  # Negative = charging
                available_surplus_w += int(abs(bat.power_w))

        # PLAT-1674: Aggressive grid regulation — target grid ≈ 0W (±100W).
        # Only subtract hysteresis when IMPORTING (avoid oscillation).
        # When EXPORTING: use full surplus (absorb all PV, minimize export).
        if house_grid_power_w > 0:
            # Importing — back off slightly to avoid overshoot
            available_surplus_w = max(
                0, available_surplus_w - int(self._GRID_HYSTERESIS_W),
            )

        # PLAT-1674: FM priority — EV before bat when EV connected + FM
        # FM (06-12): EV gets surplus first, bat gets remainder
        # EM (12+): bat gets surplus first, EV after bat full
        _FM_START_H = 6
        _FM_END_H = 12
        ev_fm_priority = (
            _FM_START_H <= snapshot.hour < _FM_END_H
            and snapshot.ev.connected
            and snapshot.ev.soc_pct < snapshot.ev.target_soc_pct
        )

        if ev_fm_priority and available_surplus_w >= _EV_MIN_SURPLUS_W:
            # FM + EV connected + surplus ≥ 1.4 kW:
            # EV FIRST, remainder to bat (NEVER export)
            ev_cmds = self._ev_surplus_evaluate(
                available_surplus_w, house_grid_power_w, snapshot,
            )
            ev_used_w: int = 0
            if self._ev_surplus and self._ev_surplus.is_charging:
                ev_used_w = int(
                    self._ev_surplus.current_amps
                    * self._ev_surplus._cfg.w_per_amp
                )
            # Remainder after EV → bat (not export!)
            available_surplus_w = max(0, available_surplus_w - ev_used_w)
            if ev_cmds:
                await self._executor.execute(ev_cmds)
            # Fall through to bat allocation with reduced surplus

        # SoC balancing: allocate surplus between batteries
        bat_socs = {b.battery_id: b.soc_pct for b in snapshot.batteries}
        bat_caps = {b.battery_id: b.cap_kwh for b in snapshot.batteries}
        total_cap = sum(bat_caps.values()) or 1.0
        allocations: dict[str, int] = {}

        # PLAT-1695: Stop charging at bat_charge_stop_soc_pct (matches S8 entry)
        # when BudgetConfig is wired; otherwise fall back to MAX_SOC_PCT (100%).
        # Consistent with budget.py:_allocate_bat filter.
        charge_stop_pct: float = (
            self._budget_config.bat_charge_stop_soc_pct
            if self._budget_config is not None
            else MAX_SOC_PCT
        )
        active_bats = [
            b for b in snapshot.batteries if b.soc_pct < charge_stop_pct
        ]
        if not active_bats:
            # All full — standby all, then route surplus to EV/dispatch
            cmds: list[Command] = []
            for bat in snapshot.batteries:
                if bat.ems_mode.value != EMSMode.BATTERY_STANDBY.value:
                    cmds.append(Command(
                        command_type=CommandType.SET_EMS_MODE,
                        target_id=bat.battery_id,
                        value=EMSMode.BATTERY_STANDBY.value,
                        rule_id="PV_CHARGE_PLAN",
                        reason="SoC 100% → standby",
                    ))

            # Bat full → route remaining surplus to EV then dispatch
            ev_cmds = self._ev_surplus_evaluate(
                available_surplus_w, house_grid_power_w, snapshot,
            )
            cmds.extend(ev_cmds)

            ev_w_full: int = 0
            if self._ev_surplus and self._ev_surplus.is_charging:
                ev_w_full = int(
                    self._ev_surplus.current_amps * self._ev_surplus._cfg.w_per_amp
                )
            dispatch_surplus = max(0, available_surplus_w - ev_w_full)
            dispatch_cmds = self._dispatch_evaluate(dispatch_surplus, snapshot)
            cmds.extend(dispatch_cmds)

            if cmds:
                return await self._executor.execute(cmds)
            return None

        discharge_ids: set[str] = set()
        if len(active_bats) == _DUAL_BATTERY_COUNT:
            ids = [b.battery_id for b in active_bats]
            soc_diff = abs(bat_socs[ids[0]] - bat_socs[ids[1]])

            if soc_diff > self._SOC_BALANCE_THRESHOLD_PCT:
                # Unbalanced — lower SoC gets MORE, higher gets LESS
                # (NEVER 100/0 — both must charge to 100%)
                lower_id = (
                    ids[0] if bat_socs[ids[0]] < bat_socs[ids[1]]
                    else ids[1]
                )
                higher_id = ids[1] if lower_id == ids[0] else ids[0]
                # 80/20 split — lower catches up, higher still charges
                allocations[lower_id] = int(
                    available_surplus_w * self._SOC_BALANCE_LOWER_RATIO
                )
                allocations[higher_id] = int(
                    available_surplus_w * self._SOC_BALANCE_HIGHER_RATIO
                )
            else:
                # Balanced — proportional by capacity
                for bid in ids:
                    share = bat_caps[bid] / total_cap
                    allocations[bid] = int(available_surplus_w * share)
        elif len(active_bats) == 1:
            allocations[active_bats[0].battery_id] = available_surplus_w

        # Apply charge_battery + limit per battery
        cmds = []
        for bat in snapshot.batteries:
            limit_w = allocations.get(bat.battery_id, 0)

            if bat.battery_id in discharge_ids:
                # Active SoC balancing: discharge higher-SoC bat at fixed rate
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bat.battery_id,
                    value=EMSMode.DISCHARGE_PV.value,
                    rule_id='PV_CHARGE_PLAN',
                    reason=(
                        f'SoC imbalance: {bat.battery_id}'
                        f' (soc={bat.soc_pct:.0f}%) → discharge_pv balance'
                    ),
                ))
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_POWER_LIMIT,
                    target_id=bat.battery_id,
                    value=self._BALANCE_DISCHARGE_RATE_W,
                    rule_id='PV_CHARGE_PLAN',
                    reason=f'Active balance: limit={self._BALANCE_DISCHARGE_RATE_W}W',
                ))
            elif limit_w > 0:
                # charge_battery with PV surplus limit
                if bat.ems_mode.value != EMSMode.CHARGE_BATTERY.value:
                    cmds.append(Command(
                        command_type=CommandType.SET_EMS_MODE,
                        target_id=bat.battery_id,
                        value=EMSMode.CHARGE_BATTERY.value,
                        rule_id="PV_CHARGE_PLAN",
                        reason=(
                            f"PV surplus {limit_w}W on {bat.battery_id}"
                            f" (soc={bat.soc_pct:.0f}%) → charge_battery"
                        ),
                    ))
                cmds.append(Command(
                    command_type=CommandType.SET_EMS_POWER_LIMIT,
                    target_id=bat.battery_id,
                    value=limit_w,
                    rule_id="PV_CHARGE_PLAN",
                    reason=(
                        f"charge_battery: limit={limit_w}W"
                        f" (PV surplus regulated)"
                    ),
                ))
            else:
                # No allocation → standby
                if bat.ems_mode.value != EMSMode.BATTERY_STANDBY.value:
                    cmds.append(Command(
                        command_type=CommandType.SET_EMS_MODE,
                        target_id=bat.battery_id,
                        value=EMSMode.BATTERY_STANDBY.value,
                        rule_id="PV_CHARGE_PLAN",
                        reason=(
                            f"No PV surplus for {bat.battery_id}"
                            f" → standby"
                        ),
                    ))
                    if bat.ct_placement == CTPlacement.LOCAL_LOAD:
                        cmds.append(Command(
                            command_type=CommandType.SET_EXPORT_LIMIT,
                            target_id=bat.battery_id,
                            value=_DEFAULT_EXPORT_LIMIT_W,
                            rule_id="PV_CHARGE_PLAN",
                            reason="Standby: restore export_limit",
                        ))

        # After bat allocation, check if surplus remains for EV + dispatch
        bat_allocated = sum(allocations.values())
        remaining_surplus = max(0, available_surplus_w - bat_allocated)

        if remaining_surplus > 0:
            ev_cmds = self._ev_surplus_evaluate(
                remaining_surplus, house_grid_power_w, snapshot,
            )
            cmds.extend(ev_cmds)

            # Subtract EV consumption from remaining for dispatch
            ev_consume_w: int = 0
            if self._ev_surplus and self._ev_surplus.is_charging:
                ev_consume_w = int(
                    self._ev_surplus.current_amps * self._ev_surplus._cfg.w_per_amp
                )
            dispatch_surplus = max(0, remaining_surplus - int(ev_consume_w))
            dispatch_cmds = self._dispatch_evaluate(dispatch_surplus, snapshot)
            cmds.extend(dispatch_cmds)

        if cmds:
            exec_result = await self._executor.execute(cmds)
            logger.info(
                "PV CHARGE PLAN: surplus=%dW grid=%dW alloc=%s ev=%s",
                available_surplus_w,
                int(house_grid_power_w),
                {k: f"{v}W" for k, v in allocations.items()},
                (f"{self._ev_surplus.current_amps}A"
                 if self._ev_surplus and self._ev_surplus.is_charging
                 else "off"),
            )
            return exec_result
        return None

    # ------------------------------------------------------------------
    # EV Surplus + Dispatch helpers
    # ------------------------------------------------------------------

    def _ev_surplus_evaluate(
        self,
        surplus_w: int,
        grid_power_w: float,
        snapshot: SystemSnapshot,
    ) -> list[Command]:
        """Evaluate EV surplus charging if controller is configured."""
        if not self._ev_surplus:
            return []
        return self._ev_surplus.evaluate(
            surplus_w=float(surplus_w),
            grid_power_w=grid_power_w,
            ev_connected=snapshot.ev.connected,
            ev_soc_pct=snapshot.ev.soc_pct,
            ev_target_soc_pct=snapshot.ev.target_soc_pct,
        )

    def _dispatch_evaluate(
        self, surplus_w: int, snapshot: Optional[SystemSnapshot] = None,
    ) -> list[Command]:
        """Evaluate surplus dispatch if controller is configured.

        Converts SurplusResult allocations to Command objects.
        """
        if not self._surplus_dispatch or snapshot is None:
            return []
        consumers = list(snapshot.consumers)
        result = self._surplus_dispatch.evaluate(float(surplus_w), consumers)
        cmds: list[Command] = []
        for alloc in result.allocations:
            if alloc.action == "start":
                cmds.append(Command(
                    command_type=CommandType.TURN_ON_CONSUMER,
                    target_id=alloc.consumer_id,
                    value=None,
                    rule_id="PV_SURPLUS_DISPATCH",
                    reason=alloc.reason,
                ))
            elif alloc.action == "stop":
                cmds.append(Command(
                    command_type=CommandType.TURN_OFF_CONSUMER,
                    target_id=alloc.consumer_id,
                    value=None,
                    rule_id="PV_SURPLUS_DISPATCH",
                    reason=alloc.reason,
                ))
        return cmds

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
