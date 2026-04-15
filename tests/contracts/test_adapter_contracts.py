"""Adapter contract tests (PLAT-1594).

T1: Battery adapter timeout → safe defaults
T2: EV adapter rejection → False (no crash)
T3: Partial availability → safe defaults for unavailable entities
T4: All adapters implement full interface
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from adapters.base import EVChargerAdapter, InverterAdapter
from adapters.easee import EaseeAdapter
from adapters.goodwe import GoodWeAdapter
from adapters.service_map import EVChargerServiceMap, InverterServiceMap
from config.schema import (
    BatteryConfig,
    BatteryEntities,
    EVChargerConfig,
    EVChargerEntities,
)
from core.models import CTPlacement


# ---------------------------------------------------------------------------
# Named test constants
# ---------------------------------------------------------------------------
_SAFE_DEFAULT_SOC_PCT: float = 0.0        # Safe default for unavailable SoC
_SAFE_DEFAULT_TEMP_C: float = 20.0        # Safe default for unavailable temp
_SAFE_DEFAULT_SOH_PCT: float = 100.0      # Safe default for unavailable SoH
_TEST_EV_CURRENT_A: int = 10              # Test fixture EV current
_TEST_CAP_KWH: float = 15.0              # Test battery capacity


def _make_bat_config() -> BatteryConfig:
    """Create minimal BatteryConfig for testing."""
    return BatteryConfig(
        id="test_bat",
        name="Test Battery",
        cap_kwh=_TEST_CAP_KWH,
        ct_placement=CTPlacement.LOCAL_LOAD,
        entities=BatteryEntities(
            soc="sensor.test_soc",
            power="sensor.test_power",
            cell_temp="sensor.test_temp",
            pv_power="sensor.test_pv",
            grid_power="sensor.test_grid",
            load_power="sensor.test_load",
            ems_mode="sensor.test_ems_mode",
            ems_power_limit="number.test_limit",
            fast_charging="switch.test_fast",
            soh="sensor.test_soh",
        ),
    )


def _make_ev_config() -> EVChargerConfig:
    """Create minimal EVChargerConfig for testing."""
    return EVChargerConfig(
        id="test_ev",
        name="Test Charger",
        charger_id="EH999999",
        entities=EVChargerEntities(
            status="sensor.test_status",
            power="sensor.test_power",
            current="sensor.test_current",
            enabled="switch.test_enabled",
            cable_locked="switch.test_cable",
            reason_for_no_current="sensor.test_reason",
        ),
    )


# ===========================================================================
# T1: Battery adapter timeout → safe defaults
# ===========================================================================


@pytest.mark.asyncio()
class TestBatteryAdapterContractTimeout:
    """T1: GoodWeAdapter returns safe defaults on timeout/unavailable."""

    async def test_battery_adapter_contract_timeout(self) -> None:
        ha_mock = AsyncMock()
        ha_mock.get_state = AsyncMock(return_value=None)
        ha_mock.get_states_batch = AsyncMock(return_value={})

        adapter = GoodWeAdapter(
            ha_api=ha_mock,
            config=_make_bat_config(),
            services=InverterServiceMap(),
        )

        soc = await adapter.get_battery_soc()
        assert soc == _SAFE_DEFAULT_SOC_PCT

        temp = await adapter.get_cell_temperature()
        assert temp == _SAFE_DEFAULT_TEMP_C

        soh = await adapter.get_soh()
        assert soh == _SAFE_DEFAULT_SOH_PCT


# ===========================================================================
# T2: EV adapter rejection → False (no crash)
# ===========================================================================


@pytest.mark.asyncio()
class TestEVAdapterContractRejection:
    """T2: EaseeAdapter returns False on service call failure."""

    async def test_ev_adapter_contract_rejection(self) -> None:
        ha_mock = AsyncMock()
        ha_mock.call_service = AsyncMock(return_value=False)
        ha_mock.get_state = AsyncMock(return_value=None)

        adapter = EaseeAdapter(
            ha_api=ha_mock,
            config=_make_ev_config(),
            services=EVChargerServiceMap(),
        )

        result = await adapter.set_current(_TEST_EV_CURRENT_A)
        assert result is False


# ===========================================================================
# T3: Partial availability → safe defaults
# ===========================================================================


@pytest.mark.asyncio()
class TestAdapterPartialAvailability:
    """T3: GoodWeAdapter handles partial entity availability."""

    async def test_adapter_partial_availability_contract(self) -> None:
        ha_mock = AsyncMock()
        # Some entities return None, some valid
        ha_mock.get_state = AsyncMock(return_value=None)
        ha_mock.get_states_batch = AsyncMock(return_value={
            "sensor.test_soc": {"state": "unavailable"},
            "sensor.test_power": {"state": "1500"},
            "sensor.test_temp": {"state": "unavailable"},
        })

        adapter = GoodWeAdapter(
            ha_api=ha_mock,
            config=_make_bat_config(),
            services=InverterServiceMap(),
        )

        readings = await adapter.get_all_readings()
        assert isinstance(readings, list)

        # Unavailable entities give safe defaults
        soc = await adapter.get_battery_soc()
        assert soc == _SAFE_DEFAULT_SOC_PCT


# ===========================================================================
# T4: All adapters implement full interface
# ===========================================================================


class TestAllAdaptersImplementFullInterface:
    """T4: Concrete adapters implement all abstract methods."""

    def test_all_adapters_implement_full_interface(self) -> None:
        # InverterAdapter → GoodWeAdapter
        assert issubclass(GoodWeAdapter, InverterAdapter)
        inverter_abstracts = {
            name for name, method in inspect.getmembers(InverterAdapter)
            if getattr(method, "__isabstractmethod__", False)
        }
        for method_name in inverter_abstracts:
            assert hasattr(GoodWeAdapter, method_name), (
                f"GoodWeAdapter missing {method_name}"
            )

        # EVChargerAdapter → EaseeAdapter
        assert issubclass(EaseeAdapter, EVChargerAdapter)
        ev_abstracts = {
            name for name, method in inspect.getmembers(EVChargerAdapter)
            if getattr(method, "__isabstractmethod__", False)
        }
        for method_name in ev_abstracts:
            assert hasattr(EaseeAdapter, method_name), (
                f"EaseeAdapter missing {method_name}"
            )
