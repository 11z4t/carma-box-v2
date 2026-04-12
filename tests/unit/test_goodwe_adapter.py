"""Tests for the GoodWe ET inverter adapter.

Tests verify:
- All read methods with mock HA states (success + unavailable)
- Write methods: correct service domain/service/data
- Truthy-trap defense: set_ems_power_limit(0) sends value=0
- INV-3/B7: set_ems_mode does NOT touch fast_charging
- B10/B14: mode "auto" is FORBIDDEN
- Batch reading via get_all_readings
- Properties from config
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from adapters.goodwe import GoodWeAdapter
from adapters.ha_api import HAApiClient
from config.schema import BatteryConfig

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_battery_config(**overrides: Any) -> BatteryConfig:
    """Create a BatteryConfig with real Sanduddsvagen 60 entity patterns."""
    defaults: dict[str, Any] = {
        "id": "kontor",
        "name": "Kontor",
        "type": "goodwe_et",
        "cap_kwh": 15.0,
        "ct_placement": "local_load",
        "goodwe_device_id": "696f2a85fed59b45f2ced7fc2663984a",
        "entities": {
            "soc": "sensor.goodwe_battery_state_of_charge_kontor",
            "power": "sensor.goodwe_battery_power_kontor",
            "cell_temp": "sensor.goodwe_battery_temperature_kontor",
            "pv_power": "sensor.pv_power",
            "grid_power": "sensor.house_grid_power",
            "load_power": "sensor.back_up_load",
            "ems_mode": "select.goodwe_kontor_ems_mode",
            "ems_power_limit": "number.goodwe_kontor_ems_power_limit",
            "fast_charging": "switch.goodwe_fast_charging_switch_kontor",
            "soh": "sensor.battery_state_of_health",
        },
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


@pytest.fixture()
def mock_api() -> AsyncMock:
    """Create a mock HAApiClient."""
    api = AsyncMock(spec=HAApiClient)
    api.get_state = AsyncMock(return_value=None)
    api.get_states_batch = AsyncMock(return_value={})
    api.call_service = AsyncMock(return_value=True)
    return api


@pytest.fixture()
def kontor_config() -> BatteryConfig:
    return _make_battery_config()


@pytest.fixture()
def forrad_config() -> BatteryConfig:
    return _make_battery_config(
        id="forrad",
        name="Forrad",
        cap_kwh=5.0,
        ct_placement="house_grid",
        goodwe_device_id="e087f4789d3713e9b18f1ff27d4e7cb9",
        entities={
            "soc": "sensor.goodwe_battery_state_of_charge_forrad",
            "power": "sensor.goodwe_battery_power_forrad",
            "cell_temp": "sensor.goodwe_battery_temperature_forrad",
            "pv_power": "sensor.pv_power_2",
            "grid_power": "sensor.house_grid_power",
            "load_power": "sensor.back_up_load_2",
            "ems_mode": "select.goodwe_forrad_ems_mode",
            "ems_power_limit": "number.goodwe_forrad_ems_power_limit",
            "fast_charging": "switch.goodwe_fast_charging_switch_forrad",
            "soh": "sensor.battery_state_of_health_2",
        },
    )


@pytest.fixture()
def adapter(mock_api: AsyncMock, kontor_config: BatteryConfig) -> GoodWeAdapter:
    return GoodWeAdapter(mock_api, kontor_config)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    """Test that properties come from config, not hardcoded."""

    def test_battery_id(self, adapter: GoodWeAdapter) -> None:
        assert adapter.battery_id == "kontor"

    def test_capacity_kwh(self, adapter: GoodWeAdapter) -> None:
        assert adapter.capacity_kwh == 15.0

    def test_ct_placement(self, adapter: GoodWeAdapter) -> None:
        assert adapter.ct_placement == "local_load"

    def test_device_id(self, adapter: GoodWeAdapter) -> None:
        assert adapter.device_id == "696f2a85fed59b45f2ced7fc2663984a"

    def test_forrad_properties(
        self, mock_api: AsyncMock, forrad_config: BatteryConfig
    ) -> None:
        adapter = GoodWeAdapter(mock_api, forrad_config)
        assert adapter.battery_id == "forrad"
        assert adapter.capacity_kwh == 5.0
        assert adapter.ct_placement == "house_grid"


# ---------------------------------------------------------------------------
# Read methods — success
# ---------------------------------------------------------------------------


class TestReadSuccess:
    """Test read methods with valid HA sensor states."""

    async def test_get_battery_soc(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "72.5"
        result = await adapter.get_battery_soc()
        assert result == 72.5
        mock_api.get_state.assert_awaited_with(
            "sensor.goodwe_battery_state_of_charge_kontor"
        )

    async def test_get_battery_power_discharge(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "1500"
        result = await adapter.get_battery_power()
        assert result == 1500.0

    async def test_get_battery_power_charge(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "-2000"
        result = await adapter.get_battery_power()
        assert result == -2000.0

    async def test_get_cell_temperature(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "14.8"
        result = await adapter.get_cell_temperature()
        assert result == 14.8

    async def test_get_pv_power(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "4500"
        result = await adapter.get_pv_power()
        assert result == 4500.0

    async def test_get_pv_power_clamps_negative_to_zero(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        """PV power should never be negative."""
        mock_api.get_state.return_value = "-5"
        result = await adapter.get_pv_power()
        assert result == 0.0

    async def test_get_grid_power_import(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "800"
        result = await adapter.get_grid_power()
        assert result == 800.0

    async def test_get_grid_power_export(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "-200"
        result = await adapter.get_grid_power()
        assert result == -200.0

    async def test_get_load_power(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "1413"
        result = await adapter.get_load_power()
        assert result == 1413.0

    async def test_get_load_power_clamps_negative(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        """Load power should never be negative."""
        mock_api.get_state.return_value = "-10"
        result = await adapter.get_load_power()
        assert result == 0.0

    async def test_get_ems_mode(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "charge_pv"
        result = await adapter.get_ems_mode()
        assert result == "charge_pv"

    async def test_get_ems_power_limit(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "3000"
        result = await adapter.get_ems_power_limit()
        assert result == 3000

    async def test_get_fast_charging_on(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "on"
        result = await adapter.get_fast_charging()
        assert result is True

    async def test_get_fast_charging_off(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "off"
        result = await adapter.get_fast_charging()
        assert result is False

    async def test_get_soh(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "98"
        result = await adapter.get_soh()
        assert result == 98.0


# ---------------------------------------------------------------------------
# Read methods — unavailable / failure
# ---------------------------------------------------------------------------


class TestReadUnavailable:
    """Test read methods return safe defaults when sensors are unavailable."""

    async def test_soc_unavailable_returns_zero(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = None
        result = await adapter.get_battery_soc()
        assert result == 0.0

    async def test_cell_temp_unavailable_returns_safe_default(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = None
        result = await adapter.get_cell_temperature()
        assert result == 20.0  # Safe default, not triggering cold logic

    async def test_soh_unavailable_returns_100(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = None
        result = await adapter.get_soh()
        assert result == 100.0  # Optimistic default

    async def test_ems_mode_unavailable_returns_standby(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = None
        result = await adapter.get_ems_mode()
        assert result == "battery_standby"

    async def test_unparseable_float_returns_default(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "not_a_number"
        result = await adapter.get_battery_soc()
        assert result == 0.0

    async def test_empty_entity_id_returns_default(
        self, mock_api: AsyncMock
    ) -> None:
        """When entity ID is empty, should return default without API call."""
        config = _make_battery_config(
            entities={
                "soc": "sensor.test_soc",
                "power": "sensor.test_power",
                "cell_temp": "",  # Empty = not configured
                "ems_mode": "select.test_mode",
                "ems_power_limit": "number.test_limit",
                "fast_charging": "switch.test_fc",
            }
        )
        adapter = GoodWeAdapter(mock_api, config)
        result = await adapter.get_cell_temperature()
        assert result == 20.0
        # Should NOT call the API for empty entity
        mock_api.get_state.assert_not_awaited()


# ---------------------------------------------------------------------------
# Write methods — correct service calls
# ---------------------------------------------------------------------------


class TestWriteMethods:
    """Test write methods send correct HA service calls."""

    async def test_set_ems_mode_calls_select_option(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        result = await adapter.set_ems_mode("discharge_pv")
        assert result is True
        mock_api.call_service.assert_awaited_once_with(
            "select", "select_option",
            {
                "entity_id": "select.goodwe_kontor_ems_mode",
                "option": "discharge_pv",
            },
        )

    async def test_set_ems_mode_charge_pv(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        await adapter.set_ems_mode("charge_pv")
        mock_api.call_service.assert_awaited_once_with(
            "select", "select_option",
            {
                "entity_id": "select.goodwe_kontor_ems_mode",
                "option": "charge_pv",
            },
        )

    async def test_set_ems_mode_battery_standby(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        await adapter.set_ems_mode("battery_standby")
        mock_api.call_service.assert_awaited_once_with(
            "select", "select_option",
            {
                "entity_id": "select.goodwe_kontor_ems_mode",
                "option": "battery_standby",
            },
        )

    async def test_set_fast_charging_on(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        await adapter.set_fast_charging(True)
        mock_api.call_service.assert_awaited_once_with(
            "switch", "turn_on",
            {"entity_id": "switch.goodwe_fast_charging_switch_kontor"},
        )

    async def test_set_fast_charging_off(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        await adapter.set_fast_charging(False)
        mock_api.call_service.assert_awaited_once_with(
            "switch", "turn_off",
            {"entity_id": "switch.goodwe_fast_charging_switch_kontor"},
        )


# ---------------------------------------------------------------------------
# REGRESSION: B9 — truthy-trap defense
# ---------------------------------------------------------------------------


class TestTruthyTrap:
    """Regression tests for ems_power_limit truthy-trap (B9).

    The GoodWe firmware/HACS integration may skip writing 0 if the
    value is falsy. Our adapter MUST explicitly send 0.
    """

    async def test_set_ems_power_limit_zero_sends_zero(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        """CRITICAL: value=0 must be explicitly sent, not skipped."""
        result = await adapter.set_ems_power_limit(0)
        assert result is True
        mock_api.call_service.assert_awaited_once_with(
            "number", "set_value",
            {
                "entity_id": "number.goodwe_kontor_ems_power_limit",
                "value": 0,  # MUST be 0, not missing/None
            },
        )

    async def test_set_ems_power_limit_nonzero(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        await adapter.set_ems_power_limit(3000)
        mock_api.call_service.assert_awaited_once_with(
            "number", "set_value",
            {
                "entity_id": "number.goodwe_kontor_ems_power_limit",
                "value": 3000,
            },
        )

    async def test_set_ems_power_limit_uses_number_service(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        """B9: Must use number.set_value, NOT goodwe.set_parameter."""
        await adapter.set_ems_power_limit(1500)
        call_args = mock_api.call_service.call_args
        assert call_args[0][0] == "number"  # domain
        assert call_args[0][1] == "set_value"  # service


# ---------------------------------------------------------------------------
# REGRESSION: B7/INV-3 — set_ems_mode must NOT touch fast_charging
# ---------------------------------------------------------------------------


class TestInv3Regression:
    """Regression tests for INV-3 (B7): set_ems_mode independence.

    set_ems_mode MUST NOT call any switch service or touch fast_charging.
    The caller (decision engine/guards) is responsible for fast_charging.
    """

    async def test_set_ems_mode_does_not_call_switch_service(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        """set_ems_mode should ONLY call select.select_option."""
        await adapter.set_ems_mode("discharge_pv")

        # Exactly one call
        assert mock_api.call_service.await_count == 1

        # That call must be to select, not switch
        call_args = mock_api.call_service.call_args
        assert call_args[0][0] == "select"
        assert call_args[0][1] == "select_option"

    async def test_discharge_pv_does_not_modify_fast_charging(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        """Even when setting discharge_pv, fast_charging is untouched."""
        await adapter.set_ems_mode("discharge_pv")

        # Verify no switch.turn_on or switch.turn_off was called
        for call in mock_api.call_service.call_args_list:
            domain = call[0][0]
            assert domain != "switch", (
                "set_ems_mode must NOT call switch services (INV-3/B7)"
            )


# ---------------------------------------------------------------------------
# REGRESSION: B10/B14 — mode "auto" is FORBIDDEN
# ---------------------------------------------------------------------------


class TestForbiddenModes:
    """Regression tests for B10/B14: auto mode prohibition."""

    async def test_auto_mode_is_rejected(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        """Mode 'auto' must be refused — GoodWe makes uncontrolled decisions."""
        result = await adapter.set_ems_mode("auto")
        assert result is False
        mock_api.call_service.assert_not_awaited()

    async def test_unknown_mode_is_rejected(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        result = await adapter.set_ems_mode("turbo_mode")
        assert result is False
        mock_api.call_service.assert_not_awaited()

    async def test_valid_modes_are_accepted(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        """All valid non-forbidden modes should work."""
        valid_modes = [
            "charge_pv", "discharge_pv", "battery_standby",
            "import_ac", "export_ac", "conserve",
        ]
        for mode in valid_modes:
            mock_api.call_service.reset_mock()
            result = await adapter.set_ems_mode(mode)
            assert result is True, f"Mode '{mode}' should be accepted"


# ---------------------------------------------------------------------------
# Batch reading
# ---------------------------------------------------------------------------


class TestBatchReading:
    """Test get_all_readings batch API call."""

    async def test_returns_readings_for_available_entities(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_states_batch.return_value = {
            "sensor.goodwe_battery_state_of_charge_kontor": {
                "entity_id": "sensor.goodwe_battery_state_of_charge_kontor",
                "state": "65",
                "attributes": {"unit_of_measurement": "%"},
            },
            "sensor.goodwe_battery_power_kontor": {
                "entity_id": "sensor.goodwe_battery_power_kontor",
                "state": "1200",
                "attributes": {"unit_of_measurement": "W"},
            },
        }

        readings = await adapter.get_all_readings()
        assert len(readings) == 2
        assert readings[0].entity_id == "sensor.goodwe_battery_state_of_charge_kontor"
        assert readings[0].value == "65"
        assert readings[0].unit == "%"

    async def test_excludes_unavailable_entities(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_states_batch.return_value = {
            "sensor.goodwe_battery_state_of_charge_kontor": {
                "state": "65",
                "attributes": {},
            },
            "sensor.goodwe_battery_temperature_kontor": {
                "state": "unavailable",
                "attributes": {},
            },
        }

        readings = await adapter.get_all_readings()
        assert len(readings) == 1  # Temperature excluded

    async def test_empty_batch_on_api_failure(
        self, adapter: GoodWeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_states_batch.return_value = {}
        readings = await adapter.get_all_readings()
        assert readings == []
