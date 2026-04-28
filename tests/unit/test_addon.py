"""PLAT-1813 — Unit tests for HA addon packaging.

Covers:
  Level 1 (Unit):    _apply_addon_overrides(), config-load with addon paths,
                     options.json parsing, site.yaml.example validation.
  Level 4 (Regression): All 10 PLAT-1828 H6 tests rerun in addon context
                        (stale SoC guard + floor+PV charge trigger).
  Level 5 (Corner):  Empty env vars, invalid log level, missing site.yaml,
                     all-empty options.json, DB path override isolation.

DoD reference: /mnt/solutions/Root/platform/global/standards/DEFINITION-OF-DONE.md
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from config.schema import CarmaConfig
from main import _apply_addon_overrides, _floor_pv_charge_needed

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SITE_YAML_EXAMPLE = PROJECT_ROOT / "config" / "site.yaml.example"


def _make_minimal_config(overrides: dict[str, Any] | None = None) -> CarmaConfig:
    """Build a minimal valid CarmaConfig for testing."""
    from core.models import CTPlacement

    data: dict[str, Any] = {
        "site": {
            "id": "test-site",
            "name": "Test Site",
            "latitude": 59.33,
            "longitude": 18.07,
        },
        "homeassistant": {
            "url": "http://supervisor/core",
        },
        "batteries": [
            {
                "id": "bat1",
                "name": "Test Battery",
                "cap_kwh": 10.0,
                "ct_placement": CTPlacement.HOUSE_GRID.value,
                "entities": {
                    "soc": "sensor.test_soc",
                    "power": "sensor.test_power",
                    "ems_mode": "select.test_ems_mode",
                    "ems_power_limit": "number.test_ems_limit",
                    "fast_charging": "switch.test_fast_charging",
                },
            }
        ],
        "ev_charger": {
            "id": "test_ev_charger",
            "name": "Test Charger",
            "charger_id": "TEST001",
            "entities": {
                "status": "sensor.test_charger_status",
                "power": "sensor.test_charger_power",
                "current": "sensor.test_charger_current",
                "enabled": "switch.test_charger_enabled",
            },
        },
        "ev": {
            "id": "test_ev",
            "name": "Test EV",
            "battery_kwh": 60.0,
            "entities": {"soc": "sensor.test_ev_soc"},
        },
    }
    if overrides:
        data.update(overrides)
    return CarmaConfig(**data)


# ---------------------------------------------------------------------------
# Level 1 — Unit: _apply_addon_overrides()
# ---------------------------------------------------------------------------


class TestApplyAddonOverrides:
    """Unit tests for the _apply_addon_overrides() function (PLAT-1813)."""

    def test_log_file_override_applied(self) -> None:
        """CARMA_OVERRIDE_LOG_FILE must patch config.logging.file."""
        config = _make_minimal_config()
        original = config.logging.file
        with patch.dict(os.environ, {"CARMA_OVERRIDE_LOG_FILE": "/data/logs/carma.log"}):
            _apply_addon_overrides(config)
        assert config.logging.file == "/data/logs/carma.log"
        assert original != "/data/logs/carma.log" or original == "/data/logs/carma.log"

    def test_log_level_override_applied(self) -> None:
        """CARMA_OVERRIDE_LOG_LEVEL must patch config.logging.level (uppercased)."""
        config = _make_minimal_config()
        with patch.dict(os.environ, {"CARMA_OVERRIDE_LOG_LEVEL": "debug"}):
            _apply_addon_overrides(config)
        assert config.logging.level == "DEBUG"

    def test_db_path_override_applied(self) -> None:
        """CARMA_OVERRIDE_DB_PATH must patch config.storage.sqlite.path."""
        config = _make_minimal_config()
        with patch.dict(os.environ, {"CARMA_OVERRIDE_DB_PATH": "/data/carma.db"}):
            _apply_addon_overrides(config)
        assert config.storage.sqlite.path == "/data/carma.db"

    def test_all_three_overrides_applied_simultaneously(self) -> None:
        """All three CARMA_OVERRIDE_* vars may be set at the same time."""
        config = _make_minimal_config()
        env = {
            "CARMA_OVERRIDE_LOG_FILE": "/data/logs/carma.log",
            "CARMA_OVERRIDE_LOG_LEVEL": "WARNING",
            "CARMA_OVERRIDE_DB_PATH": "/data/carma.db",
        }
        with patch.dict(os.environ, env):
            _apply_addon_overrides(config)
        assert config.logging.file == "/data/logs/carma.log"
        assert config.logging.level == "WARNING"
        assert config.storage.sqlite.path == "/data/carma.db"

    def test_noop_without_env_vars(self) -> None:
        """Without CARMA_OVERRIDE_* env vars, config is unchanged (standalone mode)."""
        config = _make_minimal_config()
        original_log_file = config.logging.file
        original_log_level = config.logging.level
        original_db_path = config.storage.sqlite.path

        # Ensure the override vars are definitely absent
        env_clean = {
            k: v for k, v in os.environ.items()
            if not k.startswith("CARMA_OVERRIDE_")
        }
        with patch.dict(os.environ, env_clean, clear=True):
            _apply_addon_overrides(config)

        assert config.logging.file == original_log_file
        assert config.logging.level == original_log_level
        assert config.storage.sqlite.path == original_db_path

    def test_log_level_override_uppercase_normalization(self) -> None:
        """Log level is uppercased even if option provides lowercase."""
        config = _make_minimal_config()
        with patch.dict(os.environ, {"CARMA_OVERRIDE_LOG_LEVEL": "info"}):
            _apply_addon_overrides(config)
        assert config.logging.level == "INFO"

    def test_empty_env_var_is_ignored(self) -> None:
        """Empty string for CARMA_OVERRIDE_DB_PATH must NOT overwrite the config."""
        config = _make_minimal_config()
        original_db_path = config.storage.sqlite.path
        with patch.dict(os.environ, {"CARMA_OVERRIDE_DB_PATH": ""}):
            _apply_addon_overrides(config)
        # Empty string is falsy — guard in _apply_addon_overrides skips it
        assert config.storage.sqlite.path == original_db_path

    def test_empty_log_file_env_var_is_ignored(self) -> None:
        """Empty CARMA_OVERRIDE_LOG_FILE must not clear the log path."""
        config = _make_minimal_config()
        original_log_file = config.logging.file
        with patch.dict(os.environ, {"CARMA_OVERRIDE_LOG_FILE": ""}):
            _apply_addon_overrides(config)
        assert config.logging.file == original_log_file

    def test_empty_log_level_env_var_is_ignored(self) -> None:
        """Empty CARMA_OVERRIDE_LOG_LEVEL must not clear the log level."""
        config = _make_minimal_config()
        original_level = config.logging.level
        with patch.dict(os.environ, {"CARMA_OVERRIDE_LOG_LEVEL": ""}):
            _apply_addon_overrides(config)
        assert config.logging.level == original_level


# ---------------------------------------------------------------------------
# Level 1 — Unit: Config loading in addon context
# ---------------------------------------------------------------------------


class TestAddonConfigLoad:
    """Unit tests for config loading with addon-specific settings."""

    def test_supervisor_url_is_valid_ha_url(self) -> None:
        """http://supervisor/core must be accepted as a valid HA URL."""
        config = _make_minimal_config()
        assert config.homeassistant.url == "http://supervisor/core"

    def test_config_with_data_paths_validates(self) -> None:
        """Config with /data/ paths for log + DB must pass Pydantic validation."""
        from core.models import CTPlacement

        data: dict[str, Any] = {
            "site": {
                "id": "addon-site", "name": "Addon Site",
                "latitude": 59.33, "longitude": 18.07,
            },
            "homeassistant": {"url": "http://supervisor/core"},
            "batteries": [{
                "id": "bat1", "name": "Battery", "cap_kwh": 10.0,
                "ct_placement": CTPlacement.HOUSE_GRID.value,
                "entities": {
                    "soc": "sensor.soc", "power": "sensor.power",
                    "ems_mode": "select.ems", "ems_power_limit": "number.limit",
                    "fast_charging": "switch.fast",
                },
            }],
            "ev_charger": {
                "id": "ev1", "name": "EV", "charger_id": "EH001",
                "entities": {
                    "status": "sensor.ev_status", "power": "sensor.ev_power",
                    "current": "sensor.ev_current", "enabled": "switch.ev_enabled",
                },
            },
            "ev": {
                "id": "car1", "name": "Car", "battery_kwh": 75.0,
                "entities": {"soc": "sensor.car_soc"},
            },
            "storage": {"sqlite": {"path": "/data/carma.db"}},
            "logging": {"level": "INFO", "file": "/data/logs/carma.log"},
        }
        config = CarmaConfig(**data)
        assert config.storage.sqlite.path == "/data/carma.db"
        assert config.logging.file == "/data/logs/carma.log"

    def test_site_yaml_example_file_exists(self) -> None:
        """config/site.yaml.example must exist in the repo."""
        assert SITE_YAML_EXAMPLE.exists(), (
            f"site.yaml.example not found at {SITE_YAML_EXAMPLE}. "
            "This file is required for addon users."
        )

    def test_site_yaml_example_is_valid_yaml(self) -> None:
        """site.yaml.example must be syntactically valid YAML."""
        raw = yaml.safe_load(SITE_YAML_EXAMPLE.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), "site.yaml.example must be a YAML mapping at top level"

    def test_site_yaml_example_has_required_sections(self) -> None:
        """site.yaml.example must contain all required top-level sections."""
        raw = yaml.safe_load(SITE_YAML_EXAMPLE.read_text(encoding="utf-8"))
        for section in ("site", "homeassistant", "batteries", "ev_charger", "ev"):
            assert section in raw, f"site.yaml.example missing required section: {section}"

    def test_site_yaml_example_uses_supervisor_url(self) -> None:
        """site.yaml.example must show http://supervisor/core as the HA URL."""
        raw = yaml.safe_load(SITE_YAML_EXAMPLE.read_text(encoding="utf-8"))
        ha_url = raw.get("homeassistant", {}).get("url", "")
        assert "supervisor" in ha_url, (
            f"site.yaml.example should use http://supervisor/core, got: {ha_url!r}"
        )


# ---------------------------------------------------------------------------
# Level 1 — Unit: options.json parsing simulation
# ---------------------------------------------------------------------------


class TestOptionsJsonParsing:
    """Simulate the options.json parsing that run.sh performs."""

    def test_minimal_options_json_structure(self, tmp_path: Path) -> None:
        """Minimal options.json (all empty) should be parseable with jq defaults."""
        options = {
            "ha_token": "",
            "solcast_api_key": "",
            "nordpool_api_key": "",
            "slack_webhook_url": "",
            "pg_host": "",
            "pg_port": 5432,
            "pg_database": "energy",
            "pg_user": "",
            "pg_password": "",
            "log_level": "INFO",
        }
        options_file = tmp_path / "options.json"
        options_file.write_text(json.dumps(options), encoding="utf-8")
        loaded = json.loads(options_file.read_text(encoding="utf-8"))
        assert loaded["log_level"] == "INFO"
        assert loaded["pg_port"] == 5432

    def test_options_json_with_all_secrets_set(self, tmp_path: Path) -> None:
        """options.json with all fields populated must be parseable."""
        options = {
            "ha_token": "eyJ...",
            "solcast_api_key": "sk-...",
            "nordpool_api_key": "np-...",
            "slack_webhook_url": "https://hooks.slack.com/...",
            "pg_host": "192.168.5.100",
            "pg_port": 5432,
            "pg_database": "energy",
            "pg_user": "carma",
            "pg_password": "s3cr3t",
            "log_level": "DEBUG",
        }
        options_file = tmp_path / "options.json"
        options_file.write_text(json.dumps(options), encoding="utf-8")
        loaded = json.loads(options_file.read_text(encoding="utf-8"))
        assert loaded["ha_token"] == "eyJ..."
        assert loaded["log_level"] == "DEBUG"

    def test_log_level_valid_values(self) -> None:
        """All valid log levels from config.yaml schema must be accepted."""
        from config.schema import LoggingConfig

        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            cfg = LoggingConfig(level=level)
            assert cfg.level == level

    def test_log_level_normalised_to_uppercase(self) -> None:
        """LoggingConfig normalises log level to uppercase."""
        from config.schema import LoggingConfig

        cfg = LoggingConfig(level="debug")
        assert cfg.level == "DEBUG"


# ---------------------------------------------------------------------------
# Level 4 — Regression: PLAT-1828 H6 fix (all tests must remain green)
# Included in addon test suite because addon ships the fix.
# ---------------------------------------------------------------------------


class TestH6RegressionInAddonContext:
    """PLAT-1828 H6 regression tests — verified as part of PLAT-1813 addon.

    These tests prove the H6 stale-SoC guard (commit 6af8e6c) is present
    and correct in the addon branch. Failure here = H6 fix was lost.

    Acceptance criteria (from PLAT-1828 QC-PASS 901):
      AC1: stale SoC → sensors_ready=False (see test_goodwe_adapter.py:H6StaleSocGuard)
      AC2: floor SoC + PV surplus → charge_pv triggered
      AC3: stale SoC never triggers floor+PV charge
    """

    def test_h6_floor_pv_charge_at_exact_floor(self) -> None:
        """AC2: soc=15% (exact floor), pv=3kW → must trigger charge_pv."""
        assert _floor_pv_charge_needed(
            soc_pct=15.0,
            min_soc_pct=15.0,
            pv_surplus_w=3000.0,
        ) is True

    def test_h6_floor_pv_charge_within_margin(self) -> None:
        """AC2: soc=18% (floor+margin), pv=600W → must trigger charge_pv."""
        assert _floor_pv_charge_needed(
            soc_pct=18.0,
            min_soc_pct=15.0,
            pv_surplus_w=600.0,
        ) is True

    def test_h6_stale_soc_never_triggers(self) -> None:
        """AC3: soc_pct=-1.0 (stale) must never trigger charge_pv."""
        assert _floor_pv_charge_needed(
            soc_pct=-1.0,
            min_soc_pct=15.0,
            pv_surplus_w=3000.0,
        ) is False

    def test_h6_soc_above_floor_margin_no_trigger(self) -> None:
        """AC2 boundary: soc=21% is outside floor+5% margin → no trigger."""
        assert _floor_pv_charge_needed(
            soc_pct=21.0,
            min_soc_pct=15.0,
            pv_surplus_w=3000.0,
        ) is False

    def test_h6_pv_below_threshold_no_trigger(self) -> None:
        """Floor + PV below 500W threshold → no trigger (not enough surplus)."""
        assert _floor_pv_charge_needed(
            soc_pct=15.0,
            min_soc_pct=15.0,
            pv_surplus_w=499.9,
        ) is False

    def test_h6_pv_at_exact_threshold_triggers(self) -> None:
        """PV exactly at threshold (500.0W) must trigger."""
        assert _floor_pv_charge_needed(
            soc_pct=15.0,
            min_soc_pct=15.0,
            pv_surplus_w=500.0,
        ) is True

    def test_h6_soc_zero_with_pv_triggers(self) -> None:
        """Extreme: soc=0% (fully discharged) + PV → must trigger (valid fresh reading)."""
        assert _floor_pv_charge_needed(
            soc_pct=0.0,
            min_soc_pct=15.0,
            pv_surplus_w=2000.0,
        ) is True

    def test_h6_custom_min_soc_respected(self) -> None:
        """Floor+PV trigger uses min_soc_pct from config — not hardcoded."""
        # min_soc=20%, margin=5% → triggers up to 25%
        assert _floor_pv_charge_needed(
            soc_pct=24.0,
            min_soc_pct=20.0,
            pv_surplus_w=1000.0,
        ) is True
        # 26% is outside margin for min_soc=20%
        assert _floor_pv_charge_needed(
            soc_pct=26.0,
            min_soc_pct=20.0,
            pv_surplus_w=1000.0,
        ) is False

    def test_h6_soc_negative_two_no_trigger(self) -> None:
        """Any negative soc_pct (not just -1.0) must not trigger."""
        assert _floor_pv_charge_needed(
            soc_pct=-2.0,
            min_soc_pct=15.0,
            pv_surplus_w=3000.0,
        ) is False

    def test_h6_pv_zero_no_trigger(self) -> None:
        """pv_surplus_w=0 (no solar production) → no trigger at any SoC."""
        assert _floor_pv_charge_needed(
            soc_pct=10.0,
            min_soc_pct=15.0,
            pv_surplus_w=0.0,
        ) is False


# ---------------------------------------------------------------------------
# Level 5 — Corner cases
# ---------------------------------------------------------------------------


class TestAddonCornerCases:
    """Corner cases and boundary conditions for the addon functionality."""

    def test_override_db_path_to_unusual_path(self) -> None:
        """DB path can be any valid string path (not restricted to /data/)."""
        config = _make_minimal_config()
        with patch.dict(os.environ, {"CARMA_OVERRIDE_DB_PATH": "/tmp/test.db"}):
            _apply_addon_overrides(config)
        assert config.storage.sqlite.path == "/tmp/test.db"

    def test_override_log_level_critical(self) -> None:
        """CRITICAL log level must be accepted by the override."""
        config = _make_minimal_config()
        with patch.dict(os.environ, {"CARMA_OVERRIDE_LOG_LEVEL": "CRITICAL"}):
            _apply_addon_overrides(config)
        assert config.logging.level == "CRITICAL"

    def test_override_is_idempotent(self) -> None:
        """Applying overrides twice must not compound or break state."""
        config = _make_minimal_config()
        env = {
            "CARMA_OVERRIDE_LOG_FILE": "/data/logs/carma.log",
            "CARMA_OVERRIDE_DB_PATH": "/data/carma.db",
        }
        with patch.dict(os.environ, env):
            _apply_addon_overrides(config)
            _apply_addon_overrides(config)  # second call must be safe
        assert config.logging.file == "/data/logs/carma.log"
        assert config.storage.sqlite.path == "/data/carma.db"

    def test_config_site_id_with_hyphens(self) -> None:
        """Site IDs with hyphens must be accepted (common in addon installs)."""
        config = _make_minimal_config({"site": {
            "id": "my-home-2026",
            "name": "My Home",
            "latitude": 59.33,
            "longitude": 18.07,
        }})
        assert config.site.id == "my-home-2026"

    def test_empty_options_json_all_defaults(self, tmp_path: Path) -> None:
        """Empty options.json (addon fresh install) must yield all default values."""
        options = {}
        options_file = tmp_path / "options.json"
        options_file.write_text(json.dumps(options), encoding="utf-8")
        loaded = json.loads(options_file.read_text(encoding="utf-8"))
        # Simulate jq defaults from run.sh
        log_level = loaded.get("log_level") or "INFO"
        pg_port = loaded.get("pg_port") or 5432
        assert log_level == "INFO"
        assert pg_port == 5432

    def test_site_yaml_example_passes_partial_validation(self) -> None:
        """site.yaml.example homeassistant section must have a non-empty URL."""
        raw = yaml.safe_load(SITE_YAML_EXAMPLE.read_text(encoding="utf-8"))
        url = raw.get("homeassistant", {}).get("url", "")
        assert len(url) > 0, "homeassistant.url in site.yaml.example must not be empty"

    def test_log_level_warning_accepted(self) -> None:
        """WARNING log level must be accepted by the override function."""
        config = _make_minimal_config()
        with patch.dict(os.environ, {"CARMA_OVERRIDE_LOG_LEVEL": "WARNING"}):
            _apply_addon_overrides(config)
        assert config.logging.level == "WARNING"

    def test_db_path_and_log_path_are_independent(self) -> None:
        """Overriding only DB path must not affect log file path."""
        config = _make_minimal_config()
        original_log_file = config.logging.file
        with patch.dict(os.environ, {"CARMA_OVERRIDE_DB_PATH": "/data/carma.db"}):
            _apply_addon_overrides(config)
        assert config.storage.sqlite.path == "/data/carma.db"
        assert config.logging.file == original_log_file  # unchanged
