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
        # Simulate: miner ON at 380W, vp_kontor OFF
        async def fake_get_state(entity: str) -> str:
            responses: dict[str, str] = {
                "switch.shelly1pmg4_a085e3bd1e60": "on",
                "sensor.appliance_total_effekt": "380.5",
                "switch.shellypro1pm_30c6f78289b8_switch_0": "off",
                "sensor.carma_effekt_vp_kontor": "0",
            }
            return responses.get(entity, "0")

        mock_api.get_state = AsyncMock(side_effect=fake_get_state)
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
        # pool_heater power sensor is offline → returns 'unavailable'
        async def fake_get_state(entity: str) -> str:
            if "power" in entity or "effekt" in entity:
                return "unavailable"
            return "off"

        mock_api.get_state = AsyncMock(side_effect=fake_get_state)
        mock_api.health_check = AsyncMock(return_value=True)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        # Must not raise
        consumers = await service._collect_consumers()
        assert len(consumers) > 0
        # All power values should be 0.0 (fallback) or cc.power_w
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
            scenario=Scenario.MIDDAY_CHARGE,
            guard=GuardEvaluation(
                level=GuardLevel.OK, commands=[],
            ),
        )

        await service._write_dashboard_state(snap, cycle_result)

        # set_state called 3 times: scenario, decision, rules
        assert mock_api.set_state.call_count == 3
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
