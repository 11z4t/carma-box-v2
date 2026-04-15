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
import logging
import signal
import sys
import zoneinfo

import aiohttp.web
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from adapters.goodwe import GoodWeAdapter
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
from core.planner import Planner, PlannerConfig
from core.ev_controller import EVAction, EVController, EVControllerConfig
from core.surplus_dispatch import SurplusConfig as SurplusDispatchConfig, SurplusDispatch
from health import HealthStatus, Metrics
from core.state_machine import StateMachine, StateMachineConfig
from notifications.slack import SlackNotifier

__version__ = "2.0.0"

logger = logging.getLogger("carma_box")

# Conversion constants — no naked numeric literals in business logic.
_W_TO_KW: float = 1000.0
_MS_PER_S: int = 1000


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

        # HA API client (None in dry-run/test mode)
        self._ha_api = ha_api
        self._last_plan_hour: int = -1
        self._last_pv_tomorrow: float = -1.0

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
        sm = StateMachine(StateMachineConfig(
            start_scenario=Scenario[config.control.start_scenario],
            min_dwell_s=config.control.scenario_transition_s,
        ))
        balancer = BatteryBalancer(BalancerConfig(
            normal_floor_pct=guard_cfg.normal_floor_pct,
            cold_floor_pct=guard_cfg.cold_floor_pct,
            freeze_floor_pct=guard_cfg.freeze_floor_pct,
        ))
        mode_mgr = ModeChangeManager(ModeChangeConfig(
            clear_wait_s=float(config.control.mode_change_clear_wait_s),
            standby_wait_s=config.control.standby_intermediate_s,
            set_wait_s=float(config.control.mode_change_set_wait_s),
            verify_wait_s=float(config.control.mode_change_verify_wait_s),
        ))
        executor = CommandExecutor(
            inverters=dict(inverters),
            mode_manager=mode_mgr,
            config=ExecutorConfig(
                mode_change_cooldown_s=config.control.mode_change_cooldown_s,
            ),
            ha_api=ha_api,
        )

        # Slack notifier
        self._slack = SlackNotifier()
        self._last_scenario: Optional[str] = None

        # Local DB
        db_path = config.storage.sqlite.path
        self._db = LocalDB(db_path)

        # Planner
        self._planner = Planner(PlannerConfig(
            ev_target_soc_pct=config.ev.daily_target_soc_pct,
            pv_high_threshold_kwh=float(
                config.night_plan.house_baseload_kw
                * config.night_plan.night_hours
            ),
            grid_charge_max_soc_pct=config.night_plan.grid_charge_max_soc_pct,
            grid_charge_price_threshold_ore=config.night_plan.grid_charge_price_threshold_ore,
        ))
        self._plan_executor = PlanExecutor(
            planner=self._planner,
            ha_api=ha_api,
            config=config,
            guard_policy=guard_policy,
        )

        # Ellevio peak tracker
        self._ellevio = EllevioTracker(EllevioConfig(
            tak_kw=config.grid.ellevio.tak_kw,
            night_weight=config.grid.ellevio.night_weight,
            day_weight=config.grid.ellevio.day_weight,
            night_start_h=config.grid.ellevio.night_start_hour,
            night_end_h=config.grid.ellevio.night_end_hour,
        ))

        # EV controller
        self._ev_controller = EVController(EVControllerConfig(
            target_soc_pct=config.ev.daily_target_soc_pct,
        ))

        # Surplus dispatch from consumer configs
        surplus_cfg = config.surplus
        self._surplus_dispatch = SurplusDispatch(SurplusDispatchConfig(
            stop_threshold_w=surplus_cfg.stop_threshold_kw * _W_TO_KW,
            start_delay_s=surplus_cfg.start_delay_s,
            max_switches_per_window=surplus_cfg.max_switches_per_window,
            switch_window_s=surplus_cfg.switch_window_min * 60,
            bump_delay_s=surplus_cfg.bump_delay_s,
            deadband_w=float(config.control.deadband.normal_w),
            doubled_deadband_w=float(config.control.deadband.doubled_w),
            doubled_deadband_s=float(config.control.deadband.doubled_duration_s),
        ))
        self._consumer_configs = config.consumers

        # H2: map battery_id → config so engine can read per-battery limits
        battery_cfg_map = {bc.id: bc for bc in config.batteries}

        self._engine = ControlEngine(
            grid_guard, sm, balancer, mode_mgr, executor,
            battery_configs=battery_cfg_map,
        )

    @property
    def config(self) -> CarmaConfig:
        """Return the loaded configuration."""
        return self._config

    @property
    def is_running(self) -> bool:
        """Whether the main loop is active."""
        return self._running

    async def start(self) -> None:
        """Start the main control loop.

        Runs until stop() is called or a signal is received.
        """
        self._running = True
        cycle_s = self._config.control.cycle_interval_s
        health_port = self._config.health.port
        logger.info(
            "Starting main loop (cycle=%ds, health=:%d)",
            cycle_s, health_port,
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
            logger.info(
                "Main loop stopped after %d cycles", self._cycle_count
            )

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
        """Execute one 30-second control cycle.

        Pipeline: COLLECT → GUARD → SCENARIO → BALANCE → EXECUTE → PERSIST
        """
        self._cycle_count += 1
        self._last_cycle = datetime.now(tz=timezone.utc)

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
        plan_hours = set(
            self._planner._config.plan_hours
        ) if self._planner else set()
        # Check for PV forecast change (>20% delta → re-plan)
        pv_changed = False
        pv_tomorrow = snapshot.grid.pv_forecast_tomorrow_kwh
        if self._last_pv_tomorrow > 0:
            delta_pct = abs(pv_tomorrow - self._last_pv_tomorrow) / self._last_pv_tomorrow
            replan_threshold = (
                self._planner._config.pv_replan_threshold
                if self._planner else 0.2
            )
            if delta_pct > replan_threshold:
                pv_changed = True
                logger.info(
                    "PV forecast changed %.0f → %.0f kWh (%.0f%%) — re-planning",
                    self._last_pv_tomorrow, pv_tomorrow, delta_pct * 100,
                )
        self._last_pv_tomorrow = pv_tomorrow

        # Startup replan: first cycle has no plan → generate immediately
        startup_replan = self._last_plan_hour == -1

        # Force replan via HA input_boolean
        force_replan = await self._check_force_replan()

        scheduled = (
            snapshot.hour in plan_hours
            and snapshot.hour != self._last_plan_hour
        )
        if self._planner and (scheduled or pv_changed or startup_replan or force_replan):
            reason = (
                "startup" if startup_replan
                else "force_replan" if force_replan
                else "pv_changed" if pv_changed
                else f"scheduled (hour={snapshot.hour})"
            )
            logger.info("Plan generation triggered: %s", reason)
            self._last_plan_hour = snapshot.hour
            await self._plan_executor.generate(snapshot)

        # Phase 1.5: ELLEVIO TRACKING — update weighted hourly average
        if self._ellevio:
            grid_kw = snapshot.grid.grid_power_w / _W_TO_KW
            self._ellevio.update(grid_kw, snapshot.timestamp)

        # Phase 1.6: MANUAL OVERRIDE — read HA helpers, set on state machine
        await self._apply_manual_override()

        # Phases 2-6: delegated to ControlEngine
        data_age_s = (
            datetime.now(tz=timezone.utc) - snapshot.timestamp
        ).total_seconds()
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
                cycle_result.cycle_id, self._cycle_count, cycle_result.error,
            )

        # Update health + metrics
        self._health.scenario = cycle_result.scenario.value
        self._health.cycle_count = self._cycle_count
        self._health.last_cycle_s = cycle_result.elapsed_s
        self._health.ha_connected = ha_connected
        self._health.guard_level = (
            cycle_result.guard.level.value if cycle_result.guard else "ok"
        )
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
                        await self._db.write_event(EventLogEntry(
                            timestamp=snapshot.timestamp.isoformat(),
                            event_type="soc_snapshot",
                            source=bat.battery_id,
                            message=f"SoC={bat.soc_pct:.1f}%",
                            data=f"hour={snapshot.hour}",
                        ))
                    logger.info("SoC snapshot: %s", {
                        b.battery_id: f"{b.soc_pct:.0f}%"
                        for b in snapshot.batteries
                    })
                except Exception as exc:
                    logger.debug("SoC snapshot: %s", exc)

        # Phase 10: PERSIST — write cycle to SQLite
        if self._db:
            try:
                guard_level = (
                    cycle_result.guard.level.value
                    if cycle_result.guard else "ok"
                )
                headroom = (
                    cycle_result.guard.headroom_kw
                    if cycle_result.guard else 0.0
                )
                violations = (
                    "; ".join(cycle_result.guard.violations)
                    if cycle_result.guard else ""
                )
                await self._db.write_cycle(CycleLogEntry(
                    cycle_id=cycle_result.cycle_id,
                    timestamp=snapshot.timestamp.isoformat(),
                    scenario=cycle_result.scenario.value,
                    guard_level=guard_level,
                    headroom_kw=headroom,
                    elapsed_s=cycle_result.elapsed_s,
                    violations=violations,
                ))
            except Exception as exc:
                logger.debug("Cycle log write failed: %s", exc)

    def _on_health_done(self, task: asyncio.Task[None]) -> None:
        """Log health server task completion/failure."""
        self._health_task = None
        if task.cancelled():
            return
        if task.exception():
            logger.error("Health server failed: %s", task.exception())

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
        self, request: aiohttp.web.Request,
    ) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            text=self._health.to_json(),
            content_type="application/json",
        )

    async def _handle_metrics(
        self, request: aiohttp.web.Request,
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

            # Read battery states
            batteries: list[BatteryState] = []
            for bat_cfg in cfg.batteries:
                ents = bat_cfg.entities
                batch = await self._ha_api.get_states_batch([
                    ents.soc, ents.power, ents.cell_temp,
                    ents.pv_power, ents.grid_power, ents.load_power,
                    ents.ems_mode, ents.ems_power_limit,
                    ents.fast_charging, ents.soh,
                ])

                def _float(eid: str, default: float = 0.0) -> float:
                    s = batch.get(eid, {}).get("state")
                    if s in (None, "unavailable", "unknown"):
                        return default
                    try:
                        return float(s)
                    except (ValueError, TypeError):
                        return default

                soc = _float(ents.soc)

                # PLAT-1539: Detect GoodWe bridge offline
                soc_state = batch.get(ents.soc, {}).get("state")
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
                avail = max(0.0, (soc - floor) / 100.0 * bat_cfg.cap_kwh * bat_cfg.efficiency)

                batteries.append(BatteryState(
                    battery_id=bat_cfg.id,
                    soc_pct=soc,
                    power_w=_float(ents.power),
                    cell_temp_c=_float(ents.cell_temp, bat_cfg.default_cell_temp_c),
                    pv_power_w=max(0.0, _float(ents.pv_power)),
                    grid_power_w=_float(ents.grid_power),
                    load_power_w=max(0.0, _float(ents.load_power)),
                    ems_mode=EMSMode(batch.get(ents.ems_mode, {}).get("state", "battery_standby")),
                    ems_power_limit_w=int(_float(ents.ems_power_limit)),
                    fast_charging=batch.get(ents.fast_charging, {}).get("state") == "on",
                    soh_pct=_float(ents.soh, bat_cfg.default_soh_pct),
                    cap_kwh=bat_cfg.cap_kwh,
                    ct_placement=bat_cfg.ct_placement,
                    available_kwh=avail,
                ))

            # Read EV state
            ev_ents = cfg.ev_charger.entities
            ev_batch = await self._ha_api.get_states_batch([
                ev_ents.status, ev_ents.power, ev_ents.current,
                ev_ents.enabled, ev_ents.reason_for_no_current,
            ])
            ev_soc_str = await self._ha_api.get_state(cfg.ev.entities.soc)
            ev_soc = float(ev_soc_str) if ev_soc_str else -1.0
            ev_status = ev_batch.get(ev_ents.status, {}).get("state", "disconnected")
            ev_enabled = ev_batch.get(ev_ents.enabled, {}).get("state") == "on"
            # Easee reports "disconnected" when disabled even if cable plugged in
            # If disabled → assume connected (cable always plugged in at home)
            ev_connected = (
                ev_status.lower() in (
                    "awaiting_start", "charging", "completed", "ready_to_charge",
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
                reason_for_no_current=ev_batch.get(
                    ev_ents.reason_for_no_current, {}
                ).get("state", ""),
                target_soc_pct=cfg.ev.daily_target_soc_pct,
            )

            # Read grid state
            grid_ents = cfg.grid.ellevio
            grid_batch = await self._ha_api.get_states_batch([
                grid_ents.entity_weighted_avg,
                grid_ents.entity_current_peak,
                grid_ents.entity_dynamic_tak,
                cfg.pricing.entity,
                cfg.pv_forecast.entity_today,
                cfg.pv_forecast.entity_tomorrow,
            ])

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
                dynamic_tak_kw=_grid_float(grid_ents.entity_dynamic_tak, 3.0),
                pv_total_w=pv_total,
                price_ore=_grid_float(cfg.pricing.entity, 100.0) * 100,
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
                    self._engine.current_scenario
                    if self._engine else Scenario.MIDDAY_CHARGE
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
                        domain, "turn_on",
                        {"entity_id": ev_cfg.entities.enabled},
                    )
                    logger.info(
                        "PLAN EXEC: EV start (plan h%d-%d)",
                        plan.ev_start_hour, plan.ev_stop_hour,
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
                        rate_w, plan.bat_charge_start_hour, plan.bat_charge_stop_hour,
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
            snapshot.grid.dynamic_tak_kw * _W_TO_KW
            - snapshot.grid.weighted_avg_kw * _W_TO_KW
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
                self._ev_controller._config
                if self._ev_controller else EVControllerConfig()
            )
            min_needed_w = float(
                ev_cfg_ctrl.start_amps * ev_cfg_ctrl.voltage_v
            )
            if headroom_w < min_needed_w:
                freed_w = 0.0
                for cc in sorted(
                    self._consumer_configs, key=lambda c: c.priority,
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
                            domain, "turn_off",
                            {"entity_id": cc.entity_switch},
                        )
                        freed_w += cc.power_w
                        logger.info(
                            "EV BUMP: stopped %s (+%dW)",
                            cc.name, cc.power_w,
                        )
            # Start EV charging
            ev_cfg = self._config.ev_charger
            await self._ha_api.call_service(
                self._entity_domain(ev_cfg.entities.enabled), "turn_on",
                {"entity_id": ev_cfg.entities.enabled},
            )
            logger.info("EV CONNECT: started charging at %dA", result.target_amps)

        elif result.action == EVAction.START:
            ev_cfg = self._config.ev_charger
            await self._ha_api.call_service(
                self._entity_domain(ev_cfg.entities.enabled), "turn_on",
                {"entity_id": ev_cfg.entities.enabled},
            )

        elif result.action == EVAction.STOP:
            ev_cfg = self._config.ev_charger
            await self._ha_api.call_service(
                self._entity_domain(ev_cfg.entities.enabled), "turn_off",
                {"entity_id": ev_cfg.entities.enabled},
            )

        elif result.action == EVAction.EMERGENCY_CUT:
            ev_cfg = self._config.ev_charger
            await self._ha_api.call_service(
                self._entity_domain(ev_cfg.entities.enabled), "turn_off",
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
                    self._entity_domain(entity), "turn_off",
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
            if (
                scenario_str is None
                or scenario_str in ("", "Auto", "unknown", "unavailable")
            ):
                self._engine.set_manual_override(None)
                return

            try:
                scenario = Scenario(scenario_str)
                self._engine.set_manual_override(scenario)
                logger.info("Manual override active: %s", scenario.value)
            except ValueError:
                logger.warning(
                    "Invalid manual scenario: '%s'", scenario_str,
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
        for cc in self._consumer_configs:
            active = False
            power = 0.0
            if cc.entity_switch:
                switch_state = batch.get(cc.entity_switch, {})
                active = switch_state.get("state") == "on"
            if cc.entity_power:
                power_state = batch.get(cc.entity_power, {})
                power_str = power_state.get("state")
                if (
                    power_str is None
                    or power_str in ("", "unavailable", "unknown")
                ):
                    power = 0.0
                else:
                    try:
                        power = float(power_str)
                    except (ValueError, TypeError):
                        power = 0.0

            consumers.append(ConsumerState(
                consumer_id=cc.id,
                name=cc.name,
                active=active,
                power_w=power if active else float(cc.power_w),
                priority=cc.priority,
                priority_shed=cc.priority_shed,
                load_type=cc.type,
                requires_active=cc.requires_active,
            ))

        # Sort by priority (lower = higher priority)
        consumers.sort(key=lambda c: c.priority)
        return consumers

    async def _execute_surplus(self, snapshot: SystemSnapshot) -> None:
        """Run surplus dispatch and execute start/stop commands."""
        if self._surplus_dispatch is None or self._ha_api is None:
            return

        # PLAT-1541: Cold outdoor battery → start cold_heater consumer
        outdoor_bat = next(
            (b for b in snapshot.batteries
             if any(bc.id == b.battery_id and bc.is_outdoor
                     for bc in self._config.batteries)),
            None,
        )
        heater_cfg = next(
            (c for c in self._consumer_configs if c.role == "cold_heater"),
            None,
        )
        if outdoor_bat and heater_cfg and heater_cfg.entity_switch:
            bat_cfg_outdoor = next(
                (bc for bc in self._config.batteries
                 if bc.id == outdoor_bat.battery_id),
                None,
            )
            cold_threshold = (
                bat_cfg_outdoor.cold_temp_c if bat_cfg_outdoor
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
                        domain, "turn_on",
                        {"entity_id": heater_cfg.entity_switch},
                    )
                    logger.info(
                        "COLD HEATER: outdoor battery (%.1f°C < %.1f°C) → started",
                        outdoor_bat.cell_temp_c, cold_threshold,
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
                    domain, "turn_on",
                    {"entity_id": cc.entity_switch},
                )
                logger.info(
                    "Surplus: START %s (%s)", cc.name, alloc.reason,
                )
            elif alloc.action == "stop":
                domain = self._entity_domain(cc.entity_switch)
                await self._ha_api.call_service(
                    domain, "turn_off",
                    {"entity_id": cc.entity_switch},
                )
                logger.info(
                    "Surplus: STOP %s (%s)", cc.name, alloc.reason,
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
        bat_socs = {
            b.battery_id: round(b.soc_pct, 1)
            for b in snapshot.batteries
        }
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
            rules = ", ".join(
                c.command_type.value for c in cycle_result.guard.commands
            )
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
        plan_tomorrow = (
            f"PV tomorrow {snapshot.grid.pv_forecast_tomorrow_kwh:.0f}kWh"
        )
        await self._ha_api.set_input_text(
            dash.entity_plan_today, plan_today,
        )
        await self._ha_api.set_input_text(
            dash.entity_plan_tomorrow, plan_tomorrow,
        )
        await self._ha_api.set_input_text(
            dash.entity_plan_day3, "",
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
                    self._entity_domain(export_entity), "set_value",
                    {"entity_id": export_entity,
                     "value": int(bat_cfg.max_discharge_kw * _W_TO_KW)},
                )
            elif snapshot.hour >= evening_hour or not pv_producing:
                await self._ha_api.call_service(
                    self._entity_domain(export_entity), "set_value",
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
