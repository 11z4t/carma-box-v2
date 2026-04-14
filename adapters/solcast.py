"""Solcast PV Forecast Adapter for CARMA Box.

Reads PV forecast from HA Solcast integration entities.
Includes p10/p90 risk assessment for conservative planning.

All entity IDs from config — zero hardcoding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from adapters.ha_api import HAApiClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PVForecast:
    """PV forecast data for a single day."""

    total_kwh: float = 0.0
    p10_kwh: float = 0.0
    p90_kwh: float = 0.0
    confidence_pct: float = 50.0
    hourly: list[float] | None = None  # Per-hour kWh


@dataclass(frozen=True)
class P10SafetyConfig:
    """P10 safety thresholds — from site.yaml."""

    threshold_kwh: float = 5.0
    confidence_pct: float = 20.0
    conservative_kw: float = 0.5
    moderate_kw: float = 1.0
    normal_kw: float = 2.0
    p10_factor: float = 0.7   # Fallback: p10 = estimate * factor
    p90_factor: float = 1.3   # Fallback: p90 = estimate * factor


class SolcastAdapter:
    """Reads Solcast PV forecast from HA entities."""

    def __init__(
        self,
        ha_api: HAApiClient,
        entity_today: str,
        entity_tomorrow: str,
        p10_config: P10SafetyConfig | None = None,
    ) -> None:
        self._api = ha_api
        self._entity_today = entity_today
        self._entity_tomorrow = entity_tomorrow
        self._p10 = p10_config or P10SafetyConfig()

    async def get_today(self) -> PVForecast:
        """Get today's PV forecast."""
        return await self._read_forecast(self._entity_today)

    async def get_tomorrow(self) -> PVForecast:
        """Get tomorrow's PV forecast."""
        return await self._read_forecast(self._entity_tomorrow)

    def discharge_rate_kw(self, forecast: PVForecast) -> float:
        """Calculate safe discharge rate based on p10 safety.

        Low confidence or low p10 → conservative rate.
        """
        low_p10 = forecast.p10_kwh < self._p10.threshold_kwh
        low_confidence = forecast.confidence_pct < self._p10.confidence_pct
        if low_p10 or low_confidence:
            return self._p10.conservative_kw
        if forecast.total_kwh < self._p10.threshold_kwh * 2:
            return self._p10.moderate_kw
        return self._p10.normal_kw

    async def _read_forecast(self, entity_id: str) -> PVForecast:
        """Read forecast from HA entity with attributes."""
        data = await self._api.get_state_with_attributes(entity_id)
        if data is None:
            return PVForecast()

        state = data.get("state", "0")
        attrs = data.get("attributes", {})

        try:
            total = float(state)
        except (ValueError, TypeError):
            total = 0.0

        p10 = float(attrs.get("pv_estimate10", total * self._p10.p10_factor))
        p90 = float(attrs.get("pv_estimate90", total * self._p10.p90_factor))
        return PVForecast(
            total_kwh=total,
            p10_kwh=p10,
            p90_kwh=p90,
            confidence_pct=self._calc_confidence(total, p10, p90),
        )

    @staticmethod
    def _calc_confidence(estimate: float, p10: float, p90: float = 0.0) -> float:
        """Calculate confidence percentage based on forecast spread.

        Higher confidence when p90-p10 spread is small relative to estimate.
        confidence = max(0, 1 - (p90 - p10) / estimate) * 100
        """
        if estimate <= 0:
            return 0.0
        spread = p90 - p10
        return max(0.0, (1.0 - spread / estimate)) * 100.0
