"""Health Endpoint + Prometheus Metrics for CARMA Box.

HTTP health endpoint on port 8412:
- GET /health → JSON status
- GET /metrics → Prometheus text format

Status: ok/degraded/error based on guard status and HA connection.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    """Current health status for the endpoint."""

    status: str = "ok"
    scenario: str = "PV_SURPLUS_DAY"
    uptime_s: float = 0.0
    cycle_count: int = 0
    last_cycle_s: float = 0.0
    guard_level: str = "ok"
    ha_connected: bool = True
    version: str = "2.0.0"

    def to_json(self) -> str:
        return json.dumps({
            "status": self.status,
            "scenario": self.scenario,
            "uptime_s": round(self.uptime_s, 1),
            "cycle_count": self.cycle_count,
            "last_cycle_s": round(self.last_cycle_s, 3),
            "guard_level": self.guard_level,
            "ha_connected": self.ha_connected,
            "version": self.version,
        })


@dataclass
class Metrics:
    """Simple metrics tracking (Prometheus-compatible)."""

    cycles_total: int = 0
    guard_triggers_total: int = 0
    commands_total: int = 0
    commands_failed_total: int = 0
    scenario_transitions_total: int = 0
    grid_import_kw: float = 0.0
    battery_soc_pct: float = 0.0
    ev_power_kw: float = 0.0

    def to_prometheus(self) -> str:
        """Format as Prometheus text exposition."""
        lines = [
            "# HELP carma_cycles_total Total control cycles",
            "# TYPE carma_cycles_total counter",
            f"carma_cycles_total {self.cycles_total}",
            "# HELP carma_guard_triggers_total Total guard triggers",
            "# TYPE carma_guard_triggers_total counter",
            f"carma_guard_triggers_total {self.guard_triggers_total}",
            "# HELP carma_commands_total Total commands sent",
            "# TYPE carma_commands_total counter",
            f"carma_commands_total {self.commands_total}",
            "# HELP carma_commands_failed_total Failed commands",
            "# TYPE carma_commands_failed_total counter",
            f"carma_commands_failed_total {self.commands_failed_total}",
            "# HELP carma_grid_import_kw Current grid import",
            "# TYPE carma_grid_import_kw gauge",
            f"carma_grid_import_kw {self.grid_import_kw:.3f}",
            "# HELP carma_battery_soc_pct Battery SoC",
            "# TYPE carma_battery_soc_pct gauge",
            f"carma_battery_soc_pct {self.battery_soc_pct:.1f}",
        ]
        return "\n".join(lines) + "\n"

    def increment_cycle(self) -> None:
        self.cycles_total += 1

    def increment_guard_trigger(self) -> None:
        self.guard_triggers_total += 1

    def record_commands(self, total: int, failed: int) -> None:
        self.commands_total += total
        self.commands_failed_total += failed
