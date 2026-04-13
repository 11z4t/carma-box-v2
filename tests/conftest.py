"""Shared test fixtures for CARMA Box test suite.

Provides reusable configuration objects and state factories
that all test modules can import.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from config.schema import CarmaConfig, load_config
from core.models import (
    BatteryState,
    CTPlacement,
    EVState,
    GridState,
    Scenario,
    SystemSnapshot,
)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITE_YAML = PROJECT_ROOT / "config" / "site.yaml"


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def site_yaml_path() -> Path:
    """Return the path to the production site.yaml."""
    return SITE_YAML


@pytest.fixture()
def config(site_yaml_path: Path) -> CarmaConfig:
    """Load and return the production site configuration."""
    return load_config(str(site_yaml_path))


@pytest.fixture()
def minimal_config_dict() -> dict[str, Any]:
    """Return a minimal valid configuration dictionary.

    Contains only required fields with sensible defaults for testing.
    """
    return {
        "site": {
            "id": "test-site",
            "name": "Test Site",
            "latitude": 59.427,
            "longitude": 18.005,
        },
        "homeassistant": {
            "url": "http://localhost:8123",
        },
        "batteries": [
            {
                "id": "test_bat",
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
            "entities": {
                "soc": "sensor.test_ev_soc",
            },
        },
    }


# ---------------------------------------------------------------------------
# State factories
# ---------------------------------------------------------------------------


def make_battery_state(**overrides: Any) -> BatteryState:
    """Create a BatteryState with sensible defaults.

    Args:
        **overrides: Field values to override.
    """
    defaults: dict[str, Any] = {
        "battery_id": "kontor",
        "soc_pct": 60.0,
        "power_w": 0.0,
        "cell_temp_c": 20.0,
        "pv_power_w": 0.0,
        "grid_power_w": 0.0,
        "load_power_w": 500.0,
        "ems_mode": "battery_standby",
        "ems_power_limit_w": 0,
        "fast_charging": False,
        "soh_pct": 95.0,
        "cap_kwh": 15.0,
        "ct_placement": CTPlacement.LOCAL_LOAD,
        "available_kwh": 6.075,
    }
    defaults.update(overrides)
    return BatteryState(**defaults)


def make_ev_state(**overrides: Any) -> EVState:
    """Create an EVState with sensible defaults.

    Args:
        **overrides: Field values to override.
    """
    defaults: dict[str, Any] = {
        "soc_pct": 50.0,
        "connected": False,
        "charging": False,
        "power_w": 0.0,
        "current_a": 0.0,
        "charger_status": "awaiting_start",
        "reason_for_no_current": "",
        "target_soc_pct": 75.0,
    }
    defaults.update(overrides)
    return EVState(**defaults)


def make_grid_state(**overrides: Any) -> GridState:
    """Create a GridState with sensible defaults.

    Args:
        **overrides: Field values to override.
    """
    defaults: dict[str, Any] = {
        "grid_power_w": 500.0,
        "weighted_avg_kw": 1.0,
        "current_peak_kw": 1.5,
        "dynamic_tak_kw": 2.0,
        "pv_total_w": 0.0,
        "price_ore": 50.0,
        "pv_forecast_today_kwh": 20.0,
        "pv_forecast_tomorrow_kwh": 25.0,
    }
    defaults.update(overrides)
    return GridState(**defaults)


def make_snapshot(**overrides: Any) -> SystemSnapshot:
    """Create a SystemSnapshot with sensible defaults.

    Args:
        **overrides: Field values to override.
    """
    defaults: dict[str, Any] = {
        "timestamp": datetime.now(tz=timezone.utc),
        "batteries": [make_battery_state()],
        "ev": make_ev_state(),
        "grid": make_grid_state(),
        "consumers": [],
        "current_scenario": Scenario.MIDDAY_CHARGE,
        "hour": 12,
        "minute": 0,
    }
    defaults.update(overrides)
    return SystemSnapshot(**defaults)
