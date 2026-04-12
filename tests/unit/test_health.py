"""Tests for Health Endpoint + Metrics.

Covers:
- HealthStatus JSON format
- Status computation
- Metrics increment/set
- Prometheus text format
"""

from __future__ import annotations

import json

from health import HealthStatus, Metrics


class TestHealthStatus:
    """Health status JSON format."""

    def test_to_json_format(self) -> None:
        status = HealthStatus(status="ok", scenario="MIDDAY_CHARGE", cycle_count=100)
        data = json.loads(status.to_json())
        assert data["status"] == "ok"
        assert data["scenario"] == "MIDDAY_CHARGE"
        assert data["cycle_count"] == 100
        assert "version" in data

    def test_degraded_status(self) -> None:
        status = HealthStatus(status="degraded", guard_level="warning")
        data = json.loads(status.to_json())
        assert data["status"] == "degraded"
        assert data["guard_level"] == "warning"

    def test_error_status(self) -> None:
        status = HealthStatus(status="error", ha_connected=False)
        data = json.loads(status.to_json())
        assert data["status"] == "error"
        assert data["ha_connected"] is False


class TestMetrics:
    """Prometheus metrics."""

    def test_increment_cycle(self) -> None:
        m = Metrics()
        m.increment_cycle()
        m.increment_cycle()
        assert m.cycles_total == 2

    def test_increment_guard(self) -> None:
        m = Metrics()
        m.increment_guard_trigger()
        assert m.guard_triggers_total == 1

    def test_record_commands(self) -> None:
        m = Metrics()
        m.record_commands(total=5, failed=1)
        assert m.commands_total == 5
        assert m.commands_failed_total == 1

    def test_prometheus_format(self) -> None:
        m = Metrics(cycles_total=42, grid_import_kw=1.5, battery_soc_pct=65.0)
        text = m.to_prometheus()
        assert "carma_cycles_total 42" in text
        assert "carma_grid_import_kw 1.500" in text
        assert "carma_battery_soc_pct 65.0" in text
        assert "# TYPE" in text
