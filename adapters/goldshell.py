"""Goldshell SC Box II miner adapter for CARMA Box.

PLAT-1700: Mining hardware must NEVER be power-cycled via relay (e.g. Shelly)
— power cycling damages ASIC chips. Control must go through the miner's own
REST interface at ``/mcb/newconfig`` with mode values 0/1/2:

    0 = off / idle    (soft stop)
    1 = low hashrate  (cold-heater friendly)
    2 = full hashrate (production)

Current status (2026-04-18):
- Network reachability from carma-box host to the miner is not yet in place.
- HTTP :80 and CGMiner TCP :4028 are unreachable from VM 900 / 902.
- Until reachability is established, dispatch methods are SAFE no-ops that
  log a warning and return False. Power draw is still read via HA
  ``entity_power`` so surplus accounting sees the miner as a fixed load.

When the network is opened, this adapter should:
  1. Use aiohttp to POST ``http://<host>/mcb/newconfig`` with ``mode`` body.
  2. Use CGMiner TCP :4028 for read-only telemetry (summary, pools, devs).
  3. Expose ``set_mode()`` for 0/1/2 alongside the LoadAdapter surface.

Kund-agnostisk: host + credentials live in ConsumerConfig / site.yaml —
this module has zero hardcoded endpoints.
"""

from __future__ import annotations

import logging

from adapters.base import LoadAdapter
from adapters.ha_api import HAApiClient

logger = logging.getLogger(__name__)

_ACTIVE_THRESHOLD_W: float = 10.0


class GoldshellMinerAdapter(LoadAdapter):
    """Safe-by-default stub adapter for Goldshell SC Box II miners.

    Reads power from an HA sensor (entity_power). Dispatch writes
    (turn_on/turn_off/set_power) are no-ops that log and return False
    until the REST/TCP channel to the miner is wired.
    """

    def __init__(
        self,
        ha_api: HAApiClient,
        consumer_id: str,
        entity_power: str = "",
    ) -> None:
        self._ha = ha_api
        self._consumer_id = consumer_id
        self._entity_power = entity_power
        self._log = logger.getChild(consumer_id)
        self._log.info(
            "GoldshellMinerAdapter (stub) initialised — dispatch disabled "
            "until /mcb/newconfig channel is wired. Power is read-only.",
        )

    async def get_power(self) -> float:
        """Read current power draw from HA entity_power sensor."""
        if not self._entity_power:
            return 0.0
        try:
            state = await self._ha.get_state(self._entity_power)
            if state is None:
                return 0.0
            return float(state)
        except (TypeError, ValueError):
            return 0.0

    async def is_active(self) -> bool:
        """Active iff measured power exceeds the activity threshold."""
        return await self.get_power() > _ACTIVE_THRESHOLD_W

    async def turn_on(self) -> bool:
        """PLAT-1700: disabled until CGMiner REST channel is wired.

        NEVER fall back to a Shelly relay — power-cycling the miner
        damages ASIC hardware.
        """
        self._log.warning(
            "turn_on requested but GoldshellMinerAdapter is a stub "
            "(no /mcb/newconfig channel). Ignoring to avoid relay fallback.",
        )
        return False

    async def turn_off(self) -> bool:
        """PLAT-1700: disabled until CGMiner REST channel is wired."""
        self._log.warning(
            "turn_off requested but GoldshellMinerAdapter is a stub "
            "(no /mcb/newconfig channel). Ignoring to avoid relay fallback.",
        )
        return False

    async def set_power(self, watts: int) -> bool:
        """PLAT-1700: disabled — future impl maps watts → mode 0/1/2."""
        self._log.warning(
            "set_power(%d W) requested but GoldshellMinerAdapter is a stub "
            "(no /mcb/newconfig channel). Ignoring.",
            watts,
        )
        return False

    @property
    def load_id(self) -> str:
        return self._consumer_id

    @property
    def load_type(self) -> str:
        return "goldshell_miner"
