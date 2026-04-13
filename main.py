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
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from adapters.goodwe import GoodWeAdapter
from adapters.ha_api import HAApiClient
from config.schema import CarmaConfig, load_config
from core.balancer import BalancerConfig, BatteryBalancer
from core.engine import ControlEngine
from core.executor import CommandExecutor, ExecutorConfig
from core.guards import GridGuard, GuardConfig
from core.mode_change import ModeChangeConfig, ModeChangeManager
from core.models import (
    BatteryState,
    EVState,
    GridState,
    Scenario,
    SystemSnapshot,
)
from core.state_machine import StateMachine, StateMachineConfig

__version__ = "2.0.0"

logger = logging.getLogger("carma_box")


def setup_logging(config: CarmaConfig) -> None:
    """Configure logging from site.yaml settings.

    Args:
        config: Validated site configuration.
    """
    log_cfg = config.logging
    level = getattr(logging, log_cfg.level.upper(), logging.INFO)

    root_logger = logging.getLogger("carma_box")
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

        # HA API client (None in dry-run/test mode)
        self._ha_api = ha_api

        # Create components only when HA API is available
        if ha_api is not None:
            self._setup_components(config, ha_api)
        else:
            self._engine: Optional[ControlEngine] = None

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
            max_changes_per_window=g.g5_oscillation.max_changes_per_window,
            window_s=g.g5_oscillation.window_s,
            doubled_deadband_s=g.g5_oscillation.doubled_deadband_s,
            stale_threshold_s=g.g6_stale_data.threshold_s,
            ha_health_timeout_s=g.g7_communication_lost.ha_health_timeout_s,
        )

        guard = GridGuard(guard_cfg)
        sm = StateMachine(StateMachineConfig(
            min_dwell_s=config.control.scenario_transition_s,
        ))
        balancer = BatteryBalancer(BalancerConfig(
            normal_floor_pct=guard_cfg.normal_floor_pct,
            cold_floor_pct=guard_cfg.cold_floor_pct,
            freeze_floor_pct=guard_cfg.freeze_floor_pct,
        ))
        mode_mgr = ModeChangeManager(ModeChangeConfig(
            clear_wait_s=60.0,
            standby_wait_s=config.control.standby_intermediate_s,
            set_wait_s=60.0,
            verify_wait_s=30.0,
        ))
        executor = CommandExecutor(
            inverters=inverters,  # type: ignore[arg-type]
            mode_manager=mode_mgr,
            config=ExecutorConfig(
                mode_change_cooldown_s=config.control.mode_change_cooldown_s,
            ),
        )

        self._engine = ControlEngine(guard, sm, balancer, mode_mgr, executor)

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
        logger.info("Starting main loop (cycle=%ds)", cycle_s)

        try:
            while self._running:
                await self._run_cycle()
                await asyncio.sleep(cycle_s)
        except asyncio.CancelledError:
            logger.info("Main loop cancelled")
        finally:
            self._running = False
            logger.info(
                "Main loop stopped after %d cycles", self._cycle_count
            )

    async def stop(self) -> None:
        """Signal the main loop to stop gracefully."""
        logger.info("Stop requested")
        self._running = False

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

        # Phases 2-6: delegated to ControlEngine
        cycle_result = await self._engine.run_cycle(
            snapshot=snapshot,
            ha_connected=ha_connected,
        )

        if cycle_result.error:
            logger.error("Cycle %d error: %s", self._cycle_count, cycle_result.error)

        logger.debug(
            "Cycle %d complete in %.3fs (scenario=%s)",
            self._cycle_count, cycle_result.elapsed_s,
            cycle_result.scenario.value,
        )

    async def _collect_snapshot(self, ha_connected: bool) -> Optional[SystemSnapshot]:
        """Collect system state from all adapters."""
        if not ha_connected or self._ha_api is None:
            return None

        try:
            now = datetime.now(tz=timezone.utc)
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
                floor = 15.0  # Effective floor handled by guards
                avail = max(0.0, (soc - floor) / 100.0 * bat_cfg.cap_kwh * bat_cfg.efficiency)

                batteries.append(BatteryState(
                    battery_id=bat_cfg.id,
                    soc_pct=soc,
                    power_w=_float(ents.power),
                    cell_temp_c=_float(ents.cell_temp, 20.0),
                    pv_power_w=max(0.0, _float(ents.pv_power)),
                    grid_power_w=_float(ents.grid_power),
                    load_power_w=max(0.0, _float(ents.load_power)),
                    ems_mode=batch.get(ents.ems_mode, {}).get("state", "battery_standby"),
                    ems_power_limit_w=int(_float(ents.ems_power_limit)),
                    fast_charging=batch.get(ents.fast_charging, {}).get("state") == "on",
                    soh_pct=_float(ents.soh, 100.0),
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
            ev_connected = ev_status.lower() in (
                "awaiting_start", "charging", "completed", "ready_to_charge",
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
                power_w=_ev_float(ev_ents.power) * 1000,  # kW → W
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
                consumers=[],  # Populated when consumer adapters exist
                current_scenario=(
                    self._engine._sm.state.current
                    if self._engine else Scenario.MIDDAY_CHARGE
                ),
                hour=now.hour,
                minute=now.minute,
            )

        except Exception as exc:
            logger.error("Snapshot collection failed: %s", exc, exc_info=True)
            return None


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

    # Create service
    service = CarmaBoxService(config)  # pragma: no cover

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
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        logger.info("CARMA Box stopped")

    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
