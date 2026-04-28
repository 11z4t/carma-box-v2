"""CARMA Box entry point.

Usage:
    python -m main --config config/site.yaml
    python main.py --config /etc/carma-box/site.yaml

The service runs a 30-second control loop that:
1. Collects sensor state from Home Assistant
2. Evaluates safety guards (VETO layer)
3. Runs the decision engine (pure function)
4. Executes commands via adapters
5. Persists state to storage
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
import logging
import os
import signal
import sys
import time
import zoneinfo

import aiohttp.web
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from adapters.easee import EaseeAdapter
from adapters.solcast import SolcastAdapter
from adapters.goodwe import GoodWeAdapter
from adapters.shelly import ShellyAdapter
from adapters.ha_api import HAApiClient
from config.schema import CarmaConfig, load_config
from core.balancer import BalancerConfig, BatteryBalancer
from storage.local_db import CycleLogEntry, LocalDB
from core.engine import ControlEngine, CycleResult
from core.executor import CommandExecutor, ExecutorConfig
from core.guards import ExportGuard, GridGuard, GuardConfig, GuardPolicy
from core.plan_executor import PlanExecutor
from core.mode_change import ModeChangeConfig, ModeChangeManager
from core.models import (
    MAX_SOC_PCT,
    BatteryState,
    ConsumerState,
    EMSMode,
    EVState,
    GridState,
    Scenario,
    SystemSnapshot,
)
from core.ellevio import EllevioConfig, EllevioTracker
from core.day_plan import DayPlan, HourlyForecast
from core.day_planner import (
    BatteryPlanConfig,
    DayPlanConfig,
    EVPlanConfig,
    generate_day_plan,
)
from core.planner import Planner, PlannerConfig
from core.ev_controller import EVAction, EVController, EVControllerConfig
from core.bat_support_controller import BatSupportConfig as BatSupportCtrlCfg

# BudgetConfig no longer imported directly — constructed via config.budget.to_budget_config()
from core.ev_night_controller import NightEVConfig
from core.ev_surplus import EVSurplusConfig, EVSurplusController
from core.surplus_dispatch import SurplusConfig as SurplusDispatchConfig, SurplusDispatch
from health import HealthStatus, Metrics
from core.state_machine import StateMachine, StateMachineConfig
from notifications.slack import SlackNotifier

__version__ = "2.0.0"

logger = logging.getLogger("carma_box")

# Conversion constants — no naked numeric literals in business logic.
_W_TO_KW: float = 1000.0
_MS_PER_S: int = 1000
_PCT_TO_RATIO: float = 100.0

# H6 fix: SoC sensor freshness threshold.
# If the SoC entity's last_updated is older than this, the reading is stale.
# Stale SoC → soc_pct set to -1.0 → sensors_ready=False → battery_standby.
_MAX_SOC_AGE_S: int = 120

# H6 fix: PV surplus threshold to trigger charge_pv when SoC is at floor.
_FLOOR_PV_CHARGE_THRESHOLD_W: float = 500.0

# H6 fix: SoC margin above floor that qualifies for forced charge_pv trigger.
_FLOOR_CHARGE_SOC_MARGIN_PCT: float = 5.0

def _floor_pv_charge_needed(
    soc_pct: float,
    min_soc_pct: float,
    pv_surplus_w: float,
    threshold_w: float = _FLOOR_PV_CHARGE_THRESHOLD_W,
    margin_pct: float = _FLOOR_CHARGE_SOC_MARGIN_PCT,
) -> bool:
    """H6 fix: Return True when battery is at floor AND PV surplus is available.

    When SoC is at or near the floor with active PV export, always charge —
    there is no reason to be in standby when free energy is available and
    the battery is nearly empty.

    soc_pct < 0 means stale/unknown — never triggers (sensors_ready=False path).
    """
    return (
        soc_pct >= 0.0
        and soc_pct <= min_soc_pct + margin_pct
        and pv_surplus_w >= threshold_w
    )


# DayPlan generation fallback constants (PLAT-1627)
_DEFAULT_SOC_PCT: float = 50.0
_PV_P10_RATIO: float = 0.7
_PV_P90_RATIO: float = 1.3
_DEFAULT_BASELOAD_KW: float = 2.5
_DAYLIGHT_HOURS: int = 12
_FALLBACK_WINDOW_START_H: int = 6
_FALLBACK_WINDOW_END_H: int = 22
_PV_REPLAN_FALLBACK_THRESHOLD: float = 0.20
_DYNAMIC_TAK_DEFAULT_KW: float = 3.0
_PRICE_DEFAULT_ORE: float = 100.0
_CYCLE_DURATION_WINDOW: int = 100
_P95_PERCENTILE: float = 0.95
_CYCLE_P95_LOG_INTERVAL: int = 20
# PLAT-1786 hotfix constant: headroom threshold above which EV-start-by-
# Ellevio-headroom will never trigger. Set effectively-infinite so the
# headroom path in core.ev_controller is disabled — EV charging is driven
# by EVSurplusController + NightEV only. Permanent fix in PLAT-1790.
_EV_HEADROOM_DISABLED_W: float = 1_000_000.0


def _apply_addon_overrides(config: CarmaConfig) -> None:
    """Apply HA addon environment-variable overrides to the loaded config.

    When carma-box runs as a Home Assistant Supervisor addon, the entrypoint
    script (run.sh) sets these variables so addon-managed paths (/data/) take
    precedence over whatever the user wrote in site.yaml.

    In standalone (non-addon) mode these env vars are not set, so this
    function is a no-op and normal site.yaml values are used unchanged.

    Variables honoured:
        CARMA_OVERRIDE_LOG_FILE   — overrides config.logging.file
        CARMA_OVERRIDE_LOG_LEVEL  — overrides config.logging.level
        CARMA_OVERRIDE_DB_PATH    — overrides config.storage.sqlite.path
    """
    log_file = os.environ.get("CARMA_OVERRIDE_LOG_FILE", "")
    if log_file:
        config.logging.file = log_file

    log_level = os.environ.get("CARMA_OVERRIDE_LOG_LEVEL", "")
    if log_level:
        config.logging.level = log_level.upper()

    db_path = os.environ.get("CARMA_OVERRIDE_DB_PATH", "")
    if db_path:
        config.storage.sqlite.path = db_path


def setup_logging(config: CarmaConfig) -> None:
    """Configure logging from site.yaml settings.

    Configures the root logger so all module loggers (core.guards,
    adapters.goodwe, etc.) inherit the level and handlers automatically.

    Args:
        config: Validated site configuration.
    """
    log_cfg = config.logging
    level = getattr(logging, log_cfg.level.upper(), logging.INFO)

    # Configure root logger so all child loggers (core.*, adapters.*, etc.) inherit
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # Console handler (always present for systemd journal capture)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler (if log directory exists)
    log_path = Path(log_cfg.file)
    if log_path.parent.exists():
        file_handler = RotatingFileHandler(
            filename=str(log_path),
            maxBytes=log_cfg.max_bytes,
            backupCount=log_cfg.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    else:
        root_logger.warning(
            "Log directory %s does not exist, file logging disabled",
            log_path.parent,
        )


class CarmaBoxService:
    """Main CARMA Box service coordinating all components.

    Lifecycle:
        1. __init__ — load config, create components
        2. start   — enter main loop
        3. stop    — graceful shutdown
    """

    def __init__(self, config: CarmaConfig, ha_api: Optional[HAApiClient] = None) -> None:
        self._config = config
        self._running = False
        self._cycle_count = 0
        self._last_cycle: Optional[datetime] = None
        self._health = HealthStatus(version=__version__)
        self._metrics = Metrics()
        self._health_task: Optional[asyncio.Task[None]] = None
        # PLAT-1753: rolling window of full-cycle durations for p95 profiling.
        # maxlen=100 keeps memory bounded and gives a ~25 min window at 15 s/cycle.
        self._cycle_durations: deque[float] = deque(maxlen=_CYCLE_DURATION_WINDOW)

        # HA API client (None in dry-run/test mode)
        self._ha_api = ha_api
        self._last_plan_hour: int = -1
        self._last_pv_tomorrow: float = -1.0
        # DayPlan: set by generate_day_plan(), read by dashboard sensor (PLAT-1627)
        self._current_day_plan: Optional[Any] = None

        # Create components only when HA API is available
        if ha_api is not None:
            self._setup_components(config, ha_api)
        else:
            self._engine: Optional[ControlEngine] = None
            self._surplus_dispatch: Optional[SurplusDispatch] = None
            self._slack: Optional[SlackNotifier] = None
            self._db: Optional[LocalDB] = None
            self._planner: Optional[Planner] = None
            self._ellevio: Optional[EllevioTracker] = None
            self._ev_controller: Optional[EVController] = None
            self._solcast: Optional[SolcastAdapter] = None
            self._consumer_configs = config.consumers
            # Dry-run: create PlanExecutor with minimal deps for generate_48h
            _dry_planner = Planner(PlannerConfig())
            _dry_guard = GuardPolicy(GridGuard(GuardConfig()), ExportGuard())
            self._plan_executor = PlanExecutor(
                planner=_dry_planner,
                ha_api=None,
                config=config,
                guard_policy=_dry_guard,
            )

        logger.info(
            "CarmaBoxService initialized for site '%s' (cycle=%ds, live=%s)",
            config.site.name,
            config.control.cycle_interval_s,
            ha_api is not None,
        )

    def _setup_components(self, config: CarmaConfig, ha_api: HAApiClient) -> None:
        """Create all runtime components from config."""
        # Adapters: one GoodWeAdapter per battery
        inverters: dict[str, GoodWeAdapter] = {}
        for bat_cfg in config.batteries:
            inverters[bat_cfg.id] = GoodWeAdapter(ha_api, bat_cfg)

        # Guard config from site.yaml
        g = config.guards
        guard_cfg = GuardConfig(
            tak_kw=config.grid.ellevio.tak_kw,
            night_weight=config.grid.ellevio.night_weight,
            day_weight=config.grid.ellevio.day_weight,
            night_start_hour=config.grid.ellevio.night_start_hour,
            night_end_hour=config.grid.ellevio.night_end_hour,
            margin=config.grid.ellevio.margin,
            emergency_factor=config.grid.ellevio.emergency_factor,
            recovery_hold_s=g.g3_ellevio_breach.recovery_hold_s,
            normal_floor_pct=g.g1_soc_floor.floor_pct,
            cold_floor_pct=g.g1_soc_floor.cold_floor_pct,
            freeze_floor_pct=g.g1_soc_floor.freeze_floor_pct,
            max_changes_per_window=g.g5_oscillation.max_changes_per_window,
            window_s=g.g5_oscillation.window_s,
            doubled_deadband_s=g.g5_oscillation.doubled_deadband_s,
            stale_threshold_s=g.g6_stale_data.threshold_s,
            ha_health_timeout_s=g.g7_communication_lost.ha_health_timeout_s,
        )

        grid_guard = GridGuard(guard_cfg)
        guard_policy = GuardPolicy(grid_guard, ExportGuard())
        sm = StateMachine(
            StateMachineConfig(
                start_scenario=Scenario[config.control.start_scenario],
                min_dwell_s=config.control.scenario_transition_s,
                surplus_entry_soc_pct=config.control.battery_gate.charge_stop_soc_pct,
            )
        )
        balancer = BatteryBalancer(
            BalancerConfig(
                normal_floor_pct=guard_cfg.normal_floor_pct,
                cold_floor_pct=guard_cfg.cold_floor_pct,
                freeze_floor_pct=guard_cfg.freeze_floor_pct,
            )
        )
        mode_mgr = ModeChangeManager(
            ModeChangeConfig(
                clear_wait_s=float(config.control.mode_change_clear_wait_s),
                standby_wait_s=config.control.standby_intermediate_s,
                set_wait_s=float(config.control.mode_change_set_wait_s),
                verify_wait_s=float(config.control.mode_change_verify_wait_s),
            )
        )
        # EV charger adapter — wired into executor for EV commands
        ev_adapter: Optional[EaseeAdapter] = None
        if config.ev_charger and config.ev_charger.charger_id:
            ev_adapter = EaseeAdapter(
                ha_api=ha_api,
                config=config.ev_charger,
            )
            logger.info(
                "EaseeAdapter wired: charger_id=%s",
                config.ev_charger.charger_id,
            )

        # PLAT-1700: Dispatchable consumers — factory per consumer.adapter.
        # read_only + dispatchable=False consumers are skipped → v2 cannot
        # control them (safety: prevents miner power-cycling via Shelly relay).
        from adapters.goldshell import GoldshellMinerAdapter
        from core.executor import LoadPort

        consumers: dict[str, LoadPort] = {}
        skipped: list[str] = []
        for consumer_cfg in config.consumers:
            if not consumer_cfg.dispatchable:
                skipped.append(f"{consumer_cfg.id}(dispatchable=False)")
                continue
            if consumer_cfg.adapter == "read_only":
                skipped.append(f"{consumer_cfg.id}(read_only)")
                continue
            if consumer_cfg.adapter == "goldshell_miner":
                consumers[consumer_cfg.id] = GoldshellMinerAdapter(
                    ha_api=ha_api,
                    consumer_id=consumer_cfg.id,
                    entity_power=consumer_cfg.entity_power,
                )
                logger.info(
                    "GoldshellMinerAdapter wired (stub): %s",
                    consumer_cfg.id,
                )
                continue
            # Default: Shelly relay
            if consumer_cfg.entity_switch:
                consumers[consumer_cfg.id] = ShellyAdapter(
                    ha_api=ha_api,
                    consumer_id=consumer_cfg.id,
                    entity_switch=consumer_cfg.entity_switch,
                    entity_power=consumer_cfg.entity_power,
                    load_type_str=consumer_cfg.type,
                )
        if consumers:
            logger.info("Consumer adapters wired: %s", ", ".join(consumers.keys()))
        if skipped:
            logger.info("Consumers SKIPPED (read-only): %s", ", ".join(skipped))

        executor = CommandExecutor(
            inverters=dict(inverters),
            mode_manager=mode_mgr,
            config=ExecutorConfig(
                mode_change_cooldown_s=config.control.mode_change_cooldown_s,
            ),
            ha_api=ha_api,
            ev_charger=ev_adapter,
            consumers=consumers,
        )

        # Slack notifier
        self._slack = SlackNotifier()
        self._last_scenario: Optional[str] = None

        # Local DB
        db_path = config.storage.sqlite.path
        self._db = LocalDB(db_path)

        # Planner
        self._planner = Planner(
            PlannerConfig(
                ev_target_soc_pct=config.ev.daily_target_soc_pct,
                pv_high_threshold_kwh=float(
                    config.night_plan.house_baseload_kw * config.night_plan.night_hours
                ),
                grid_charge_max_soc_pct=config.night_plan.grid_charge_max_soc_pct,
                grid_charge_price_threshold_ore=config.night_plan.grid_charge_price_threshold_ore,
            )
        )
        self._plan_executor = PlanExecutor(
            planner=self._planner,
            ha_api=ha_api,
            config=config,
            guard_policy=guard_policy,
        )

        # Solcast adapter — hourly PV forecast for DayPlan (PLAT-1659)
        self._solcast = None
        if config.pv_forecast.entity_today:
            self._solcast = SolcastAdapter(
                ha_api=ha_api,
                entity_today=config.pv_forecast.entity_today,
                entity_tomorrow=config.pv_forecast.entity_tomorrow,
            )
            logger.info("SolcastAdapter wired: %s", config.pv_forecast.entity_today)

        # Ellevio peak tracker
        self._ellevio = EllevioTracker(
            EllevioConfig(
                tak_kw=config.grid.ellevio.tak_kw,
                night_weight=config.grid.ellevio.night_weight,
                day_weight=config.grid.ellevio.day_weight,
                night_start_h=config.grid.ellevio.night_start_hour,
                night_end_h=config.grid.ellevio.night_end_hour,
            )
        )

        # EV controller
        # HOTFIX 2026-04-20 (PLAT-1786): start_headroom_w = _EV_HEADROOM_DISABLED_W
        # to effectively disable headroom-based EV starts. User rule: EV charges
        # ONLY from PV surplus (via EVSurplusController), never from Ellevio
        # headroom triggers. Permanent fix in PLAT-1790.
        self._ev_controller = EVController(
            EVControllerConfig(
                target_soc_pct=config.ev.daily_target_soc_pct,
                start_headroom_w=_EV_HEADROOM_DISABLED_W,
            )
        )

        # Surplus dispatch from consumer configs
        surplus_cfg = config.surplus
        self._surplus_dispatch = SurplusDispatch(
            SurplusDispatchConfig(
                stop_threshold_w=surplus_cfg.stop_threshold_kw * _W_TO_KW,
                start_delay_s=surplus_cfg.start_delay_s,
                max_switches_per_window=surplus_cfg.max_switches_per_window,
                switch_window_s=surplus_cfg.switch_window_min * 60,
                bump_delay_s=surplus_cfg.bump_delay_s,
                deadband_w=float(config.control.deadband.normal_w),
                doubled_deadband_w=float(config.control.deadband.doubled_w),
                doubled_deadband_s=float(config.control.deadband.doubled_duration_s),
            )
        )
        self._consumer_configs = config.consumers

        # EV surplus controller — PV-only EV charging with ramp
        ev_surplus_ctrl: Optional[EVSurplusController] = None
        if ev_adapter is not None:
            ev_ramp = config.ev_charger.ramp
            ev_surplus_ctrl = EVSurplusController(
                EVSurplusConfig(
                    min_amps=config.ev_charger.min_amps,
                    max_amps=config.ev_charger.max_amps,
                    phases=config.ev_charger.phases,
                    voltage_v=config.ev_charger.voltage_v,
                    step_amps=ev_ramp.step_amps,
                )
            )
            logger.info(
                "EVSurplusController wired: %d-%dA, %d-phase",
                config.ev_charger.min_amps,
                config.ev_charger.max_amps,
                config.ev_charger.phases,
            )

        # H2: map battery_id → config so engine can read per-battery limits
        battery_cfg_map = {bc.id: bc for bc in config.batteries}

        # PLAT-1674: Wire up NightEVController + BatSupportController
        night_ev_cfg: Optional[NightEVConfig] = None
        bat_support_cfg: Optional[BatSupportCtrlCfg] = None
        if config.night_ev.enabled:
            night_ev_cfg = NightEVConfig(
                night_start_hour=config.night_ev.night_start_hour,
                night_end_hour=config.night_ev.night_end_hour,
                start_amps=config.night_ev.start_amps,
                max_amps=config.night_ev.max_amps,
                min_amps=config.night_ev.min_amps,
                ramp_step_amps=config.night_ev.ramp_step_amps,
                ramp_interval_s=config.night_ev.ramp_interval_s,
                tak_weighted_kw=config.night_ev.tak_weighted_kw,
                grid_safety_margin_up=config.night_ev.grid_safety_margin_up,
                grid_safety_margin_down=config.night_ev.grid_safety_margin_down,
            )
        if config.bat_support.enabled:
            bat_support_cfg = BatSupportCtrlCfg(
                enabled=config.bat_support.enabled,
                tak_weighted_kw=config.bat_support.tak_weighted_kw,
                night_weight=config.bat_support.night_weight,
                safety_margin=config.bat_support.safety_margin,
                min_soc_normal_pct=config.bat_support.min_soc_normal_pct,
                min_soc_cold_pct=config.bat_support.min_soc_cold_pct,
                cold_temp_c=config.bat_support.cold_temp_c,
            )

        self._engine = ControlEngine(
            grid_guard,
            sm,
            balancer,
            mode_mgr,
            executor,
            battery_configs=battery_cfg_map,
            ev_surplus=ev_surplus_ctrl,
            surplus_dispatch=self._surplus_dispatch,
            night_ev_config=night_ev_cfg,
            bat_support_config=bat_support_cfg,
        )
        # PLAT-1686: Activate Budget Allocator for daytime PV charging
        # PLAT-1695: Share charge_stop_soc_pct with state machine via config
        # PLAT-1748: Map all BudgetSection fields from site.yaml
        if config.ev_charger:
            # Build all tunables from budget: section in site.yaml, then
            # apply the PLAT-1695 invariant override: bat_charge_stop_soc_pct
            # must always equal control.battery_gate.charge_stop_soc_pct so
            # that S8 (PV_SURPLUS entry SoC) and the budget stop SoC stay in
            # sync. A drift between them causes a dead zone where the state
            # machine says "surplus" but the budget keeps charging.
            import dataclasses

            self._engine._budget_config = dataclasses.replace(
                config.budget.to_budget_config(),
                bat_charge_stop_soc_pct=(config.control.battery_gate.charge_stop_soc_pct),
            )

    @property
    def config(self) -> CarmaConfig:
        """Return the loaded configuration."""
        return self._config

    @property
    def is_running(self) -> bool:
        """Whether the main loop is active."""
        return self._running

    @property
    def cycle_p95_s(self) -> float:
        """95th-percentile full-cycle duration from the last 100 cycles (seconds).

        PLAT-1753: Use for capacity planning — target p95 < 0.5 s.
        Returns 0.0 when no cycles have been recorded yet.
        """
        if not self._cycle_durations:
            return 0.0
        sorted_d = sorted(self._cycle_durations)
        idx = int(len(sorted_d) * _P95_PERCENTILE)
        return sorted_d[min(idx, len(sorted_d) - 1)]

    async def start(self) -> None:
        """Start the main control loop.

        Runs until stop() is called or a signal is received.
        """
        self._running = True
        cycle_s = self._config.control.cycle_interval_s
        health_port = self._config.health.port
        logger.info(
            "Starting main loop (cycle=%ds, health=:%d)",
            cycle_s,
            health_port,
        )

        # Initialize local DB
        if self._db:
            try:
                await self._db.initialize()
            except Exception as exc:
                logger.warning("DB init failed: %s", exc)

        # Start health HTTP server
        self._health_task = asyncio.create_task(
            self._start_health_server(health_port),
        )
        self._health_task.add_done_callback(self._on_health_done)

        try:
            while self._running:
                await self._run_cycle()
                await asyncio.sleep(cycle_s)
        except asyncio.CancelledError:
            logger.info("Main loop cancelled")
        finally:
            self._running = False
            if self._health_task:
                self._health_task.cancel()
            logger.info("Main loop stopped after %d cycles", self._cycle_count)

    async def stop(self) -> None:
        """Signal the main loop to stop gracefully."""
        logger.info("Stop requested")
        self._running = False

    async def shutdown(self) -> None:
        """Graceful shutdown: close all external connections.

        Called after the main loop exits (SIGTERM, SIGINT, CancelledError).
        Closes HA API session so aiohttp connectors are released cleanly.
        """
        logger.info("Shutting down CARMA Box (closing connections)...")
        if self._ha_api is not None:
            try:
                await self._ha_api.close()
            except Exception as exc:  # pragma: no cover
                logger.warning("Error closing HA API session: %s", exc)
        logger.info("Shutdown complete")

    async def _run_cycle(self) -> None:
        """Execute one control cycle.

        Pipeline: COLLECT → GUARD → SCENARIO → BALANCE → EXECUTE → PERSIST
        """
        self._cycle_count += 1
        self._last_cycle = datetime.now(tz=timezone.utc)
        # PLAT-1753: measure full-cycle wall time for p95 profiling.
        _cycle_start = time.monotonic()

        if self._engine is None or self._ha_api is None:
            logger.debug("Cycle %d (no engine — dry run)", self._cycle_count)
            return

        # Phase 1: COLLECT — read all sensors via HA API
        ha_connected = await self._ha_api.health_check()
        snapshot = await self._collect_snapshot(ha_connected)
        if snapshot is None:
            logger.warning("Cycle %d: failed to collect snapshot", self._cycle_count)
            return

        # Phase 1.4: PLAN GENERATION at configured hours
        plan_hours = set(self._planner._config.plan_hours) if self._planner else set()
        # Check for PV forecast change (>20% delta → re-plan)
        pv_changed = False
        pv_tomorrow = snapshot.grid.pv_forecast_tomorrow_kwh
        if self._last_pv_tomorrow > 0:
            delta_pct = abs(pv_tomorrow - self._last_pv_tomorrow) / self._last_pv_tomorrow
            replan_threshold = (
                self._planner._config.pv_replan_threshold
                if self._planner
                else _PV_REPLAN_FALLBACK_THRESHOLD
            )
            if delta_pct > replan_threshold:
                pv_changed = True
                logger.info(
                    "PV forecast changed %.0f → %.0f kWh (%.0f%%) — re-planning",
                    self._last_pv_tomorrow,
                    pv_tomorrow,
                    delta_pct * 100,
                )
        self._last_pv_tomorrow = pv_tomorrow

        # Startup replan: first cycle has no plan → generate immediately
        startup_replan = self._last_plan_hour == -1

        # Force replan via HA input_boolean
        force_replan = await self._check_force_replan()

        scheduled = snapshot.hour in plan_hours and snapshot.hour != self._last_plan_hour
        if self._planner and (scheduled or pv_changed or startup_replan or force_replan):
            reason = (
                "startup"
                if startup_replan
                else "force_replan"
                if force_replan
                else "pv_changed"
                if pv_changed
                else f"scheduled (hour={snapshot.hour})"
            )
            logger.info("Plan generation triggered: %s", reason)
            self._last_plan_hour = snapshot.hour
            is_forced = startup_replan or force_replan
            await self._plan_executor.generate(snapshot, force=is_forced)

            # Generate DayPlan for dashboard sensor (PLAT-1627)
            try:
                self._current_day_plan = await self._generate_day_plan(snapshot)
            except Exception as exc:
                logger.warning("DayPlan generation skipped: %s", exc)

        # Phase 1.5: ELLEVIO TRACKING — update weighted hourly average
        if self._ellevio:
            grid_kw = snapshot.grid.grid_power_w / _W_TO_KW
            self._ellevio.update(grid_kw, snapshot.timestamp)

        # Phase 1.6: MANUAL OVERRIDE — read HA helpers, set on state machine
        await self._apply_manual_override()

        # Phase 1.7: H6 fix — floor+PV charge_pv trigger.
        # If any battery is at/near its SoC floor AND PV surplus is available,
        # force charge_pv immediately. Prevents standby-while-exporting edge case
        # that caused the 2026-04-26 charge-failure incident.
        # soc_pct < 0 (stale) is explicitly excluded by _floor_pv_charge_needed().
        if self._engine is not None:
            pv_surplus_w = -snapshot.grid.grid_power_w
            for bat in snapshot.batteries:
                bat_cfg_match = next(
                    (b for b in self._config.batteries if b.id == bat.battery_id), None
                )
                if bat_cfg_match is None:
                    continue
                if _floor_pv_charge_needed(
                    soc_pct=bat.soc_pct,
                    min_soc_pct=bat_cfg_match.min_soc_pct,
                    pv_surplus_w=pv_surplus_w,
                ):
                    logger.info(
                        "H6 floor+PV trigger: %s soc=%.0f%% floor=%.0f%% pv=%.0fW → charge_pv",
                        bat.battery_id, bat.soc_pct, bat_cfg_match.min_soc_pct, pv_surplus_w,
                    )
                    self._engine._mode_manager.request_change(
                        battery_id=bat.battery_id,
                        target_mode="charge_pv",
                        reason=f"H6: floor+PV soc={bat.soc_pct:.0f}% pv={pv_surplus_w:.0f}W",
                    )

        # Phases 2-6: delegated to ControlEngine
        data_age_s = (datetime.now(tz=timezone.utc) - snapshot.timestamp).total_seconds()
        cycle_result = await self._engine.run_cycle(
            snapshot=snapshot,
            ha_connected=ha_connected,
            data_age_s=data_age_s,
        )

        # Phase 6.5: PLAN EXECUTION — execute active night/evening plan
        await self._execute_plan(snapshot)

        # Phase 6.6: EV CONTROLLER — evaluate charging decision
        await self._evaluate_ev(snapshot)

        # Phase 7: SURPLUS DISPATCH — manage dispatchable consumers
        if snapshot.consumers:
            await self._execute_surplus(snapshot)

        # Phase 8: DASHBOARD WRITE-BACK — update HA sensors for dashboard
        try:
            await self._write_dashboard_state(snapshot, cycle_result)
        except Exception as exc:
            logger.error("Dashboard write failed: %s", exc)

        if cycle_result.error:
            logger.error(
                "[%s] Cycle %d error: %s",
                cycle_result.cycle_id,
                self._cycle_count,
                cycle_result.error,
            )

        # Update health + metrics
        self._health.scenario = cycle_result.scenario.value
        self._health.cycle_count = self._cycle_count
        self._health.last_cycle_s = cycle_result.elapsed_s
        self._health.ha_connected = ha_connected
        self._health.guard_level = cycle_result.guard.level.value if cycle_result.guard else "ok"
        self._metrics.increment_cycle()
        if cycle_result.guard and cycle_result.guard.commands:
            self._metrics.increment_guard_trigger()

        logger.info(
            "[%s] Cycle %d complete in %.0fms (scenario=%s, guard=%s)",
            cycle_result.cycle_id,
            self._cycle_count,
            cycle_result.elapsed_s * _MS_PER_S,
            cycle_result.scenario.value,
            cycle_result.guard.level.value if cycle_result.guard else "n/a",
        )

        # Phase 9: SLACK NOTIFICATIONS — scenario transitions + guard triggers
        if self._slack:
            scenario_name = cycle_result.scenario.value
            if self._last_scenario and scenario_name != self._last_scenario:
                await self._slack.notify(
                    "scenario_transition",
                    f"{self._last_scenario} → {scenario_name}",
                )
            self._last_scenario = scenario_name

            if cycle_result.guard and cycle_result.guard.commands:
                guard_level = cycle_result.guard.level.value
                await self._slack.notify(
                    "guard_trigger",
                    f"{guard_level}: "
                    + ", ".join(c.command_type.value for c in cycle_result.guard.commands),
                    severity=guard_level,
                )

        # Phase 9.5: SOC SNAPSHOT — daily at 00:00 and 06:00
        if self._db and self._planner:
            snap_hours = (0, self._planner._config.night_end_hour)
            if snapshot.hour in snap_hours and snapshot.minute == 0 and self._cycle_count > 1:
                try:
                    from storage.local_db import EventLogEntry

                    for bat in snapshot.batteries:
                        await self._db.write_event(
                            EventLogEntry(
                                timestamp=snapshot.timestamp.isoformat(),
                                event_type="soc_snapshot",
                                source=bat.battery_id,
                                message=f"SoC={bat.soc_pct:.1f}%",
                                data=f"hour={snapshot.hour}",
                            )
                        )
                    logger.info(
                        "SoC snapshot: %s",
                        {b.battery_id: f"{b.soc_pct:.0f}%" for b in snapshot.batteries},
                    )
                except Exception as exc:
                    logger.debug("SoC snapshot: %s", exc)

        # Phase 10: PERSIST — write cycle to SQLite
        if self._db:
            try:
                guard_level = cycle_result.guard.level.value if cycle_result.guard else "ok"
                headroom = cycle_result.guard.headroom_kw if cycle_result.guard else 0.0
                violations = "; ".join(cycle_result.guard.violations) if cycle_result.guard else ""
                await self._db.write_cycle(
                    CycleLogEntry(
                        cycle_id=cycle_result.cycle_id,
                        timestamp=snapshot.timestamp.isoformat(),
                        scenario=cycle_result.scenario.value,
                        guard_level=guard_level,
                        headroom_kw=headroom,
                        elapsed_s=cycle_result.elapsed_s,
                        violations=violations,
                    )
                )
            except Exception as exc:
                logger.debug("Cycle log write failed: %s", exc)

        # PLAT-1753: record full-cycle wall time and log p95 every 20 cycles.
        _cycle_wall_s = time.monotonic() - _cycle_start
        self._cycle_durations.append(_cycle_wall_s)
        if self._cycle_count % _CYCLE_P95_LOG_INTERVAL == 0:
            logger.info(
                "PLAT-1753 cycle timing: p95=%.0fms over last %d cycles",
                self.cycle_p95_s * _MS_PER_S,
                len(self._cycle_durations),
            )

    def _on_health_done(self, task: asyncio.Task[None]) -> None:
        """Log health server task completion/failure."""
        self._health_task = None
        if task.cancelled():
            return
        if task.exception():
            logger.error("Health server failed: %s", task.exception())

    async def _generate_day_plan(self, snapshot: SystemSnapshot) -> Optional[DayPlan]:
        """Generate DayPlan from current state + Solcast forecast.

        PLAT-1627: Creates DayPlan for dashboard sensor and Excel report.
        Returns None if insufficient data.
        """
        cfg = self._config
        batteries = tuple(
            BatteryPlanConfig(
                battery_id=bat_cfg.id,
                cap_kwh=bat_cfg.cap_kwh,
                max_charge_kw=bat_cfg.max_charge_kw,
                efficiency=bat_cfg.efficiency,
                min_soc_pct=bat_cfg.min_soc_pct,
                current_soc_pct=next(
                    (b.soc_pct for b in snapshot.batteries if b.battery_id == bat_cfg.id),
                    _DEFAULT_SOC_PCT,
                ),
            )
            for bat_cfg in cfg.batteries
        )
        ev = EVPlanConfig(
            min_amps=cfg.ev_charger.min_amps,
            max_amps=cfg.ev_charger.max_amps,
            phases=cfg.ev_charger.phases,
            voltage_v=cfg.ev_charger.voltage_v,
            current_soc_pct=snapshot.ev.soc_pct,
            target_soc_pct=snapshot.ev.target_soc_pct,
            battery_kwh=cfg.ev.battery_kwh,
            efficiency=cfg.ev.efficiency,
            connected=snapshot.ev.connected,
        )
        # Build hourly PV forecast from Solcast adapter (PLAT-1659)
        pv_hourly: dict[int, HourlyForecast] = {}
        if self._solcast:
            try:
                pv_hourly = await self._solcast.get_hourly_forecast(snapshot.hour)
            except Exception as exc:
                logger.debug("Solcast hourly fetch skipped: %s", exc)
        if not pv_hourly:
            # Fallback: flat distribution of daily total
            total = snapshot.grid.pv_forecast_today_kwh
            per_hour = total / _DAYLIGHT_HOURS if total > 0 else 0.0
            pv_hourly = {
                h: HourlyForecast(
                    p10_kwh=per_hour * _PV_P10_RATIO,
                    p50_kwh=per_hour,
                    p90_kwh=per_hour * _PV_P90_RATIO,
                )
                for h in range(_FALLBACK_WINDOW_START_H, _FALLBACK_WINDOW_END_H)
            }

        plan_cfg = DayPlanConfig(
            batteries=batteries,
            ev=ev,
            baseload_kw=cfg.night_plan.house_baseload_kw if self._planner else _DEFAULT_BASELOAD_KW,
        )
        try:
            return generate_day_plan(pv_hourly, plan_cfg)
        except Exception as exc:
            logger.error("DayPlan generation failed: %s", exc)
            return None

    @staticmethod
    def _entity_domain(entity_id: str) -> str:
        """Extract domain from entity_id (e.g. 'switch.x' → 'switch')."""
        return entity_id.split(".")[0] if "." in entity_id else "homeassistant"

    async def _start_health_server(self, port: int) -> None:
        """Start aiohttp health endpoint server."""
        app = aiohttp.web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/metrics", self._handle_metrics)
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "0.0.0.0", port)
        try:
            await site.start()
            logger.info("Health server started on :%d", port)
            await asyncio.Event().wait()  # Run until cancelled
        except asyncio.CancelledError:
            await runner.cleanup()

    async def _handle_health(
        self,
        request: aiohttp.web.Request,
    ) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            text=self._health.to_json(),
            content_type="application/json",
        )

    async def _handle_metrics(
        self,
        request: aiohttp.web.Request,
    ) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            text=self._metrics.to_prometheus(),
            content_type="text/plain",
        )

    async def _collect_snapshot(self, ha_connected: bool) -> Optional[SystemSnapshot]:
        """Collect system state from all adapters."""
        if not ha_connected or self._ha_api is None:
            return None

        try:
            tz = zoneinfo.ZoneInfo(self._config.site.timezone)
            now = datetime.now(tz=tz)
            cfg = self._config

            # PLAT-1753: Atomic prefetch — one GET /api/states at cycle start.
            # All subsequent get_states_batch() calls in this cycle use the
            # cached response, ensuring every sensor reflects the same instant.
            await self._ha_api.warm_batch_cache()

            # PLAT-1757 + PLAT-1753: Atomic battery sensor read.
            # warm_batch_cache() (above, PLAT-1753) pre-fetches all HA states
            # in one GET /api/states. Collecting ALL battery entity IDs into a
            # single get_states_batch() call (PLAT-1757) ensures every battery
            # reads from the exact same HA snapshot — eliminating the 50-100 ms
            # sensor-skew race where sequential per-battery calls saw divergent
            # SoC that did not exist in hardware.
            all_bat_entity_ids: list[str] = []
            for bat_cfg in cfg.batteries:
                ents = bat_cfg.entities
                all_bat_entity_ids.extend(
                    [
                        ents.soc,
                        ents.power,
                        ents.cell_temp,
                        ents.pv_power,
                        ents.grid_power,
                        ents.load_power,
                        ents.ems_mode,
                        ents.ems_power_limit,
                        ents.fast_charging,
                        ents.soh,
                    ]
                )
            bat_batch = await self._ha_api.get_states_batch(all_bat_entity_ids)

            def _float(eid: str, default: float = 0.0) -> float:
                s = bat_batch.get(eid, {}).get("state")
                if s in (None, "unavailable", "unknown"):
                    return default
                try:
                    return float(s)
                except (ValueError, TypeError):
                    return default

            batteries: list[BatteryState] = []
            for bat_cfg in cfg.batteries:
                ents = bat_cfg.entities

                soc = _float(ents.soc)

                # H6 fix: SoC freshness guard.
                # Check last_updated from the batch response. If the SoC entity
                # has not been updated within _MAX_SOC_AGE_S seconds, the value
                # is stale — set soc=-1.0 so downstream sees sensors_ready=False.
                soc_last_updated = bat_batch.get(ents.soc, {}).get("last_updated")
                if soc_last_updated and soc >= 0.0:
                    try:
                        soc_ts = datetime.fromisoformat(
                            soc_last_updated.replace("Z", "+00:00")
                        )
                        soc_age_s = (
                            datetime.now(tz=timezone.utc) - soc_ts
                        ).total_seconds()
                        if soc_age_s > _MAX_SOC_AGE_S:
                            logger.warning(
                                "H6 STALE SOC: %s age=%.0fs > %ds — sensors_ready=False",
                                bat_cfg.id, soc_age_s, _MAX_SOC_AGE_S,
                            )
                            soc = -1.0
                    except (ValueError, AttributeError):
                        pass

                # PLAT-1539: Detect GoodWe bridge offline
                soc_state = bat_batch.get(ents.soc, {}).get("state")
                if soc_state in ("unavailable", "unknown"):
                    logger.warning(
                        "GoodWe %s OFFLINE — sensor unavailable",
                        bat_cfg.id,
                    )
                    if self._slack:
                        await self._slack.notify(
                            "communication_lost",
                            f"GoodWe {bat_cfg.id} bridge OFFLINE",
                            severity="critical",
                        )

                floor = cfg.guards.g1_soc_floor.floor_pct  # From site.yaml
                soc_ratio = (soc - floor) / _PCT_TO_RATIO
                avail = max(0.0, soc_ratio * bat_cfg.cap_kwh * bat_cfg.efficiency)

                batteries.append(
                    BatteryState(
                        battery_id=bat_cfg.id,
                        soc_pct=soc,
                        power_w=_float(ents.power),
                        cell_temp_c=_float(ents.cell_temp, bat_cfg.default_cell_temp_c),
                        pv_power_w=max(0.0, _float(ents.pv_power)),
                        grid_power_w=_float(ents.grid_power),
                        load_power_w=max(0.0, _float(ents.load_power)),
                        ems_mode=EMSMode(
                            bat_batch.get(ents.ems_mode, {}).get("state", "battery_standby")
                        ),
                        ems_power_limit_w=int(_float(ents.ems_power_limit)),
                        fast_charging=bat_batch.get(ents.fast_charging, {}).get("state") == "on",
                        soh_pct=_float(ents.soh, bat_cfg.default_soh_pct),
                        cap_kwh=bat_cfg.cap_kwh,
                        ct_placement=bat_cfg.ct_placement,
                        available_kwh=avail,
                    )
                )

            # Read EV state
            # PLAT-1753: ev.entities.soc merged into batch — no separate get_state call.
            ev_ents = cfg.ev_charger.entities
            ev_batch = await self._ha_api.get_states_batch(
                [
                    ev_ents.status,
                    ev_ents.power,
                    ev_ents.current,
                    ev_ents.enabled,
                    ev_ents.reason_for_no_current,
                    cfg.ev.entities.soc,
                ]
            )
            ev_soc_raw = ev_batch.get(cfg.ev.entities.soc, {}).get("state")
            ev_soc = float(ev_soc_raw) if ev_soc_raw else -1.0
            ev_status = ev_batch.get(ev_ents.status, {}).get("state", "disconnected")
            ev_enabled = ev_batch.get(ev_ents.enabled, {}).get("state") == "on"
            # Easee reports "disconnected" when disabled even if cable plugged in
            # If disabled → assume connected (cable always plugged in at home)
            ev_connected = (
                ev_status.lower()
                in (
                    "awaiting_start",
                    "charging",
                    "completed",
                    "ready_to_charge",
                )
                or not ev_enabled  # disabled = car connected but charger off
            )

            def _ev_float(eid: str) -> float:
                s = ev_batch.get(eid, {}).get("state")
                try:
                    return float(s) if s else 0.0
                except (ValueError, TypeError):
                    return 0.0

            ev = EVState(
                soc_pct=ev_soc,
                connected=ev_connected,
                charging=ev_status.lower() == "charging",
                power_w=_ev_float(ev_ents.power) * _W_TO_KW,  # kW → W
                current_a=_ev_float(ev_ents.current),
                charger_status=ev_status,
                reason_for_no_current=ev_batch.get(ev_ents.reason_for_no_current, {}).get(
                    "state", ""
                ),
                target_soc_pct=cfg.ev.daily_target_soc_pct,
            )

            # Read grid state
            grid_ents = cfg.grid.ellevio
            grid_batch = await self._ha_api.get_states_batch(
                [
                    grid_ents.entity_weighted_avg,
                    grid_ents.entity_current_peak,
                    grid_ents.entity_dynamic_tak,
                    cfg.pricing.entity,
                    cfg.pv_forecast.entity_today,
                    cfg.pv_forecast.entity_tomorrow,
                ]
            )

            def _grid_float(eid: str, default: float = 0.0) -> float:
                s = grid_batch.get(eid, {}).get("state")
                try:
                    return float(s) if s else default
                except (ValueError, TypeError):
                    return default

            # Total PV from all batteries
            pv_total = sum(b.pv_power_w for b in batteries)

            grid = GridState(
                grid_power_w=batteries[0].grid_power_w if batteries else 0.0,
                weighted_avg_kw=_grid_float(grid_ents.entity_weighted_avg),
                current_peak_kw=_grid_float(grid_ents.entity_current_peak),
                dynamic_tak_kw=_grid_float(grid_ents.entity_dynamic_tak, _DYNAMIC_TAK_DEFAULT_KW),
                pv_total_w=pv_total,
                price_ore=_grid_float(cfg.pricing.entity, _PRICE_DEFAULT_ORE) * _PCT_TO_RATIO,
                pv_forecast_today_kwh=_grid_float(cfg.pv_forecast.entity_today),
                pv_forecast_tomorrow_kwh=_grid_float(cfg.pv_forecast.entity_tomorrow),
            )

            return SystemSnapshot(
                timestamp=now,
                batteries=batteries,
                ev=ev,
                grid=grid,
                consumers=await self._collect_consumers(),
                current_scenario=(
                    self._engine.current_scenario if self._engine else Scenario.PV_SURPLUS_DAY
                ),
                hour=now.hour,
                minute=now.minute,
            )

        except Exception as exc:
            logger.error("Snapshot collection failed: %s", exc, exc_info=True)
            return None

    async def _execute_plan(self, snapshot: SystemSnapshot) -> None:
        """Execute active night/evening plan actions."""
        if self._ha_api is None:
            return

        hour = snapshot.hour
        plan = self._plan_executor.active_night_plan

        if plan and snapshot.is_night:
            # EV charging per plan
            ev_cfg = self._config.ev_charger
            if (
                plan.ev_start_hour <= hour < plan.ev_stop_hour
                and not plan.ev_skip
                and snapshot.ev.connected
                and snapshot.ev.soc_pct < MAX_SOC_PCT
            ):
                # Start EV if not already charging
                if not snapshot.ev.charging:
                    domain = self._entity_domain(ev_cfg.entities.enabled)
                    await self._ha_api.call_service(
                        domain,
                        "turn_on",
                        {"entity_id": ev_cfg.entities.enabled},
                    )
                    logger.info(
                        "PLAN EXEC: EV start (plan h%d-%d)",
                        plan.ev_start_hour,
                        plan.ev_stop_hour,
                    )

            # Grid charge bat per plan
            if (
                plan.bat_charge_start_hour <= hour < plan.bat_charge_stop_hour
                and plan.bat_charge_need_kwh > 0
                and snapshot.total_battery_soc_pct < self._config.night_plan.grid_charge_max_soc_pct
            ):
                # Set charge mode with grid charge rate
                rate_w = int(plan.bat_charge_rate_kw * _W_TO_KW)
                if rate_w > 0 and self._engine:
                    for bat in snapshot.batteries:
                        if not self._engine._mode_manager.is_in_progress(bat.battery_id):
                            self._engine._mode_manager.request_change(
                                battery_id=bat.battery_id,
                                target_mode="charge_pv",
                                reason=f"Plan: grid charge {rate_w}W",
                            )
                    logger.info(
                        "PLAN EXEC: grid charge bat %dW (plan h%d-%d)",
                        rate_w,
                        plan.bat_charge_start_hour,
                        plan.bat_charge_stop_hour,
                    )

        # Clear night plan at morning
        if plan and not snapshot.is_night and hour >= 6:
            self._plan_executor.active_night_plan = None
            logger.info("PLAN: night plan cleared (morning)")

    async def _evaluate_ev(self, snapshot: SystemSnapshot) -> None:
        """Evaluate EV charging — proactive connect trigger + ramp."""
        if self._ev_controller is None or self._ha_api is None:
            return

        ev = snapshot.ev
        headroom_w = (
            snapshot.grid.dynamic_tak_kw * _W_TO_KW - snapshot.grid.weighted_avg_kw * _W_TO_KW
        )

        # PV surplus = negative grid = export
        pv_surplus_w = -snapshot.grid.grid_power_w

        result = self._ev_controller.evaluate(
            ev_connected=ev.connected,
            ev_soc_pct=ev.soc_pct,
            charging=ev.charging,
            current_amps=ev.current_a,
            grid_import_w=snapshot.grid.grid_power_w,
            ellevio_headroom_w=headroom_w,
            reason_for_no_current=ev.reason_for_no_current,
            is_night=snapshot.is_night,
            pv_surplus_w=pv_surplus_w,
        )

        if result.action == EVAction.NO_CHANGE:
            return

        if result.action == EVAction.CONNECT_TRIGGER:
            logger.info("EV CONNECT: %s", result.reason)
            # Bump low-priority consumers to make room
            ev_cfg_ctrl = (
                self._ev_controller._config if self._ev_controller else EVControllerConfig()
            )
            min_needed_w = float(ev_cfg_ctrl.start_amps * ev_cfg_ctrl.voltage_v)
            if headroom_w < min_needed_w:
                freed_w = 0.0
                for cc in sorted(
                    self._consumer_configs,
                    key=lambda c: c.priority,
                ):
                    if freed_w >= (min_needed_w - headroom_w):
                        break
                    # Check if consumer is active
                    batch = await self._ha_api.get_states_batch(
                        [cc.entity_switch],
                    )
                    state = batch.get(cc.entity_switch, {})
                    if state.get("state") == "on":
                        domain = self._entity_domain(cc.entity_switch)
                        await self._ha_api.call_service(
                            domain,
                            "turn_off",
                            {"entity_id": cc.entity_switch},
                        )
                        freed_w += cc.power_w
                        logger.info(
                            "EV BUMP: stopped %s (+%dW)",
                            cc.name,
                            cc.power_w,
                        )
            # Start EV charging
            ev_cfg = self._config.ev_charger
            await self._ha_api.call_service(
                self._entity_domain(ev_cfg.entities.enabled),
                "turn_on",
                {"entity_id": ev_cfg.entities.enabled},
            )
            logger.info("EV CONNECT: started charging at %dA", result.target_amps)

        elif result.action == EVAction.START:
            ev_cfg = self._config.ev_charger
            await self._ha_api.call_service(
                self._entity_domain(ev_cfg.entities.enabled),
                "turn_on",
                {"entity_id": ev_cfg.entities.enabled},
            )

        elif result.action == EVAction.STOP:
            ev_cfg = self._config.ev_charger
            await self._ha_api.call_service(
                self._entity_domain(ev_cfg.entities.enabled),
                "turn_off",
                {"entity_id": ev_cfg.entities.enabled},
            )

        elif result.action == EVAction.EMERGENCY_CUT:
            ev_cfg = self._config.ev_charger
            await self._ha_api.call_service(
                self._entity_domain(ev_cfg.entities.enabled),
                "turn_off",
                {"entity_id": ev_cfg.entities.enabled},
            )
            logger.warning("EV EMERGENCY CUT: %s", result.reason)

    async def _check_force_replan(self) -> bool:
        """Check HA input_boolean for force replan request.

        If the entity is 'on', turn it off and return True.
        """
        if self._ha_api is None:
            return False

        entity = self._config.manual_override.force_replan_entity
        if not entity:
            return False

        try:
            state = await self._ha_api.get_state(entity)
            if state == "on":
                await self._ha_api.call_service(
                    self._entity_domain(entity),
                    "turn_off",
                    {"entity_id": entity},
                )
                logger.info("Force replan triggered via %s", entity)
                return True
        except Exception as exc:
            logger.error("Force replan check failed: %s", exc)

        return False

    async def _apply_manual_override(self) -> None:
        """Read manual override helpers from HA and apply to state machine."""
        if self._ha_api is None or self._engine is None:
            return

        override_cfg = self._config.manual_override
        if not override_cfg.enabled_entity:
            return

        try:
            enabled = await self._ha_api.get_state(
                override_cfg.enabled_entity,
            )
            if enabled != "on":
                self._engine.set_manual_override(None)
                return

            scenario_str = await self._ha_api.get_state(
                override_cfg.scenario_entity,
            )
            if scenario_str is None or scenario_str in ("", "Auto", "unknown", "unavailable"):
                self._engine.set_manual_override(None)
                return

            try:
                scenario = Scenario(scenario_str)
                self._engine.set_manual_override(scenario)
                logger.info("Manual override active: %s", scenario.value)
            except ValueError:
                logger.warning(
                    "Invalid manual scenario: '%s'",
                    scenario_str,
                )
                self._engine.set_manual_override(None)
        except Exception as exc:
            logger.error("Manual override read failed: %s", exc)

    async def _collect_consumers(self) -> list[ConsumerState]:
        """Read consumer states from HA via batch fetch (V5: 1 HTTP call)."""
        if not self._ha_api or not self._consumer_configs:
            return []

        # Collect all entity IDs for one batch fetch
        entity_ids: list[str] = []
        for cc in self._consumer_configs:
            if cc.entity_switch:
                entity_ids.append(cc.entity_switch)
            if cc.entity_power:
                entity_ids.append(cc.entity_power)

        batch = await self._ha_api.get_states_batch(entity_ids)

        consumers: list[ConsumerState] = []
        # PLAT-1700: consumers without an entity_switch (e.g. goldshell_miner,
        # read_only adapters) derive `active` from measured power instead.
        _ACTIVE_POWER_THRESHOLD_W: float = 10.0
        for cc in self._consumer_configs:
            active = False
            power = 0.0
            if cc.entity_power:
                power_state = batch.get(cc.entity_power, {})
                power_str = power_state.get("state")
                if power_str is None or power_str in ("", "unavailable", "unknown"):
                    power = 0.0
                else:
                    try:
                        power = float(power_str)
                    except (ValueError, TypeError):
                        power = 0.0
            if cc.entity_switch:
                switch_state = batch.get(cc.entity_switch, {})
                active = switch_state.get("state") == "on"
            else:
                # No switch — fall back to measured power.
                active = power > _ACTIVE_POWER_THRESHOLD_W

            consumers.append(
                ConsumerState(
                    consumer_id=cc.id,
                    name=cc.name,
                    active=active,
                    power_w=power if active else float(cc.power_w),
                    priority=cc.priority,
                    priority_shed=cc.priority_shed,
                    load_type=cc.type,
                    requires_active=cc.requires_active,
                )
            )

        # Sort by priority (lower = higher priority)
        consumers.sort(key=lambda c: c.priority)
        return consumers

    async def _execute_surplus(self, snapshot: SystemSnapshot) -> None:
        """Run surplus dispatch and execute start/stop commands."""
        if self._surplus_dispatch is None or self._ha_api is None:
            return

        # PLAT-1541: Cold outdoor battery → start cold_heater consumer
        outdoor_bat = next(
            (
                b
                for b in snapshot.batteries
                if any(bc.id == b.battery_id and bc.is_outdoor for bc in self._config.batteries)
            ),
            None,
        )
        heater_cfg = next(
            (c for c in self._consumer_configs if c.role == "cold_heater"),
            None,
        )
        if outdoor_bat and heater_cfg and heater_cfg.entity_switch:
            bat_cfg_outdoor = next(
                (bc for bc in self._config.batteries if bc.id == outdoor_bat.battery_id),
                None,
            )
            cold_threshold = (
                bat_cfg_outdoor.cold_temp_c
                if bat_cfg_outdoor
                else self._config.guards.g1_soc_floor.cold_floor_pct
            )
            if outdoor_bat.cell_temp_c < cold_threshold:
                # Cold outdoor battery — start heater consumer
                batch = await self._ha_api.get_states_batch(
                    [heater_cfg.entity_switch],
                )
                heater_state = batch.get(heater_cfg.entity_switch, {})
                if heater_state.get("state") != "on":
                    domain = self._entity_domain(heater_cfg.entity_switch)
                    await self._ha_api.call_service(
                        domain,
                        "turn_on",
                        {"entity_id": heater_cfg.entity_switch},
                    )
                    logger.info(
                        "COLD HEATER: outdoor battery (%.1f°C < %.1f°C) → started",
                        outdoor_bat.cell_temp_c,
                        cold_threshold,
                    )

        # Calculate available surplus: negative grid = export
        surplus_w = -snapshot.grid.grid_power_w
        # Add power from currently active consumers (they're part of the surplus)
        for c in snapshot.consumers:
            if c.active:
                surplus_w += c.power_w

        active_deps = {c.consumer_id for c in snapshot.consumers if c.active}

        result = self._surplus_dispatch.evaluate(
            available_surplus_w=surplus_w,
            consumers=snapshot.consumers,
            active_dependencies=active_deps,
        )

        # Execute allocations
        for alloc in result.allocations:
            if alloc.action == "no_change":
                continue

            # Find consumer config for switch entity
            cc = next(
                (c for c in self._consumer_configs if c.id == alloc.consumer_id),
                None,
            )
            if cc is None or not cc.entity_switch:
                continue

            if alloc.action == "start":
                domain = self._entity_domain(cc.entity_switch)
                await self._ha_api.call_service(
                    domain,
                    "turn_on",
                    {"entity_id": cc.entity_switch},
                )
                logger.info(
                    "Surplus: START %s (%s)",
                    cc.name,
                    alloc.reason,
                )
            elif alloc.action == "stop":
                domain = self._entity_domain(cc.entity_switch)
                await self._ha_api.call_service(
                    domain,
                    "turn_off",
                    {"entity_id": cc.entity_switch},
                )
                logger.info(
                    "Surplus: STOP %s (%s)",
                    cc.name,
                    alloc.reason,
                )

    async def _write_dashboard_state(
        self,
        snapshot: SystemSnapshot,
        cycle_result: CycleResult,
    ) -> None:
        """Write scenario, rules and decision info to HA for dashboard display."""
        if self._ha_api is None:
            return

        dash = self._config.dashboard

        # Scenario sensor with battery/grid attributes
        bat_socs = {b.battery_id: round(b.soc_pct, 1) for b in snapshot.batteries}
        attrs: dict[str, object] = {
            "friendly_name": "CARMA Box Scenario",
            "cycle": self._cycle_count,
            "hour": snapshot.hour,
            "battery_soc": bat_socs,
            "grid_power_w": round(snapshot.grid.grid_power_w),
            "pv_total_w": round(snapshot.grid.pv_total_w),
            "weighted_avg_kw": round(snapshot.grid.weighted_avg_kw, 2),
        }
        await self._ha_api.set_state(
            dash.entity_scenario,
            cycle_result.scenario.value,
            attrs,
        )

        # Decision reason — guard level + scenario
        guard_level = "OK"
        if cycle_result.guard:
            guard_level = cycle_result.guard.level.value
        reason = f"{guard_level} | {cycle_result.scenario.value}"
        await self._ha_api.set_state(
            dash.entity_decision_reason,
            reason,
            {"friendly_name": "CARMA Box Decision"},
        )

        # Rules sensor — active guards summary
        rules = "OK"
        if cycle_result.guard and cycle_result.guard.commands:
            rules = ", ".join(c.command_type.value for c in cycle_result.guard.commands)
        await self._ha_api.set_state(
            dash.entity_rules,
            rules,
            {"friendly_name": "CARMA Box Active Rules"},
        )

        # Plan text fields — write PV forecast summary
        plan_today = (
            f"PV {snapshot.grid.pv_forecast_today_kwh:.0f}kWh "
            f"Price {snapshot.grid.price_ore:.0f}ore "
            f"Scenario {cycle_result.scenario.value}"
        )
        plan_tomorrow = f"PV tomorrow {snapshot.grid.pv_forecast_tomorrow_kwh:.0f}kWh"
        await self._ha_api.set_input_text(
            dash.entity_plan_today,
            plan_today,
        )
        await self._ha_api.set_input_text(
            dash.entity_plan_tomorrow,
            plan_tomorrow,
        )
        await self._ha_api.set_input_text(
            dash.entity_plan_day3,
            "",
        )

        # Export limit — open during PV, close at evening
        # Uses surplus PV min threshold + night start hour from config
        pv_min_w = self._config.surplus.start_threshold_kw * _W_TO_KW
        evening_hour = self._config.grid.ellevio.night_start_hour
        pv_producing = snapshot.grid.pv_total_w > pv_min_w
        for bat_cfg in self._config.batteries:
            export_entity = bat_cfg.entities.export_limit
            if not export_entity:
                continue
            if pv_producing and snapshot.hour < evening_hour:
                await self._ha_api.call_service(
                    self._entity_domain(export_entity),
                    "set_value",
                    {"entity_id": export_entity, "value": int(bat_cfg.max_discharge_kw * _W_TO_KW)},
                )
            elif snapshot.hour >= evening_hour or not pv_producing:
                await self._ha_api.call_service(
                    self._entity_domain(export_entity),
                    "set_value",
                    {"entity_id": export_entity, "value": 0},
                )

        # PV forecast flags — pv_high_today/tomorrow
        pv_high_threshold = 20.0  # kWh
        pv_today = snapshot.grid.pv_forecast_today_kwh
        pv_tomorrow = snapshot.grid.pv_forecast_tomorrow_kwh
        pv_high_today_entity = self._config.pv_forecast.entity_pv_high_today
        pv_high_tomorrow_entity = self._config.pv_forecast.entity_pv_high_tomorrow
        if pv_high_today_entity:
            await self._ha_api.call_service(
                self._entity_domain(pv_high_today_entity),
                "turn_on" if pv_today >= pv_high_threshold else "turn_off",
                {"entity_id": pv_high_today_entity},
            )
        if pv_high_tomorrow_entity:
            await self._ha_api.call_service(
                self._entity_domain(pv_high_tomorrow_entity),
                "turn_on" if pv_tomorrow >= pv_high_threshold else "turn_off",
                {"entity_id": pv_high_tomorrow_entity},
            )

        # DayPlan sensor — write current day plan (PLAT-1627)
        # LÄRDOM [dead-code-guard]: always init in __init__, never use hasattr()
        if self._current_day_plan is not None:
            plan = self._current_day_plan
            current_slot = plan.get_slot(snapshot.hour)
            plan_state = "active" if current_slot else "no_slot"
            plan_attrs: dict[str, object] = {
                "friendly_name": "CARMA Box Day Plan",
                "slots": plan.to_dict()["slots"],
                "can_discharge_fm": plan.can_discharge_fm,
                "total_expected_export_kwh": round(
                    plan.total_expected_export_kwh,
                    2,
                ),
                "bat_target_soc_pct": plan.bat_target_soc_pct,
                "ev_target_soc_pct": plan.ev_target_soc_pct,
                "created_at": plan.to_dict()["created_at"],
            }
            if current_slot:
                plan_attrs["current_hour"] = current_slot.to_dict()
            await self._ha_api.set_state(
                "sensor.carma_box_day_plan",
                plan_state,
                plan_attrs,
            )

        # Ellevio sensor — write peak tracking data
        if self._ellevio:
            ellevio_attrs: dict[str, object] = {
                "friendly_name": "Ellevio Peak Tracker",
                "top_peaks": self._ellevio.state.top_peaks,
                "top_n_avg_kw": round(self._ellevio.state.top_n_avg, 2),
                "hit_rate_pct": round(self._ellevio.state.hit_rate_pct, 1),
                "hours_total": self._ellevio.state.hours_total,
                "last_hourly_kw": round(self._ellevio.state.last_hourly_kw, 2),
                "monthly_cost_kr": round(self._ellevio.monthly_cost_kr, 0),
            }
            await self._ha_api.set_state(
                "sensor.carma_box_ellevio",
                f"{self._ellevio.current_weighted_avg_kw:.2f}",
                ellevio_attrs,
            )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Parsed namespace with config path.
    """
    parser = argparse.ArgumentParser(
        prog="carma-box",
        description="CARMA Box — Smart Energy Optimization Service v" + __version__,
    )
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to site.yaml configuration file",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"carma-box {__version__}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Load config and exit (validation only)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Application entry point.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 = success).
    """
    args = parse_args(argv)

    # Load and validate configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: Configuration invalid: {exc}", file=sys.stderr)
        return 1

    # Apply HA addon overrides (no-op in standalone mode)
    _apply_addon_overrides(config)

    # Setup logging
    setup_logging(config)
    logger.info("CARMA Box v%s starting — site: %s", __version__, config.site.name)

    if args.dry_run:
        logger.info("Dry run — config valid, exiting")
        return 0

    # Create HA API client and service
    ha_api = HAApiClient(config.homeassistant)  # pragma: no cover
    service = CarmaBoxService(config, ha_api=ha_api)  # pragma: no cover

    # Setup signal handlers for graceful shutdown
    loop = asyncio.new_event_loop()  # pragma: no cover

    def _signal_handler(sig: int) -> None:  # pragma: no cover
        sig_name = signal.Signals(sig).name
        logger.info("Received %s, shutting down...", sig_name)
        loop.create_task(service.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):  # pragma: no cover
        loop.add_signal_handler(sig, _signal_handler, sig)

    try:  # pragma: no cover
        loop.run_until_complete(service.start())
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("KeyboardInterrupt received")
    finally:  # pragma: no cover
        loop.run_until_complete(service.shutdown())
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        logger.info("CARMA Box stopped")

    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
