"""Tests for GoldshellMinerAdapter stub (PLAT-1700).

The adapter is a safety-first stub until the /mcb/newconfig REST channel
is wired: it reads power from HA but refuses turn_on/turn_off/set_power
so a Shelly relay fallback cannot be triggered and damage the ASIC.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from adapters.goldshell import GoldshellMinerAdapter


@pytest.mark.asyncio()
async def test_turn_on_is_no_op() -> None:
    """turn_on must return False (stub) and never call the HA api."""
    api = AsyncMock()
    adapter = GoldshellMinerAdapter(
        ha_api=api, consumer_id="miner",
        entity_power="sensor.miner_power",
    )

    result = await adapter.turn_on()
    assert result is False
    api.call_service.assert_not_called()


@pytest.mark.asyncio()
async def test_turn_off_is_no_op() -> None:
    """turn_off must return False (stub) and never call the HA api —
    this is the safety invariant: NO relay power-cycling.
    """
    api = AsyncMock()
    adapter = GoldshellMinerAdapter(
        ha_api=api, consumer_id="miner",
        entity_power="sensor.miner_power",
    )

    result = await adapter.turn_off()
    assert result is False
    api.call_service.assert_not_called()


@pytest.mark.asyncio()
async def test_set_power_is_no_op() -> None:
    """set_power logs + returns False until /mcb/newconfig is wired."""
    api = AsyncMock()
    adapter = GoldshellMinerAdapter(
        ha_api=api, consumer_id="miner",
        entity_power="sensor.miner_power",
    )

    assert await adapter.set_power(300) is False
    api.call_service.assert_not_called()


@pytest.mark.asyncio()
async def test_get_power_reads_ha_entity() -> None:
    """Power must flow from the HA entity sensor — kund-agnostisk."""
    api = AsyncMock()
    api.get_state = AsyncMock(return_value="421.5")
    adapter = GoldshellMinerAdapter(
        ha_api=api, consumer_id="miner",
        entity_power="sensor.appliance_total_effekt",
    )

    power = await adapter.get_power()
    assert power == pytest.approx(421.5)
    api.get_state.assert_awaited_once_with("sensor.appliance_total_effekt")


@pytest.mark.asyncio()
async def test_get_power_returns_zero_when_entity_missing() -> None:
    """No entity configured → 0.0 W (safe default)."""
    api = AsyncMock()
    adapter = GoldshellMinerAdapter(
        ha_api=api, consumer_id="miner", entity_power="",
    )

    assert await adapter.get_power() == 0.0
    api.get_state.assert_not_called()


@pytest.mark.asyncio()
async def test_get_power_returns_zero_on_invalid_state() -> None:
    """Non-numeric state (e.g. 'unavailable') must not crash."""
    api = AsyncMock()
    api.get_state = AsyncMock(return_value="unavailable")
    adapter = GoldshellMinerAdapter(
        ha_api=api, consumer_id="miner",
        entity_power="sensor.miner_power",
    )

    assert await adapter.get_power() == 0.0


@pytest.mark.asyncio()
async def test_is_active_tracks_power_threshold() -> None:
    """Active iff measured power > 10 W threshold."""
    api = AsyncMock()
    adapter = GoldshellMinerAdapter(
        ha_api=api, consumer_id="miner",
        entity_power="sensor.miner_power",
    )

    api.get_state = AsyncMock(return_value="15")
    assert await adapter.is_active() is True

    api.get_state = AsyncMock(return_value="5")
    assert await adapter.is_active() is False


def test_load_id_and_type_exposed() -> None:
    adapter = GoldshellMinerAdapter(
        ha_api=AsyncMock(), consumer_id="miner-01",
        entity_power="sensor.miner_power",
    )
    assert adapter.load_id == "miner-01"
    assert adapter.load_type == "goldshell_miner"
