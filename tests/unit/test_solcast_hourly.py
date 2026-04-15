"""Unit tests for Solcast hourly forecast — PLAT-1628.

Tests hourly parsing, caching, bad data handling, and timezone.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from adapters.solcast import SolcastAdapter
from core.day_plan import ZERO_HOURLY_FORECAST

# ---------------------------------------------------------------------------
# Named test constants
# ---------------------------------------------------------------------------

_ENTITY_TODAY: str = "sensor.solcast_pv_forecast_forecast_today"
_ENTITY_TOMORROW: str = "sensor.solcast_pv_forecast_forecast_tomorrow"

_HOUR_10: int = 10
_HOUR_11: int = 11
_HOUR_15: int = 15
_CURRENT_HOUR: int = 10

_P50_H10: float = 2.5
_P10_H10: float = 1.8
_P90_H10: float = 3.2
_P50_H11: float = 3.0
_P10_H11: float = 2.1
_P90_H11: float = 4.0

_BAD_P10: float = 5.0
_BAD_P50: float = 2.0


def _mock_forecast_data(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build mock HA state response with forecast attribute."""
    return {
        "state": "25.0",
        "attributes": {
            "forecast": entries,
            "pv_estimate": 25.0,
        },
    }


def _entry(
    hour: int,
    p50: float,
    p10: float | None = None,
    p90: float | None = None,
) -> dict[str, Any]:
    """Build a single forecast entry."""
    result: dict[str, Any] = {
        "period_start": f"2026-04-15T{hour:02d}:00:00+02:00",
        "pv_estimate": p50,
    }
    if p10 is not None:
        result["pv_estimate10"] = p10
    if p90 is not None:
        result["pv_estimate90"] = p90
    return result


@pytest.fixture
def ha_api() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def adapter(ha_api: AsyncMock) -> SolcastAdapter:
    return SolcastAdapter(
        ha_api=ha_api,
        entity_today=_ENTITY_TODAY,
        entity_tomorrow=_ENTITY_TOMORROW,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHourlyForecast:
    """Tests for get_hourly_forecast()."""

    @pytest.mark.asyncio
    async def test_returns_all_hours(
        self, adapter: SolcastAdapter, ha_api: AsyncMock,
    ) -> None:
        """Forecast entries parsed into dict[hour, HourlyForecast]."""
        ha_api.get_state_with_attributes.return_value = _mock_forecast_data([
            _entry(_HOUR_10, _P50_H10, _P10_H10, _P90_H10),
            _entry(_HOUR_11, _P50_H11, _P10_H11, _P90_H11),
        ])

        result = await adapter.get_hourly_forecast(_CURRENT_HOUR)

        assert _HOUR_10 in result
        assert _HOUR_11 in result
        assert result[_HOUR_10].p50_kwh == _P50_H10
        assert result[_HOUR_11].p10_kwh == _P10_H11

    @pytest.mark.asyncio
    async def test_p10_lte_p50_lte_p90(
        self, adapter: SolcastAdapter, ha_api: AsyncMock,
    ) -> None:
        """Invariant p10 <= p50 <= p90 holds for normal data."""
        ha_api.get_state_with_attributes.return_value = _mock_forecast_data([
            _entry(_HOUR_10, _P50_H10, _P10_H10, _P90_H10),
        ])

        result = await adapter.get_hourly_forecast(_CURRENT_HOUR)
        f = result[_HOUR_10]

        assert f.p10_kwh <= f.p50_kwh <= f.p90_kwh

    @pytest.mark.asyncio
    async def test_missing_hour_returns_zero(
        self, adapter: SolcastAdapter, ha_api: AsyncMock,
    ) -> None:
        """Missing hours are not in dict — caller uses ZERO_HOURLY_FORECAST."""
        ha_api.get_state_with_attributes.return_value = _mock_forecast_data([
            _entry(_HOUR_10, _P50_H10, _P10_H10, _P90_H10),
        ])

        result = await adapter.get_hourly_forecast(_CURRENT_HOUR)

        assert _HOUR_15 not in result
        # Caller should use .get(hour, ZERO_HOURLY_FORECAST)
        fallback = result.get(_HOUR_15, ZERO_HOURLY_FORECAST)
        assert fallback.p50_kwh == 0.0

    @pytest.mark.asyncio
    async def test_cache_prevents_duplicate_calls(
        self, adapter: SolcastAdapter, ha_api: AsyncMock,
    ) -> None:
        """Second call same hour uses cache — no API call."""
        ha_api.get_state_with_attributes.return_value = _mock_forecast_data([
            _entry(_HOUR_10, _P50_H10, _P10_H10, _P90_H10),
        ])

        await adapter.get_hourly_forecast(_CURRENT_HOUR)
        await adapter.get_hourly_forecast(_CURRENT_HOUR)

        ha_api.get_state_with_attributes.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_invalidated_on_hour_change(
        self, adapter: SolcastAdapter, ha_api: AsyncMock,
    ) -> None:
        """New hour triggers fresh API call."""
        ha_api.get_state_with_attributes.return_value = _mock_forecast_data([
            _entry(_HOUR_10, _P50_H10, _P10_H10, _P90_H10),
        ])
        _NEXT_HOUR: int = _CURRENT_HOUR + 1

        await adapter.get_hourly_forecast(_CURRENT_HOUR)
        await adapter.get_hourly_forecast(_NEXT_HOUR)

        assert ha_api.get_state_with_attributes.call_count == 2

    @pytest.mark.asyncio
    async def test_p10_clamped_when_bad_data(
        self, adapter: SolcastAdapter, ha_api: AsyncMock,
    ) -> None:
        """p10 > p50 → clamped to p50 by HourlyForecast invariant."""
        ha_api.get_state_with_attributes.return_value = _mock_forecast_data([
            _entry(_HOUR_10, _BAD_P50, _BAD_P10, _P90_H10),
        ])

        result = await adapter.get_hourly_forecast(_CURRENT_HOUR)
        f = result[_HOUR_10]

        assert f.p10_kwh == _BAD_P50  # Clamped to p50

    @pytest.mark.asyncio
    async def test_entity_unavailable_returns_empty(
        self, adapter: SolcastAdapter, ha_api: AsyncMock,
    ) -> None:
        """Unavailable entity returns empty dict — never raises."""
        ha_api.get_state_with_attributes.return_value = None

        result = await adapter.get_hourly_forecast(_CURRENT_HOUR)

        assert result == {}

    @pytest.mark.asyncio
    async def test_empty_forecast_list_returns_empty(
        self, adapter: SolcastAdapter, ha_api: AsyncMock,
    ) -> None:
        """Empty forecast[] attribute returns empty dict."""
        ha_api.get_state_with_attributes.return_value = _mock_forecast_data([])

        result = await adapter.get_hourly_forecast(_CURRENT_HOUR)

        assert result == {}
