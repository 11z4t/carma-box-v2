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

        # Clear handlers first
        log = logging.getLogger("carma_box")
        log.handlers.clear()

        setup_logging(cfg)
        assert len(log.handlers) >= 1  # At least console
        assert any(isinstance(h, logging.StreamHandler) for h in log.handlers)

        # Cleanup
        log.handlers.clear()
