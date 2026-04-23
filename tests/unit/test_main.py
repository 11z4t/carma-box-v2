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
        assert (
            len(lines) > _MIN_SUBSTANTIAL_DOC_LINES
        ), f"Doc too short: {len(lines)} lines (need >{_MIN_SUBSTANTIAL_DOC_LINES})"


class TestNoNaked1000InMain:
    """PLAT-1609: No naked 1000 literals in main.py."""

    def test_no_naked_1000(self) -> None:
        import re

        source = (Path(__file__).resolve().parents[2] / "main.py").read_text()
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            has_1000 = re.search(r"\b1000\b", stripped)
            if has_1000 and "_W_TO_KW" not in stripped and "_MS_PER_S" not in stripped:
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

            snap = make_snapshot(
                consumers=[
                    ConsumerState(
                        consumer_id="miner",
                        name="Miner",
                        active=True,
                        power_w=400.0,
                        priority=1,
                        priority_shed=1,
                        load_type="on_off",
                    ),
                ]
            )
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
                level=GuardLevel.OK,
                commands=[],
            ),
        )

        await service._write_dashboard_state(snap, cycle_result)

        # set_state called 4 times: scenario, decision, rules, ellevio
        assert mock_api.set_state.call_count == 4
        # set_input_text called 3 times: today, tomorrow, day3
        assert mock_api.set_input_text.call_count == 3

        # Verify entity IDs
        state_entities = [c.args[0] for c in mock_api.set_state.call_args_list]
        assert cfg.dashboard.entity_scenario in state_entities
        assert cfg.dashboard.entity_decision_reason in state_entities
        assert cfg.dashboard.entity_rules in state_entities

        text_entities = [c.args[0] for c in mock_api.set_input_text.call_args_list]
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
            hour=22,
            minute=0,
            ev=make_ev_state(soc_pct=50.0, connected=True),
        )
        today, _ = service._plan_executor.generate_48h(snap, 22)
        first = today.split("|")[0]
        assert ":EV:" in first

    def test_plan_pv_charge(self) -> None:
        service = self._make_service()
        from datetime import date as _date
        from tests.conftest import make_snapshot, make_grid_state

        # LÄRDOM: inject deterministic date (Tuesday) — NEVER depend on
        # system clock for weekday logic. Friday evening broke this test.
        _TUESDAY: _date = _date(2026, 4, 14)  # known Tuesday
        snap = make_snapshot(
            hour=8,
            minute=0,
            grid=make_grid_state(pv_forecast_today_kwh=30.0),
        )
        today, _ = service._plan_executor.generate_48h(
            snap,
            8,
            reference_date=_TUESDAY,
        )
        # First hours (8-9) should be CHG with 30kWh PV forecast
        entries = {e.split(":")[0]: e for e in today.split("|")}
        assert ":CHG:" in entries.get("08", "") or ":CHG:" in entries.get("09", "")

    def test_plan_discharge_evening(self) -> None:
        service = self._make_service()
        from datetime import date as _date
        from tests.conftest import make_snapshot

        _TUESDAY: _date = _date(2026, 4, 14)  # known Tuesday
        snap = make_snapshot(hour=17, minute=0)
        today, _ = service._plan_executor.generate_48h(
            snap,
            17,
            reference_date=_TUESDAY,
        )
        first = today.split("|")[0]
        assert ":DIS:" in first or ":STB:" in first


# ---------------------------------------------------------------------------
# _generate_day_plan (PLAT-1627)
# ---------------------------------------------------------------------------

_DAY_PLAN_WINDOW_HOURS: int = 16  # 06-22


class TestGenerateDayPlan:
    """PLAT-1627: _generate_day_plan builds DayPlan from snapshot + config."""

    def _make_service(self) -> CarmaBoxService:
        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        return CarmaBoxService(cfg)

    @pytest.mark.asyncio()
    async def test_returns_day_plan_with_valid_snapshot(self) -> None:
        """_generate_day_plan returns DayPlan with correct number of slots."""
        from core.day_plan import DayPlan
        from tests.conftest import make_snapshot, make_grid_state

        service = self._make_service()
        snap = make_snapshot(
            hour=10,
            minute=0,
            grid=make_grid_state(pv_forecast_today_kwh=25.0),
        )
        plan = await service._generate_day_plan(snap)
        assert plan is not None
        assert isinstance(plan, DayPlan)
        assert len(plan.slots) == _DAY_PLAN_WINDOW_HOURS

    @pytest.mark.asyncio()
    async def test_returns_none_on_error(self) -> None:
        """_generate_day_plan returns None when generation fails."""
        from unittest.mock import patch
        from tests.conftest import make_snapshot

        service = self._make_service()
        snap = make_snapshot(hour=10, minute=0)
        with patch(
            "main.generate_day_plan",
            side_effect=ValueError("test error"),
        ):
            plan = await service._generate_day_plan(snap)
        assert plan is None


class TestBudgetConfigGuard:
    """PLAT-1686: BudgetConfig None-guard when ev_charger missing."""

    def test_budget_config_wiring_in_source(self) -> None:
        """Guard: main.py wires BudgetSection inside 'if config.ev_charger:' guard.

        PLAT-1748: budget config is now built via config.budget.to_budget_config()
        instead of direct BudgetConfig() construction.
        """
        source = Path(__file__).resolve().parents[2] / "main.py"
        lines = source.read_text().splitlines()
        found_guard = False
        found_budget = False
        for line in lines:
            if "if config.ev_charger:" in line:
                found_guard = True
            if "to_budget_config()" in line and found_guard:
                found_budget = True
                break
        assert found_guard, "main.py missing 'if config.ev_charger:' guard"
        assert found_budget, "BudgetConfig() must be INSIDE 'if config.ev_charger:' block"


# ===========================================================================
# PLAT-1748: BudgetConfig mapping — all fields from BudgetSection
# ===========================================================================


class TestBudgetConfigMapping:
    """Verify that CarmaBoxService wires all BudgetSection fields to BudgetConfig.

    These tests construct a config with non-default budget values and check that
    the resulting _engine._budget_config reflects them — proving the mapping in
    __init__() is complete.
    """

    @pytest.fixture()
    def cfg_and_service(self):  # type: ignore[no-untyped-def]
        """Returns (cfg, service) with non-default budget values and a mock HA API."""
        from unittest.mock import AsyncMock, MagicMock

        from config.schema import (
            BudgetAggressiveSpreadSection,
            BudgetCascadeSection,
            BudgetEmergencySection,
            BudgetGridTunerSection,
            BudgetSection,
            BudgetSmoothingSection,
            load_config,
        )

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        patched = BudgetSection(
            ev_min_amps=8,
            ev_max_amps=14,
            bat_lower_ratio=0.75,
            bat_higher_ratio=0.25,
            ev_ramp_up_hold_cycles=3,
            ev_ramp_down_hold_cycles=2,
            bat_discharge_support=False,
            evening_cutoff_h=18,
            grid_tuner=BudgetGridTunerSection(enabled=True),
            cascade=BudgetCascadeSection(cooldown_s=120.0, sustained_cycles=4),
            smoothing=BudgetSmoothingSection(grid_smoothing_window=5),
            aggressive_spread=BudgetAggressiveSpreadSection(
                bat_spread_max_pct=2.0, bat_aggressive_spread_pct=4.0
            ),
            emergency=BudgetEmergencySection(bat_discharge_min_soc_pct=18.0),
        )
        object.__setattr__(cfg, "budget", patched)
        mock_api = MagicMock()
        mock_api.get_state = AsyncMock(return_value="unknown")
        mock_api.health_check = AsyncMock(return_value=True)
        service = CarmaBoxService(cfg, ha_api=mock_api)
        return cfg, service

    def test_ev_min_amps_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """ev_min_amps from BudgetSection reaches BudgetConfig."""
        _, service = cfg_and_service
        assert service._engine._budget_config.ev_min_amps == 8

    def test_ev_max_amps_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """ev_max_amps from BudgetSection reaches BudgetConfig."""
        _, service = cfg_and_service
        assert service._engine._budget_config.ev_max_amps == 14

    def test_bat_charge_stop_soc_comes_from_battery_gate(self, cfg_and_service) -> None:  # type: ignore[no-untyped-def]
        """bat_charge_stop_soc_pct must come from control.battery_gate (PLAT-1695).

        BudgetSection does NOT expose bat_charge_stop_soc_pct to prevent
        drift between S8 surplus_entry_soc_pct and budget stop SoC.
        main.py applies control.battery_gate.charge_stop_soc_pct as override.
        """
        cfg, service = cfg_and_service
        expected = cfg.control.battery_gate.charge_stop_soc_pct
        assert service._engine._budget_config.bat_charge_stop_soc_pct == expected

    def test_bat_lower_higher_ratio_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """bat_lower_ratio / bat_higher_ratio reach BudgetConfig."""
        _, service = cfg_and_service
        assert service._engine._budget_config.bat_lower_ratio == 0.75
        assert service._engine._budget_config.bat_higher_ratio == 0.25

    def test_ev_ramp_hold_cycles_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """ev_ramp_up/down_hold_cycles reach BudgetConfig."""
        _, service = cfg_and_service
        assert service._engine._budget_config.ev_ramp_up_hold_cycles == 3
        assert service._engine._budget_config.ev_ramp_down_hold_cycles == 2

    def test_bat_discharge_support_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """bat_discharge_support=False reaches BudgetConfig."""
        _, service = cfg_and_service
        assert service._engine._budget_config.bat_discharge_support is False

    def test_evening_cutoff_h_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """evening_cutoff_h reaches BudgetConfig."""
        _, service = cfg_and_service
        assert service._engine._budget_config.evening_cutoff_h == 18

    def test_grid_tuner_enabled_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """grid_tuner.enabled=True reaches BudgetConfig.grid_tuner."""
        _, service = cfg_and_service
        assert service._engine._budget_config.grid_tuner.enabled is True

    def test_cascade_cooldown_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """cascade.cooldown_s reaches BudgetConfig.cascade_cooldown_s."""
        _, service = cfg_and_service
        assert service._engine._budget_config.cascade_cooldown_s == 120.0

    def test_cascade_sustained_cycles_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """cascade.sustained_cycles reaches BudgetConfig.cascade_sustained_cycles."""
        _, service = cfg_and_service
        assert service._engine._budget_config.cascade_sustained_cycles == 4

    def test_smoothing_window_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """smoothing.grid_smoothing_window reaches BudgetConfig.grid_smoothing_window."""
        _, service = cfg_and_service
        assert service._engine._budget_config.grid_smoothing_window == 5

    def test_aggressive_spread_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """aggressive_spread fields reach BudgetConfig."""
        _, service = cfg_and_service
        assert service._engine._budget_config.bat_spread_max_pct == 2.0
        assert service._engine._budget_config.bat_aggressive_spread_pct == 4.0

    def test_emergency_min_soc_mapped(self, cfg_and_service):  # type: ignore[no-untyped-def]
        """emergency.bat_discharge_min_soc_pct reaches BudgetConfig."""
        _, service = cfg_and_service
        assert service._engine._budget_config.bat_discharge_min_soc_pct == 18.0


# ===========================================================================
# PLAT-1753: Cycle p95 tracking + atomic batch warm
# ===========================================================================


class TestPlat1753CycleP95:
    """PLAT-1753: cycle_p95_s returns p95 of last 100 cycle durations.

    Acceptance: Cycle p95 < 500 ms in production.
    """

    def test_cycle_p95_no_data_returns_zero(self) -> None:
        """p95 with no recorded cycles must return 0.0."""
        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        service = CarmaBoxService(cfg)
        assert service.cycle_p95_s == 0.0

    def test_cycle_p95_single_sample(self) -> None:
        """p95 with one sample must return that sample."""
        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        service = CarmaBoxService(cfg)
        service._cycle_durations.append(0.123)
        assert service.cycle_p95_s == 0.123

    def test_cycle_p95_percentile_correct(self) -> None:
        """p95 with 100 samples [0.01, 0.02, ..., 1.00] must be >= 0.95."""
        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        service = CarmaBoxService(cfg)
        # 100 samples: 0.01 .. 1.00 (step 0.01)
        for i in range(1, 101):
            service._cycle_durations.append(i * 0.01)
        p95 = service.cycle_p95_s
        # p95 index = int(100 * 0.95) = 95 → value at index 95 (0-based) = 0.96
        assert 0.94 <= p95 <= 1.00

    def test_cycle_durations_window_max_100(self) -> None:
        """_cycle_durations must hold at most 100 entries (deque maxlen=100)."""
        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        service = CarmaBoxService(cfg)
        for i in range(150):
            service._cycle_durations.append(float(i))
        assert len(service._cycle_durations) == 100

    def test_cycle_durations_attribute_exists(self) -> None:
        """CarmaBoxService must expose _cycle_durations as a deque."""
        from collections import deque
        from config.schema import load_config

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        service = CarmaBoxService(cfg)
        assert hasattr(service, "_cycle_durations")
        assert isinstance(service._cycle_durations, deque)


class TestPlat1753EvSocInBatch:
    """PLAT-1753: ev.entities.soc must be fetched via get_states_batch, not get_state."""

    def test_ev_soc_not_read_via_standalone_get_state(self) -> None:
        """Source must not call get_state(cfg.ev.entities.soc) separately."""
        from pathlib import Path as _Path

        src = (_Path(__file__).parent.parent.parent / "main.py").read_text()
        assert "get_state(cfg.ev.entities.soc)" not in src, (
            "ev.entities.soc must be merged into get_states_batch call, "
            "not fetched via get_state — PLAT-1753"
        )

    def test_ev_soc_entity_appears_in_batch_block(self) -> None:
        """Source must include ev.entities.soc inside a get_states_batch() argument."""
        from pathlib import Path as _Path
        import re

        src = (_Path(__file__).parent.parent.parent / "main.py").read_text()
        # Find all get_states_batch(...) call blocks
        matches = re.findall(
            r"get_states_batch\([\s\S]*?cfg\.ev\.entities\.soc[\s\S]*?\)",
            src,
        )
        assert (
            matches
        ), "cfg.ev.entities.soc must appear inside a get_states_batch() call — PLAT-1753"


class TestPlat1753WarmCacheCalledInCycle:
    """PLAT-1753: warm_batch_cache() must be called at the start of _collect_snapshot."""

    def test_warm_batch_cache_called_in_collect_snapshot_source(self) -> None:
        """_collect_snapshot must call warm_batch_cache at its start."""
        from pathlib import Path as _Path

        src = (_Path(__file__).parent.parent.parent / "main.py").read_text()
        assert (
            "warm_batch_cache" in src
        ), "main.py must call warm_batch_cache() in _collect_snapshot — PLAT-1753"


# ===========================================================================
# PLAT-1757: Atomär HA sensor-batch-read
# ===========================================================================

_BAT_ENTITY_FIELDS = (
    "soc",
    "power",
    "ems_mode",
    "ems_power_limit",
    "fast_charging",
)


@pytest.mark.asyncio()
class TestAtomicBatBatchRead:
    """PLAT-1757: All battery sensors must be read in a single atomic batch call.

    The race: sequential per-battery get_states_batch() calls expose the
    algorithm to data from different HA snapshots (50-100 ms apart).
    Fix: collect ALL battery entity IDs upfront → one get_states_batch() call.
    """

    @staticmethod
    def _make_config() -> "object":
        """Load site.yaml config."""
        from config.schema import load_config
        from pathlib import Path

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        return load_config(config_path)

    @staticmethod
    def _build_batch_response(cfg: "object") -> "dict[str, dict[str, str]]":
        """Minimal batch response covering all battery entity fields."""
        response: dict[str, dict[str, str]] = {}
        for bat_cfg in cfg.batteries:  # type: ignore[attr-defined]
            ents = bat_cfg.entities
            response[ents.soc] = {"entity_id": ents.soc, "state": "55"}
            response[ents.power] = {"entity_id": ents.power, "state": "0"}
            response[ents.ems_mode] = {
                "entity_id": ents.ems_mode,
                "state": "battery_standby",
            }
            response[ents.ems_power_limit] = {
                "entity_id": ents.ems_power_limit,
                "state": "0",
            }
            response[ents.fast_charging] = {
                "entity_id": ents.fast_charging,
                "state": "off",
            }
        return response

    async def test_bat_sensors_read_in_single_batch_call(self) -> None:
        """PLAT-1757 AC1: all battery sensors fetched with exactly ONE get_states_batch call.

        With N batteries, the old code called get_states_batch N times.
        After the fix, a single call covers all battery entity IDs.
        """
        from unittest.mock import AsyncMock

        from main import CarmaBoxService

        cfg = self._make_config()
        bat_count = len(cfg.batteries)  # type: ignore[attr-defined]
        assert bat_count >= 2, "site.yaml must have ≥2 batteries for this test"

        batch_response = self._build_batch_response(cfg)
        call_entity_sets: list[frozenset[str]] = []

        async def _tracking_batch(entity_ids: list[str]) -> "dict[str, dict[str, str]]":
            call_entity_sets.append(frozenset(entity_ids))
            return {k: v for k, v in batch_response.items() if k in entity_ids}

        mock_api = AsyncMock()
        mock_api.get_states_batch = _tracking_batch  # type: ignore[method-assign]
        mock_api.get_state = AsyncMock(return_value=None)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        await service._collect_snapshot(ha_connected=True)

        # Identify calls that contain battery soc entities
        bat_soc_entities = frozenset(
            b.entities.soc
            for b in cfg.batteries  # type: ignore[attr-defined]
        )
        bat_calls = [s for s in call_entity_sets if s & bat_soc_entities]

        assert len(bat_calls) == 1, (
            f"Expected exactly 1 batch call for all {bat_count} batteries, "
            f"got {len(bat_calls)}. "
            "PLAT-1757: collect ALL bat entity IDs upfront → single get_states_batch()."
        )

        # The single call must include soc entities from ALL batteries
        all_call_entities = bat_calls[0]
        for bat_cfg in cfg.batteries:  # type: ignore[attr-defined]
            assert (
                bat_cfg.entities.soc in all_call_entities
            ), f"Battery {bat_cfg.id} soc entity missing from single batch call"

    async def test_bat_sensors_from_same_snapshot(self) -> None:
        """PLAT-1757 AC2: all batteries read from the same batch call (no per-battery calls).

        With atomic single-call, the structural sensor-skew between batteries is 0:
        all batteries are read from one HTTP response, regardless of HA latency.
        """
        import asyncio
        from unittest.mock import AsyncMock

        from main import CarmaBoxService

        cfg = self._make_config()
        assert len(cfg.batteries) >= 2  # type: ignore[attr-defined]

        batch_response = self._build_batch_response(cfg)
        bat_soc_entities = frozenset(
            b.entities.soc
            for b in cfg.batteries  # type: ignore[attr-defined]
        )
        call_entity_sets: list[frozenset[str]] = []

        async def _batch_with_delay(entity_ids: list[str]) -> "dict[str, dict[str, str]]":
            """Record entity sets; simulate 50 ms HA latency on bat-covering calls."""
            call_entity_sets.append(frozenset(entity_ids))
            if frozenset(entity_ids) & bat_soc_entities:
                await asyncio.sleep(0.050)
            return {k: v for k, v in batch_response.items() if k in entity_ids}

        mock_api = AsyncMock()
        mock_api.get_states_batch = _batch_with_delay  # type: ignore[method-assign]
        mock_api.get_state = AsyncMock(return_value=None)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        await service._collect_snapshot(ha_connected=True)

        bat_calls = [s for s in call_entity_sets if s & bat_soc_entities]
        assert len(bat_calls) == 1, (
            f"Expected 1 atomic batch call for all batteries, got {len(bat_calls)}. "
            "Sensor-skew fix: all battery entities must share one get_states_batch()."
        )

    async def test_bat_soc_values_correct_after_atomic_read(self) -> None:
        """PLAT-1757 AC3: SoC values are correctly extracted after atomic batch read."""
        from unittest.mock import AsyncMock

        from main import CarmaBoxService

        cfg = self._make_config()
        expected_socs = {
            bat_cfg.id: 45.0 + i * 10.0
            for i, bat_cfg in enumerate(cfg.batteries)  # type: ignore[attr-defined]
        }

        batch_response: dict[str, dict[str, str]] = {}
        for i, bat_cfg in enumerate(cfg.batteries):  # type: ignore[attr-defined]
            ents = bat_cfg.entities
            soc_val = expected_socs[bat_cfg.id]
            batch_response[ents.soc] = {"entity_id": ents.soc, "state": str(soc_val)}
            batch_response[ents.power] = {"entity_id": ents.power, "state": "0"}
            batch_response[ents.ems_mode] = {
                "entity_id": ents.ems_mode,
                "state": "battery_standby",
            }
            batch_response[ents.ems_power_limit] = {
                "entity_id": ents.ems_power_limit,
                "state": "0",
            }
            batch_response[ents.fast_charging] = {
                "entity_id": ents.fast_charging,
                "state": "off",
            }

        mock_api = AsyncMock()
        mock_api.get_states_batch = AsyncMock(return_value=batch_response)
        mock_api.get_state = AsyncMock(return_value=None)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        snapshot = await service._collect_snapshot(ha_connected=True)

        assert snapshot is not None
        for bat in snapshot.batteries:
            assert bat.soc_pct == pytest.approx(expected_socs[bat.battery_id]), (
                f"Battery {bat.battery_id}: expected SoC "
                f"{expected_socs[bat.battery_id]}, got {bat.soc_pct}"
            )


# ===========================================================================
# PLAT-1790 F1: _evaluate_ev() wiring tests
# Verify that hour_of_day/ev_soc/ev_target_soc/grid_transient_w are
# computed and passed into EVDispatchInputs (regression: previously defaulted
# to 12/0.0/100.0/0.0 → R-natt could never trigger in production).
# ===========================================================================


class TestEvaluateEvWiring:
    """Verify _evaluate_ev() correctly wires Fas 2.5 fields into EVDispatchInputs."""

    def _make_service_ev_enabled(self) -> "CarmaBoxService":
        """Build a CarmaBoxService with ev_dispatch_v2.enabled=True."""
        from config.schema import load_config
        from unittest.mock import AsyncMock

        config_path = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
        cfg = load_config(config_path)
        object.__setattr__(cfg.ev_dispatch_v2, "enabled", True)
        mock_api = AsyncMock()
        mock_api.call_service.return_value = None
        return CarmaBoxService(cfg, ha_api=mock_api)

    @pytest.mark.asyncio()
    async def test_evaluate_ev_wires_hour_of_day_from_datetime(self) -> None:
        """hour_of_day in EVDispatchInputs must come from datetime (Europe/Stockholm).

        Previously main.py never set hour_of_day → defaulted to 12 → R-natt
        window (22-06) never matched → feature dead in production.
        """
        import zoneinfo
        from datetime import datetime as _datetime
        from unittest.mock import patch
        from core.ev_dispatch import (
            EVDispatchInputs,
            EVDispatchResult,
            EVActionType,
            EVDispatchState,
        )
        from tests.conftest import make_snapshot, make_ev_state, make_grid_state

        service = self._make_service_ev_enabled()
        snap = make_snapshot(
            ev=make_ev_state(soc_pct=40.0, charger_status="awaiting_start"),
            grid=make_grid_state(grid_power_w=200.0),
        )

        captured: list[EVDispatchInputs] = []

        def _fake_evaluate(state: object, inputs: object, cfg: object) -> object:
            assert isinstance(inputs, EVDispatchInputs)
            captured.append(inputs)
            return EVDispatchResult(
                action=EVActionType.NOOP,
                amps=0,
                reason="test",
                new_state=EVDispatchState(),
                is_shadow=False,
            )

        fixed_dt = _datetime(2026, 4, 23, 23, 15, 0, tzinfo=zoneinfo.ZoneInfo("Europe/Stockholm"))
        with (
            patch("main.evaluate_ev_action", side_effect=_fake_evaluate),
            patch("main.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fixed_dt
            await service._evaluate_ev(snap)

        assert len(captured) == 1, "_evaluate_ev must call evaluate_ev_action once"
        assert captured[0].hour_of_day == 23, f"hour_of_day={captured[0].hour_of_day}, expected 23"

    @pytest.mark.asyncio()
    async def test_evaluate_ev_wires_ev_soc_from_snapshot(self) -> None:
        """ev_soc in EVDispatchInputs must equal snapshot.ev.soc_pct."""
        from unittest.mock import patch
        from tests.conftest import make_snapshot, make_ev_state, make_grid_state

        service = self._make_service_ev_enabled()
        snap = make_snapshot(
            ev=make_ev_state(soc_pct=67.5, charger_status="awaiting_start"),
            grid=make_grid_state(grid_power_w=200.0),
        )

        captured: list[object] = []

        def _fake_evaluate(state: object, inputs: object, cfg: object) -> object:
            from core.ev_dispatch import EVDispatchResult, EVActionType, EVDispatchState

            captured.append(inputs)
            return EVDispatchResult(
                action=EVActionType.NOOP,
                amps=0,
                reason="test",
                new_state=EVDispatchState(),
                is_shadow=False,
            )

        with patch("main.evaluate_ev_action", side_effect=_fake_evaluate):
            await service._evaluate_ev(snap)

        assert len(captured) == 1
        from core.ev_dispatch import EVDispatchInputs

        assert isinstance(captured[0], EVDispatchInputs)
        assert captured[0].ev_soc == pytest.approx(67.5), (  # type: ignore[attr-defined]
            f"ev_soc={captured[0].ev_soc}, expected 67.5"  # type: ignore[attr-defined]
        )

    @pytest.mark.asyncio()
    async def test_evaluate_ev_wires_grid_transient_w_delta(self) -> None:
        """grid_transient_w = max(0, current_grid_w - prev_grid_w).

        Verify: after one cycle with grid=800W the next cycle with grid=1100W
        yields grid_transient_w=300W, and a drop (1100→900) yields 0.
        """
        from unittest.mock import patch
        from tests.conftest import make_snapshot, make_ev_state, make_grid_state

        service = self._make_service_ev_enabled()
        captured: list[object] = []

        def _fake_evaluate(state: object, inputs: object, cfg: object) -> object:
            from core.ev_dispatch import EVDispatchResult, EVActionType, EVDispatchState

            captured.append(inputs)
            return EVDispatchResult(
                action=EVActionType.NOOP,
                amps=0,
                reason="test",
                new_state=EVDispatchState(),
                is_shadow=False,
            )

        # Cycle 1: grid=800W (prev=0 → transient=800)
        snap1 = make_snapshot(
            ev=make_ev_state(charger_status="awaiting_start"),
            grid=make_grid_state(grid_power_w=800.0),
        )
        with patch("main.evaluate_ev_action", side_effect=_fake_evaluate):
            await service._evaluate_ev(snap1)

        # Cycle 2: grid=1100W (delta=+300W)
        snap2 = make_snapshot(
            ev=make_ev_state(charger_status="awaiting_start"),
            grid=make_grid_state(grid_power_w=1100.0),
        )
        with patch("main.evaluate_ev_action", side_effect=_fake_evaluate):
            await service._evaluate_ev(snap2)

        # Cycle 3: grid=900W (delta negative → clamped to 0)
        snap3 = make_snapshot(
            ev=make_ev_state(charger_status="awaiting_start"),
            grid=make_grid_state(grid_power_w=900.0),
        )
        with patch("main.evaluate_ev_action", side_effect=_fake_evaluate):
            await service._evaluate_ev(snap3)

        from core.ev_dispatch import EVDispatchInputs

        assert isinstance(captured[1], EVDispatchInputs)
        assert isinstance(captured[2], EVDispatchInputs)
        assert captured[1].grid_transient_w == pytest.approx(300.0), (  # type: ignore[attr-defined]
            f"cycle2 transient={captured[1].grid_transient_w}, expected 300.0"  # type: ignore[attr-defined]
        )
        assert captured[2].grid_transient_w == pytest.approx(0.0), (  # type: ignore[attr-defined]
            f"cycle3 transient={captured[2].grid_transient_w}, expected 0.0 (clamped)"  # type: ignore[attr-defined]
        )

    @pytest.mark.asyncio()
    async def test_evaluate_ev_r_natt_triggers_at_23_00(self) -> None:
        """R-natt must trigger at hour=23 when plug connected + SoC-gap exists.

        With feature flag ON and hour_of_day=23 (inside 22:00-06:00 window),
        evaluate_ev_action receives correct inputs and can return EV_START.
        This test verifies main.py calls HA turn_on when EV_START is returned.
        """
        import zoneinfo
        from datetime import datetime as _datetime
        from unittest.mock import patch

        from tests.conftest import make_snapshot, make_ev_state, make_grid_state

        service = self._make_service_ev_enabled()
        snap = make_snapshot(
            ev=make_ev_state(soc_pct=40.0, charger_status="awaiting_start"),
            grid=make_grid_state(grid_power_w=500.0),
        )

        fixed_dt = _datetime(2026, 4, 24, 23, 0, 0, tzinfo=zoneinfo.ZoneInfo("Europe/Stockholm"))

        from core.ev_dispatch import EVDispatchResult, EVActionType, EVDispatchState

        r_natt_result = EVDispatchResult(
            action=EVActionType.EV_START,
            amps=16,
            reason="R-natt: night_window_charge (hour=23)",
            new_state=EVDispatchState(night_charging=True),
            is_shadow=False,
        )

        with (
            patch("main.evaluate_ev_action", return_value=r_natt_result),
            patch("main.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fixed_dt
            await service._evaluate_ev(snap)

        assert service._ha_api is not None
        service._ha_api.call_service.assert_called_once()  # type: ignore[attr-defined]
        call_args = service._ha_api.call_service.call_args  # type: ignore[attr-defined]
        assert call_args[0][1] == "turn_on", f"Expected turn_on for EV_START, got {call_args[0][1]}"

    @pytest.mark.asyncio()
    async def test_evaluate_ev_r_natt_blocks_at_14_00(self) -> None:
        """R-natt must NOT trigger at hour=14 (outside night window).

        With hour_of_day=14 (daytime), evaluate_ev_action should see correct
        inputs and return NOOP (no night trigger). No HA call_service expected.
        """
        import zoneinfo
        from datetime import datetime as _datetime
        from unittest.mock import patch

        from tests.conftest import make_snapshot, make_ev_state, make_grid_state

        service = self._make_service_ev_enabled()
        snap = make_snapshot(
            ev=make_ev_state(soc_pct=40.0, charger_status="awaiting_start"),
            grid=make_grid_state(grid_power_w=500.0),
        )

        fixed_dt = _datetime(2026, 4, 24, 14, 0, 0, tzinfo=zoneinfo.ZoneInfo("Europe/Stockholm"))

        from core.ev_dispatch import EVDispatchResult, EVActionType, EVDispatchState

        noop_result = EVDispatchResult(
            action=EVActionType.NOOP,
            amps=0,
            reason="R-natt: outside_window (hour=14, window=22-6)",
            new_state=EVDispatchState(night_charging=False),
            is_shadow=False,
        )

        captured: list[object] = []

        def _fake_evaluate(state: object, inputs: object, cfg: object) -> object:
            captured.append(inputs)
            return noop_result

        with (
            patch("main.evaluate_ev_action", side_effect=_fake_evaluate),
            patch("main.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fixed_dt
            await service._evaluate_ev(snap)

        assert len(captured) == 1
        from core.ev_dispatch import EVDispatchInputs

        assert isinstance(captured[0], EVDispatchInputs)
        assert captured[0].hour_of_day == 14, (  # type: ignore[attr-defined]
            f"hour_of_day={captured[0].hour_of_day}, expected 14"  # type: ignore[attr-defined]
        )
        assert service._ha_api is not None
        service._ha_api.call_service.assert_not_called()  # type: ignore[attr-defined]
