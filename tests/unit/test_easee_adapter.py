"""Tests for Easee EV Charger Adapter.

Covers:
- Read methods with mock states
- set_current uses easee.set_charger_dynamic_limit with charger_id (B4)
- start/stop uses switch entity
- fix_waiting_in_fully 3-step sequence (B3)
- Properties from config
- Regression B3: waiting_in_fully fix
- Regression B4: charger_id (not device_id)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from adapters.easee import EaseeAdapter
from adapters.ha_api import HAApiClient
from config.schema import EVChargerConfig



# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config() -> EVChargerConfig:
    return EVChargerConfig(
        id="ev_main",
        name="Easee Home",
        charger_id="EH128405",
        max_amps=10,
        min_amps=6,
        phases=3,
        entities={
            "status": "sensor.easee_home_12840_status",
            "power": "sensor.easee_home_12840_power",
            "current": "sensor.easee_home_12840_current",
            "enabled": "switch.easee_home_12840_is_enabled",
            "dynamic_charger_limit": "sensor.easee_home_12840_dynamic_charger_limit",
            "max_charger_limit": "sensor.easee_home_12840_max_charger_limit",
            "reason_for_no_current": "sensor.easee_home_12840_reason_for_no_current",
            "override_schedule": "button.easee_home_12840_override_schedule",
        },
    )


@pytest.fixture()
def mock_api() -> AsyncMock:
    api = AsyncMock(spec=HAApiClient)
    api.get_state = AsyncMock(return_value=None)
    api.call_service = AsyncMock(return_value=True)
    return api


@pytest.fixture()
def adapter(mock_api: AsyncMock) -> EaseeAdapter:
    return EaseeAdapter(mock_api, _make_config())


# ===========================================================================
# Properties
# ===========================================================================


class TestProperties:
    """Properties come from config."""

    def test_charger_id(self, adapter: EaseeAdapter) -> None:
        assert adapter.charger_id == "EH128405"

    def test_max_amps(self, adapter: EaseeAdapter) -> None:
        assert adapter.max_amps == 10

    def test_min_amps(self, adapter: EaseeAdapter) -> None:
        assert adapter.min_amps == 6

    def test_phases(self, adapter: EaseeAdapter) -> None:
        assert adapter.phases == 3


# ===========================================================================
# Read methods
# ===========================================================================


@pytest.mark.asyncio
class TestReadMethods:
    """Test read methods with mock HA states."""

    async def test_get_status(self, adapter: EaseeAdapter, mock_api: AsyncMock) -> None:
        mock_api.get_state.return_value = "charging"
        result = await adapter.get_status()
        assert result == "charging"

    async def test_get_status_unavailable(self, adapter: EaseeAdapter, mock_api: AsyncMock) -> None:
        mock_api.get_state.return_value = None
        result = await adapter.get_status()
        assert result == "unknown"

    async def test_get_power_kw_to_w(self, adapter: EaseeAdapter, mock_api: AsyncMock) -> None:
        """Easee reports kW, adapter converts to W."""
        mock_api.get_state.return_value = "6.58"
        result = await adapter.get_power()
        assert result == pytest.approx(6580.0)

    async def test_get_current(self, adapter: EaseeAdapter, mock_api: AsyncMock) -> None:
        mock_api.get_state.return_value = "10.0"
        result = await adapter.get_current()
        assert result == 10.0

    async def test_is_connected_charging(self, adapter: EaseeAdapter, mock_api: AsyncMock) -> None:
        mock_api.get_state.return_value = "charging"
        result = await adapter.is_connected()
        assert result is True

    async def test_is_connected_disconnected(
        self, adapter: EaseeAdapter, mock_api: AsyncMock
    ) -> None:
        mock_api.get_state.return_value = "disconnected"
        result = await adapter.is_connected()
        assert result is False

    async def test_get_reason(self, adapter: EaseeAdapter, mock_api: AsyncMock) -> None:
        mock_api.get_state.return_value = "waiting_in_fully"
        result = await adapter.get_reason_for_no_current()
        assert result == "waiting_in_fully"

    async def test_get_reason_none(self, adapter: EaseeAdapter, mock_api: AsyncMock) -> None:
        mock_api.get_state.return_value = None
        result = await adapter.get_reason_for_no_current()
        assert result is None


# ===========================================================================
# REGRESSION B4: set_current uses charger_id
# ===========================================================================


@pytest.mark.asyncio
class TestB4ChargerIdUsed:
    """B4 regression: set_current must use charger_id, NOT device_id."""

    async def test_set_current_uses_charger_id(
        self, adapter: EaseeAdapter, mock_api: AsyncMock
    ) -> None:
        await adapter.set_current(8)
        mock_api.call_service.assert_awaited_once_with(
            "easee", "set_charger_dynamic_limit",
            {"charger_id": "EH128405", "current": 8},
        )

    async def test_set_current_clamps_min(
        self, adapter: EaseeAdapter, mock_api: AsyncMock
    ) -> None:
        """Current below min_amps should be clamped to min_amps."""
        await adapter.set_current(3)
        call_data = mock_api.call_service.call_args[0][2]
        assert call_data["current"] == 6  # min_amps

    async def test_set_current_clamps_max(
        self, adapter: EaseeAdapter, mock_api: AsyncMock
    ) -> None:
        """Current above max_amps should be clamped to max_amps."""
        await adapter.set_current(16)
        call_data = mock_api.call_service.call_args[0][2]
        assert call_data["current"] == 10  # max_amps


# ===========================================================================
# Start / Stop
# ===========================================================================


@pytest.mark.asyncio
class TestStartStop:
    """Start/stop uses switch entity."""

    async def test_start_charging(self, adapter: EaseeAdapter, mock_api: AsyncMock) -> None:
        await adapter.start_charging()
        mock_api.call_service.assert_awaited_once_with(
            "switch", "turn_on",
            {"entity_id": "switch.easee_home_12840_is_enabled"},
        )

    async def test_stop_charging(self, adapter: EaseeAdapter, mock_api: AsyncMock) -> None:
        await adapter.stop_charging()
        mock_api.call_service.assert_awaited_once_with(
            "switch", "turn_off",
            {"entity_id": "switch.easee_home_12840_is_enabled"},
        )


# ===========================================================================
# REGRESSION B3: waiting_in_fully fix
# ===========================================================================


@pytest.mark.asyncio
class TestB3WaitingInFully:
    """B3 regression: 3-step fix for waiting_in_fully."""

    async def test_fix_sequence(self, adapter: EaseeAdapter, mock_api: AsyncMock) -> None:
        """Fix should: OFF → override_schedule → ON + 6A."""
        # Patch asyncio.sleep to be instant
        import asyncio
        original_sleep = asyncio.sleep

        async def instant_sleep(s: float) -> None:
            pass

        asyncio.sleep = instant_sleep  # type: ignore[assignment]
        try:
            result = await adapter.fix_waiting_in_fully()
            assert result is True

            calls = mock_api.call_service.call_args_list
            # Should have at least 4 calls: OFF, override, ON, set_current
            assert len(calls) >= 4

            # Call 1: switch OFF
            assert calls[0][0][0] == "switch"
            assert calls[0][0][1] == "turn_off"

            # Call 2: button press (override)
            assert calls[1][0][0] == "button"
            assert calls[1][0][1] == "press"

            # Call 3: switch ON
            assert calls[2][0][0] == "switch"
            assert calls[2][0][1] == "turn_on"

            # Call 4: set_charger_dynamic_limit at 6A
            assert calls[3][0][0] == "easee"
            assert calls[3][0][1] == "set_charger_dynamic_limit"
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]
