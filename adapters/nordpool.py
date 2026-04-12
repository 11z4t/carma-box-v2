"""Nordpool Price Adapter for CARMA Box.

Reads electricity prices from HA Nordpool integration entity.
Today/tomorrow prices from entity attributes.

All entity IDs from config — zero hardcoding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from adapters.ha_api import HAApiClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NordpoolConfig:
    """Nordpool adapter config — from site.yaml."""

    entity: str = ""
    fallback_ore: float = 100.0
    cheap_ore: float = 30.0
    expensive_ore: float = 80.0


class NordpoolAdapter:
    """Reads Nordpool electricity prices from HA entity."""

    def __init__(
        self,
        ha_api: HAApiClient,
        config: NordpoolConfig,
    ) -> None:
        self._api = ha_api
        self._config = config

    async def get_current_price(self) -> float:
        """Get current electricity price in öre/kWh."""
        state = await self._api.get_state(self._config.entity)
        if state is None:
            return self._config.fallback_ore
        try:
            # Nordpool reports in SEK/kWh, convert to öre
            return float(state) * 100.0
        except (ValueError, TypeError):
            return self._config.fallback_ore

    async def get_today_prices(self) -> dict[int, float]:
        """Get today's hourly prices (hour → öre/kWh)."""
        return await self._read_prices("today")

    async def get_tomorrow_prices(self) -> dict[int, float]:
        """Get tomorrow's hourly prices. Empty dict if not available."""
        if not await self.is_tomorrow_available():
            return {}
        return await self._read_prices("tomorrow")

    async def is_tomorrow_available(self) -> bool:
        """Check if tomorrow's prices are published (~13:00 CET)."""
        data = await self._api.get_state_with_attributes(self._config.entity)
        if data is None:
            return False
        attrs = data.get("attributes", {})
        return bool(attrs.get("tomorrow_valid", False))

    async def _read_prices(self, key: str) -> dict[int, float]:
        """Read hourly prices from entity attributes."""
        data = await self._api.get_state_with_attributes(self._config.entity)
        if data is None:
            return {}

        attrs = data.get("attributes", {})
        raw = attrs.get(key, [])

        if not isinstance(raw, list):
            return {}

        prices: dict[int, float] = {}
        for i, val in enumerate(raw):
            try:
                prices[i] = float(val) * 100.0  # SEK → öre
            except (ValueError, TypeError):
                prices[i] = self._config.fallback_ore

        return prices

    def is_cheap(self, price_ore: float) -> bool:
        """Is this price considered cheap?"""
        return price_ore <= self._config.cheap_ore

    def is_expensive(self, price_ore: float) -> bool:
        """Is this price considered expensive?"""
        return price_ore >= self._config.expensive_ore
