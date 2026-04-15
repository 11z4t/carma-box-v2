"""Shelly Load Adapter for CARMA Box.

Controls Shelly switches (ON/OFF) via HA switch service.
Reads power from power sensor entity.
Used for dispatchable consumers: miner, VP, pool pump.

PLAT-1657: Part of CARMA Box v2 migration.
All entity IDs from config — zero hardcoding.
"""

from __future__ import annotations

import logging

from adapters.base import LoadAdapter
from adapters.ha_api import HAApiClient

logger = logging.getLogger(__name__)

# HA service domain for switch control.
_SWITCH_DOMAIN: str = "switch"
_SERVICE_ON: str = "turn_on"
_SERVICE_OFF: str = "turn_off"

# Power threshold below which load is considered inactive (W).
_ACTIVE_THRESHOLD_W: float = 10.0


class ShellyAdapter(LoadAdapter):
    """Shelly switch adapter — ON/OFF control via HA switch service.

    Implements LoadAdapter ABC for dispatchable consumers.
    Entity IDs come from ConsumerConfig in site.yaml.
    """

    def __init__(
        self,
        ha_api: HAApiClient,
        consumer_id: str,
        entity_switch: str,
        entity_power: str,
        load_type_str: str = "on_off",
    ) -> None:
        self._api = ha_api
        self._consumer_id = consumer_id
        self._entity_switch = entity_switch
        self._entity_power = entity_power
        self._load_type_str = load_type_str

    @property
    def load_id(self) -> str:
        return self._consumer_id

    @property
    def load_type(self) -> str:
        return self._load_type_str

    async def get_power(self) -> float:
        """Read current power from HA sensor."""
        if not self._entity_power:
            return 0.0
        state = await self._api.get_state(self._entity_power)
        if state is None:
            return 0.0
        try:
            return max(0.0, float(state))
        except (ValueError, TypeError):
            return 0.0

    async def is_active(self) -> bool:
        """Check if load is drawing power above threshold."""
        power = await self.get_power()
        return power > _ACTIVE_THRESHOLD_W

    async def turn_on(self) -> bool:
        """Turn on via HA switch service."""
        if not self._entity_switch:
            logger.error("No switch entity for %s", self._consumer_id)
            return False
        logger.info("Shelly ON: %s (%s)", self._consumer_id, self._entity_switch)
        return await self._api.call_service(
            _SWITCH_DOMAIN, _SERVICE_ON,
            {"entity_id": self._entity_switch},
        )

    async def turn_off(self) -> bool:
        """Turn off via HA switch service."""
        if not self._entity_switch:
            logger.error("No switch entity for %s", self._consumer_id)
            return False
        logger.info("Shelly OFF: %s (%s)", self._consumer_id, self._entity_switch)
        return await self._api.call_service(
            _SWITCH_DOMAIN, _SERVICE_OFF,
            {"entity_id": self._entity_switch},
        )

    async def set_power(self, watts: int) -> bool:
        """No-op for ON/OFF loads."""
        return True
