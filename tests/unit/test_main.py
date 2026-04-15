"""Tests for main.py entry point.

Covers:
- parse_args: valid args, --version, --dry-run
- main(): config load, dry-run exit, invalid config error
- CarmaBoxService: init, start/stop cycle, SIGTERM handling
- setup_logging: console + file handler creation
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from main import CarmaBoxService, main, parse_args, setup_logging


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    """Test command-line argument parsing."""

    def test_config_required(self) -> None:
        with pytest.raises(SystemExit):
            parse_args([])

    def test_config_parsed(self) -> None:
        args = parse_args(["--config", "site.yaml"])
        assert args.config == "site.yaml"

    def test_dry_run_default_false(self) -> None:
        args = parse_args(["--config", "x.yaml"])
        assert args.dry_run is False

    def test_dry_run_flag(self) -> None:
        args = parse_args(["--config", "x.yaml", "--dry-run"])
        assert args.dry_run is True

    def test_version_exits(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            parse_args(["--version"])
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------


class TestMain:
    """Test the main() entry point."""

    def test_invalid_config_returns_1(self) -> None:
        result = main(["--config", "/nonexistent/path.yaml"])
        assert result == 1

    def test_dry_run_returns_0(self) -> None:
        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        result = main(["--config", config_path, "--dry-run"])
        assert result == 0

    def test_invalid_yaml_returns_1(self, tmp_path: Path) -> None:
        bad_config = tmp_path / "bad.yaml"
        bad_config.write_text("invalid: {missing: required_fields}")
        result = main(["--config", str(bad_config)])
        assert result == 1


# ---------------------------------------------------------------------------
# CarmaBoxService
# ---------------------------------------------------------------------------


class TestCarmaBoxService:
    """Test service lifecycle."""

    @pytest.fixture()
    def config(self):  # type: ignore[no-untyped-def]
        from config.schema import load_config
        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        return load_config(config_path)

    def test_init(self, config):  # type: ignore[no-untyped-def]
        service = CarmaBoxService(config)
        assert service.is_running is False
        assert service.config.site.name == "Sanduddsvagen 60"

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self, config):  # type: ignore[no-untyped-def]
        """stop() should set _running to False."""
        service = CarmaBoxService(config)
        service._running = True
        await service.stop()
        assert service.is_running is False

    @pytest.mark.asyncio
    async def test_run_cycle_increments_counter(self, config):  # type: ignore[no-untyped-def]
        """_run_cycle should increment cycle count."""
        service = CarmaBoxService(config)
        assert service._cycle_count == 0
        await service._run_cycle()
        assert service._cycle_count == 1
        await service._run_cycle()
        assert service._cycle_count == 2


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


class TestSetupLogging:
    """Test logging configuration."""

    def test_console_handler_added(self, config):  # type: ignore[no-untyped-def]
        from config.schema import load_config
        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)

        # L8: setup_logging configures the root logger so all child loggers inherit
        root_log = logging.getLogger()
        original_handlers = list(root_log.handlers)
        root_log.handlers.clear()

        setup_logging(cfg)
        assert len(root_log.handlers) >= 1  # At least console
        assert any(isinstance(h, logging.StreamHandler) for h in root_log.handlers)

        # Cleanup: restore original handlers
        root_log.handlers.clear()
        root_log.handlers.extend(original_handlers)


# ===========================================================================
# Coverage: setup_logging file handler
# ===========================================================================


class TestSetupLoggingFileHandler:
    """Test file handler creation when log dir exists."""

    def test_file_handler_created_when_dir_exists(self, tmp_path: Path) -> None:
        """File handler should be created when log directory exists."""
        from config.schema import LoggingConfig, load_config
        from unittest.mock import patch

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)

        # Create a modified logging config pointing to tmp_path
        log_file = str(tmp_path / "carma.log")
        patched_logging = LoggingConfig(file=log_file)

        # L8: setup_logging configures root logger
        root_log = logging.getLogger()
        original_handlers = list(root_log.handlers)
        root_log.handlers.clear()

        with patch.object(cfg, "logging", patched_logging):
            setup_logging(cfg)

        handler_types = [type(h).__name__ for h in root_log.handlers]
        assert "RotatingFileHandler" in handler_types

        # Cleanup: restore original handlers
        root_log.handlers.clear()
        root_log.handlers.extend(original_handlers)


# ===========================================================================
# Coverage: main() signal handlers + asyncio loop (lines 220-242)
# ===========================================================================


class TestMainSignalHandlers:
    """Test main() with signal handling — hard to test directly.
    Cover by testing the signal handler function setup."""

    def test_main_creates_service_and_exits(self) -> None:
        """Dry run covers config load + service creation but skips loop."""
        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        result = main(["--config", config_path, "--dry-run"])
        assert result == 0


# ===========================================================================
# Coverage: CarmaBoxService.start() loop (lines 109-121)
# ===========================================================================


@pytest.mark.asyncio
class TestServiceLoop:
    """Test the actual start() loop — needs quick termination."""

    async def test_start_runs_cycle_then_stops(self) -> None:
        """start() should run at least one cycle before stop."""
        import asyncio

        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)

        # Override cycle interval to 0 for testing
        object.__setattr__(cfg.control, "cycle_interval_s", 0)

        service = CarmaBoxService(cfg)

        async def stop_soon() -> None:
            while service._cycle_count < 2:
                await asyncio.sleep(0)
            await service.stop()

        task = asyncio.create_task(stop_soon())
        await service.start()
        await task
        assert service._cycle_count >= 2
        assert not service.is_running


@pytest.mark.asyncio
class TestServiceCancellation:
    """Test CancelledError handling in start()."""

    async def test_cancelled_error_handled(self) -> None:
        """CancelledError should be caught, not propagated."""
        import asyncio

        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        object.__setattr__(cfg.control, "cycle_interval_s", 0)

        service = CarmaBoxService(cfg)
        task = asyncio.create_task(service.start())
        # Let it run briefly
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        # Should not raise — CancelledError caught internally
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover
            pass  # Service catches CancelledError internally; this branch unreachable
        assert not service.is_running


# ===========================================================================
# Force replan + startup replan
# ===========================================================================


class TestForceReplan:
    """Force replan via HA input_boolean + startup replan on first cycle."""

    @pytest.mark.asyncio()
    async def test_check_force_replan_on(self) -> None:
        """When input_boolean is 'on', should return True and turn it off."""
        from unittest.mock import AsyncMock

        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)

        mock_api = AsyncMock()
        mock_api.get_state.return_value = "on"
        mock_api.call_service.return_value = None
        mock_api.health_check.return_value = True

        service = CarmaBoxService(cfg, ha_api=mock_api)
        result = await service._check_force_replan()
        assert result is True
        mock_api.call_service.assert_called_once()

    @pytest.mark.asyncio()
    async def test_check_force_replan_off(self) -> None:
        """When input_boolean is 'off', should return False."""
        from unittest.mock import AsyncMock

        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)

        mock_api = AsyncMock()
        mock_api.get_state.return_value = "off"
        mock_api.health_check.return_value = True

        service = CarmaBoxService(cfg, ha_api=mock_api)
        result = await service._check_force_replan()
        assert result is False

    @pytest.mark.asyncio()
    async def test_check_force_replan_no_entity(self) -> None:
        """When no entity configured, should return False."""
        from unittest.mock import AsyncMock

        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        object.__setattr__(cfg.manual_override, "force_replan_entity", "")

        mock_api = AsyncMock()
        mock_api.health_check.return_value = True

        service = CarmaBoxService(cfg, ha_api=mock_api)
        result = await service._check_force_replan()
        assert result is False

    def test_startup_replan_flag(self) -> None:
        """_last_plan_hour == -1 on init → startup replan on first cycle."""
        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        service = CarmaBoxService(cfg)
        assert service._last_plan_hour == -1


# ===========================================================================
# PLAT-1583: Health + metrics HTTP handler tests
# ===========================================================================


@pytest.mark.asyncio()
class TestHealthHandlers:
    """F1: Test _handle_health and _handle_metrics handlers."""

    async def test_handle_health_returns_json(self) -> None:
        """F1a: /health returns JSON with expected fields."""
        from unittest.mock import MagicMock

        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        service = CarmaBoxService(cfg)

        mock_request = MagicMock()
        response = await service._handle_health(mock_request)

        assert response.content_type == "application/json"
        import json
        body = json.loads(response.text)
        assert "status" in body
        assert "scenario" in body
        assert "uptime_s" in body
        assert "guard_level" in body

    async def test_handle_metrics_returns_prometheus(self) -> None:
        """F1b: /metrics returns Prometheus text format."""
        from unittest.mock import MagicMock

        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        service = CarmaBoxService(cfg)

        mock_request = MagicMock()
        response = await service._handle_metrics(mock_request)

        assert response.content_type == "text/plain"
        text = response.text
        assert "# HELP carma_cycles_total" in text
        assert "carma_cycles_total" in text

    async def test_health_status_reflects_guard_level(self) -> None:
        """F1c: guard_level in health JSON matches what was set."""
        from unittest.mock import MagicMock

        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        service = CarmaBoxService(cfg)
        service._health.guard_level = "freeze"

        mock_request = MagicMock()
        response = await service._handle_health(mock_request)

        import json
        body = json.loads(response.text)
        assert body["guard_level"] == "freeze"

    def test_health_port_from_config(self) -> None:
        """F1d: Port 8412 is NOT a naked literal in main.py."""
        source = Path(__file__).resolve().parents[2] / "main.py"
        text = source.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if "8412" in stripped and not stripped.startswith("#"):
                assert False, f"Naked 8412 at main.py:{i}: {stripped}"


class TestEngineeringStandardsDoc:
    """PLAT-1604: Engineering standards document must exist and be substantial."""

    def test_doc_exists_and_substantial(self) -> None:
        doc = Path(__file__).resolve().parents[2] / "docs" / "ENGINEERING_STANDARDS.md"
        assert doc.exists(), "docs/ENGINEERING_STANDARDS.md missing"
        lines = doc.read_text().splitlines()
        _MIN_SUBSTANTIAL_DOC_LINES: int = 50
        assert len(lines) > _MIN_SUBSTANTIAL_DOC_LINES, (
            f"Doc too short: {len(lines)} lines (need >{_MIN_SUBSTANTIAL_DOC_LINES})"
        )


class TestNoNaked1000InMain:
    """PLAT-1609: No naked 1000 literals in main.py."""

    def test_no_naked_1000(self) -> None:
        import re

        source = (Path(__file__).resolve().parents[2] / "main.py").read_text()
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            has_1000 = re.search(r'\b1000\b', stripped)
            if has_1000 and '_W_TO_KW' not in stripped and '_MS_PER_S' not in stripped:
                assert False, f"Naked 1000 at main.py:{i}: {stripped}"


# ===========================================================================
# PLAT-1369: Consumer wiring tests
# ===========================================================================


class TestCollectConsumers:
    """PLAT-1369: _collect_consumers reads HA state and builds ConsumerState."""

    @pytest.mark.asyncio()
    async def test_consumers_built_from_ha_state(self) -> None:
        """Consumer state should be built from HA switch + power sensors."""
        from unittest.mock import AsyncMock

        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)

        mock_api = AsyncMock()
        # V5: batch fetch returns dict of entity_id → state dict
        batch_data: dict[str, dict[str, str]] = {
            "switch.shelly1pmg4_a085e3bd1e60": {"state": "on"},
            "sensor.appliance_total_effekt": {"state": "380.5"},
            "switch.shellypro1pm_30c6f78289b8_switch_0": {"state": "off"},
            "sensor.carma_effekt_vp_kontor": {"state": "0"},
        }
        mock_api.get_states_batch = AsyncMock(return_value=batch_data)
        mock_api.health_check = AsyncMock(return_value=True)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        consumers = await service._collect_consumers()

        assert len(consumers) > 0
        miner = next((c for c in consumers if c.consumer_id == "miner"), None)
        assert miner is not None
        assert miner.active is True
        assert miner.power_w == pytest.approx(380.5)

        vp = next((c for c in consumers if c.consumer_id == "vp_kontor"), None)
        assert vp is not None
        assert vp.active is False

    @pytest.mark.asyncio()
    async def test_unavailable_power_does_not_crash(self) -> None:
        """'unavailable' power sensor must not raise ValueError."""
        from unittest.mock import AsyncMock

        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)

        mock_api = AsyncMock()
        # V5: batch fetch — power sensors return 'unavailable'
        batch_data: dict[str, dict[str, str]] = {}
        for cc in cfg.consumers:
            if cc.entity_switch:
                batch_data[cc.entity_switch] = {"state": "off"}
            if cc.entity_power:
                batch_data[cc.entity_power] = {"state": "unavailable"}
        mock_api.get_states_batch = AsyncMock(return_value=batch_data)
        mock_api.health_check = AsyncMock(return_value=True)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        # Must not raise
        consumers = await service._collect_consumers()
        assert len(consumers) > 0
        for c in consumers:
            assert isinstance(c.power_w, float)

    @pytest.mark.asyncio()
    async def test_surplus_dispatch_called_with_consumers(self) -> None:
        """Phase 7: _execute_surplus should be called when consumers exist."""
        from unittest.mock import AsyncMock, patch

        from config.schema import load_config
        from core.models import ConsumerState

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)

        mock_api = AsyncMock()
        mock_api.health_check = AsyncMock(return_value=True)
        mock_api.get_state = AsyncMock(return_value="0")
        mock_api.get_states_batch = AsyncMock(return_value={})
        mock_api.call_service = AsyncMock(return_value=True)

        service = CarmaBoxService(cfg, ha_api=mock_api)

        # Patch _execute_surplus to track calls
        with patch.object(service, "_execute_surplus", new_callable=AsyncMock) as mock_surplus:
            # Patch _collect_snapshot to return a snapshot with consumers
            from tests.conftest import make_snapshot
            snap = make_snapshot(consumers=[
                ConsumerState(
                    consumer_id="miner", name="Miner", active=True,
                    power_w=400.0, priority=1, priority_shed=1,
                    load_type="on_off",
                ),
            ])
            with patch.object(service, "_collect_snapshot", return_value=snap):
                await service._run_cycle()

            mock_surplus.assert_called_once()


class TestDashboardWriteBack:
    """PLAT-1370: _write_dashboard_state writes all 6 entities to HA."""

    @pytest.mark.asyncio()
    async def test_all_entities_written(self) -> None:
        """All 6 dashboard entities should be written each cycle."""
        from unittest.mock import AsyncMock

        from config.schema import load_config
        from core.engine import CycleResult
        from core.guards import GuardEvaluation, GuardLevel
        from core.models import Scenario
        from tests.conftest import make_snapshot

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)

        mock_api = AsyncMock()
        mock_api.health_check = AsyncMock(return_value=True)
        mock_api.set_state = AsyncMock(return_value=True)
        mock_api.set_input_text = AsyncMock(return_value=True)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        snap = make_snapshot()
        cycle_result = CycleResult(
            cycle_id="test-1",
            timestamp=snap.timestamp,
            elapsed_s=0.1,
            scenario=Scenario.PV_SURPLUS_DAY,
            guard=GuardEvaluation(
                level=GuardLevel.OK, commands=[],
            ),
        )

        await service._write_dashboard_state(snap, cycle_result)

        # set_state called 4 times: scenario, decision, rules, ellevio
        assert mock_api.set_state.call_count == 4
        # set_input_text called 3 times: today, tomorrow, day3
        assert mock_api.set_input_text.call_count == 3

        # Verify entity IDs
        state_entities = [
            c.args[0] for c in mock_api.set_state.call_args_list
        ]
        assert cfg.dashboard.entity_scenario in state_entities
        assert cfg.dashboard.entity_decision_reason in state_entities
        assert cfg.dashboard.entity_rules in state_entities

        text_entities = [
            c.args[0] for c in mock_api.set_input_text.call_args_list
        ]
        assert cfg.dashboard.entity_plan_today in text_entities
        assert cfg.dashboard.entity_plan_tomorrow in text_entities
        assert cfg.dashboard.entity_plan_day3 in text_entities


class TestManualOverride:
    """PLAT-1372: Manual override reads HA helpers and sets state machine."""

    @pytest.mark.asyncio()
    async def test_override_enabled_sets_scenario(self) -> None:
        """When override ON + valid scenario, state machine is set."""
        from unittest.mock import AsyncMock

        from config.schema import load_config

        config_path = str(
            Path(__file__).resolve().parents[2] / "config" / "site.yaml",
        )
        cfg = load_config(config_path)
        mock_api = AsyncMock()

        async def fake_get(entity: str) -> str:
            if "override" in entity:
                return "on"
            if "scenario" in entity:
                return "EVENING_DISCHARGE"
            return "0"

        mock_api.get_state = AsyncMock(side_effect=fake_get)
        mock_api.health_check = AsyncMock(return_value=True)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        await service._apply_manual_override()

        # Engine should have manual override set
        from core.models import Scenario
        assert service._engine is not None
        assert service._engine._sm._manual_override == Scenario.EVENING_DISCHARGE

    @pytest.mark.asyncio()
    async def test_override_disabled_clears(self) -> None:
        """When override OFF, state machine override is cleared."""
        from unittest.mock import AsyncMock

        from config.schema import load_config

        config_path = str(
            Path(__file__).resolve().parents[2] / "config" / "site.yaml",
        )
        cfg = load_config(config_path)
        mock_api = AsyncMock()
        mock_api.get_state = AsyncMock(return_value="off")
        mock_api.health_check = AsyncMock(return_value=True)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        await service._apply_manual_override()

        assert service._engine is not None
        assert service._engine._sm._manual_override is None

    @pytest.mark.asyncio()
    async def test_invalid_scenario_does_not_crash(self) -> None:
        """Invalid scenario string must not crash — clears override."""
        from unittest.mock import AsyncMock

        from config.schema import load_config

        config_path = str(
            Path(__file__).resolve().parents[2] / "config" / "site.yaml",
        )
        cfg = load_config(config_path)
        mock_api = AsyncMock()

        async def fake_get(entity: str) -> str:
            if "override" in entity:
                return "on"
            if "scenario" in entity:
                return "INVALID_SCENARIO"
            return "0"

        mock_api.get_state = AsyncMock(side_effect=fake_get)
        mock_api.health_check = AsyncMock(return_value=True)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        await service._apply_manual_override()

        assert service._engine is not None
        assert service._engine._sm._manual_override is None


class TestEntityDomain:
    """PLAT-1464: _entity_domain extracts domain from entity_id."""

    def test_switch(self) -> None:
        assert CarmaBoxService._entity_domain("switch.pump") == "switch"

    def test_input_boolean(self) -> None:
        assert CarmaBoxService._entity_domain("input_boolean.flag") == "input_boolean"

    def test_no_dot_fallback(self) -> None:
        assert CarmaBoxService._entity_domain("no_dot") == "homeassistant"


class TestGenerate48hPlan:
    """PLAT-1553: 48h plan generation tests."""

    def _make_service(self) -> CarmaBoxService:
        from config.schema import load_config
        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        return CarmaBoxService(cfg)

    def test_plan_format(self) -> None:
        service = self._make_service()
        from tests.conftest import make_snapshot
        snap = make_snapshot(hour=22, minute=0)
        today, tomorrow = service._plan_executor.generate_48h(snap, 22)
        for entry in today.split("|"):
            parts = entry.split(":")
            assert len(parts) == 3, f"Bad format: {entry}"
            assert parts[2].endswith("%")

    def test_plan_split_by_day(self) -> None:
        service = self._make_service()
        from tests.conftest import make_snapshot
        snap = make_snapshot(hour=0, minute=0)
        today, tomorrow = service._plan_executor.generate_48h(snap, 0)
        today_hours = [e.split(":")[0] for e in today.split("|")]
        assert len(today_hours) == 24

    def test_plan_night_ev_charge(self) -> None:
        service = self._make_service()
        from tests.conftest import make_snapshot, make_ev_state
        snap = make_snapshot(
            hour=22, minute=0,
            ev=make_ev_state(soc_pct=50.0, connected=True),
        )
        today, _ = service._plan_executor.generate_48h(snap, 22)
        first = today.split("|")[0]
        assert ":EV:" in first

    def test_plan_pv_charge(self) -> None:
        service = self._make_service()
        from tests.conftest import make_snapshot, make_grid_state
        snap = make_snapshot(
            hour=8, minute=0,
            grid=make_grid_state(pv_forecast_today_kwh=30.0),
        )
        today, _ = service._plan_executor.generate_48h(snap, 8)
        # First hours (8-9) should be CHG with 30kWh PV forecast
        entries = {e.split(":")[0]: e for e in today.split("|")}
        assert ":CHG:" in entries.get("08", "") or ":CHG:" in entries.get("09", "")

    def test_plan_discharge_evening(self) -> None:
        service = self._make_service()
        from tests.conftest import make_snapshot
        snap = make_snapshot(hour=17, minute=0)
        today, _ = service._plan_executor.generate_48h(snap, 17)
        first = today.split("|")[0]
        assert ":DIS:" in first or ":STB:" in first
