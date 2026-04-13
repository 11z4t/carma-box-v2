"""Tests for Solcast PV Forecast and Nordpool Price Adapters.

Covers:
- Solcast: parse entity, p10/p90 risk, unavailable fallback
- Nordpool: current price, today/tomorrow, tomorrow_valid, cheap/expensive
- P10 safety tiers
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from adapters.nordpool import NordpoolAdapter, NordpoolConfig
from adapters.solcast import PVForecast, SolcastAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_api() -> AsyncMock:
    api = AsyncMock()
    api.get_state = AsyncMock(return_value=None)
    api.get_state_with_attributes = AsyncMock(return_value=None)
    return api


# ===========================================================================
# Solcast
# ===========================================================================


@pytest.mark.asyncio
class TestSolcast:
    """Solcast PV forecast adapter."""

    async def test_get_today_parses_state(self, mock_api: AsyncMock) -> None:
        mock_api.get_state_with_attributes.return_value = {
            "state": "25.5",
            "attributes": {"pv_estimate10": 18.0, "pv_estimate90": 32.0},
        }
        adapter = SolcastAdapter(mock_api, "sensor.today", "sensor.tomorrow")
        forecast = await adapter.get_today()
        assert forecast.total_kwh == pytest.approx(25.5)
        assert forecast.p10_kwh == 18.0
        assert forecast.p90_kwh == 32.0

    async def test_unavailable_returns_zero(self, mock_api: AsyncMock) -> None:
        mock_api.get_state_with_attributes.return_value = None
        adapter = SolcastAdapter(mock_api, "sensor.today", "sensor.tomorrow")
        forecast = await adapter.get_today()
        assert forecast.total_kwh == 0.0

    async def test_invalid_state_returns_zero(self, mock_api: AsyncMock) -> None:
        mock_api.get_state_with_attributes.return_value = {
            "state": "unavailable", "attributes": {},
        }
        adapter = SolcastAdapter(mock_api, "sensor.today", "sensor.tomorrow")
        forecast = await adapter.get_today()
        assert forecast.total_kwh == 0.0


class TestP10Safety:
    """P10 safety discharge rate tiers."""

    def test_low_p10_conservative(self) -> None:
        adapter = SolcastAdapter(AsyncMock(), "s.t", "s.tm")
        forecast = PVForecast(total_kwh=10.0, p10_kwh=3.0, confidence_pct=50.0)
        assert adapter.discharge_rate_kw(forecast) == 0.5  # conservative

    def test_low_confidence_conservative(self) -> None:
        adapter = SolcastAdapter(AsyncMock(), "s.t", "s.tm")
        forecast = PVForecast(total_kwh=10.0, p10_kwh=8.0, confidence_pct=15.0)
        assert adapter.discharge_rate_kw(forecast) == 0.5

    def test_moderate_forecast(self) -> None:
        adapter = SolcastAdapter(AsyncMock(), "s.t", "s.tm")
        forecast = PVForecast(total_kwh=8.0, p10_kwh=6.0, confidence_pct=50.0)
        assert adapter.discharge_rate_kw(forecast) == 1.0  # moderate

    def test_high_forecast_normal(self) -> None:
        adapter = SolcastAdapter(AsyncMock(), "s.t", "s.tm")
        forecast = PVForecast(total_kwh=25.0, p10_kwh=18.0, confidence_pct=70.0)
        assert adapter.discharge_rate_kw(forecast) == 2.0  # normal

    def test_confidence_zero_pv(self) -> None:
        assert SolcastAdapter._calc_confidence(0.0, 0.0) == 0.0

    def test_confidence_ratio(self) -> None:
        # M9 fix: confidence = max(0, 1 - (p90-p10)/estimate) * 100
        # estimate=20, p10=14, p90=26 → spread=12 → 1 - 12/20 = 0.4 → 40%
        c = SolcastAdapter._calc_confidence(20.0, 14.0, 26.0)
        assert c == pytest.approx(40.0)

    def test_confidence_narrow_spread(self) -> None:
        # Narrow spread → high confidence
        # estimate=20, p10=18, p90=22 → spread=4 → 1 - 4/20 = 0.8 → 80%
        c = SolcastAdapter._calc_confidence(20.0, 18.0, 22.0)
        assert c == pytest.approx(80.0)


# ===========================================================================
# Nordpool
# ===========================================================================


@pytest.mark.asyncio
class TestNordpool:
    """Nordpool price adapter."""

    async def test_current_price(self, mock_api: AsyncMock) -> None:
        mock_api.get_state.return_value = "0.45"  # SEK/kWh
        adapter = NordpoolAdapter(mock_api, NordpoolConfig(entity="sensor.np"))
        price = await adapter.get_current_price()
        assert price == pytest.approx(45.0)  # öre

    async def test_current_price_unavailable(self, mock_api: AsyncMock) -> None:
        mock_api.get_state.return_value = None
        adapter = NordpoolAdapter(mock_api, NordpoolConfig(entity="sensor.np"))
        price = await adapter.get_current_price()
        assert price == 100.0  # fallback

    async def test_current_price_invalid(self, mock_api: AsyncMock) -> None:
        mock_api.get_state.return_value = "not_a_number"
        adapter = NordpoolAdapter(mock_api, NordpoolConfig(entity="sensor.np"))
        price = await adapter.get_current_price()
        assert price == 100.0

    async def test_today_prices(self, mock_api: AsyncMock) -> None:
        mock_api.get_state_with_attributes.return_value = {
            "state": "0.45",
            "attributes": {
                "today": [0.30, 0.25, 0.20, 0.15],
                "tomorrow_valid": False,
            },
        }
        adapter = NordpoolAdapter(mock_api, NordpoolConfig(entity="sensor.np"))
        prices = await adapter.get_today_prices()
        assert len(prices) == 4
        assert prices[0] == pytest.approx(30.0)

    async def test_tomorrow_not_available(self, mock_api: AsyncMock) -> None:
        mock_api.get_state_with_attributes.return_value = {
            "state": "0.45",
            "attributes": {"tomorrow_valid": False},
        }
        adapter = NordpoolAdapter(mock_api, NordpoolConfig(entity="sensor.np"))
        prices = await adapter.get_tomorrow_prices()
        assert prices == {}

    async def test_tomorrow_available(self, mock_api: AsyncMock) -> None:
        mock_api.get_state_with_attributes.return_value = {
            "state": "0.45",
            "attributes": {
                "tomorrow_valid": True,
                "tomorrow": [0.50, 0.60],
            },
        }
        adapter = NordpoolAdapter(mock_api, NordpoolConfig(entity="sensor.np"))
        available = await adapter.is_tomorrow_available()
        assert available is True
        prices = await adapter.get_tomorrow_prices()
        assert len(prices) == 2

    async def test_read_prices_none_returns_empty(self, mock_api: AsyncMock) -> None:
        mock_api.get_state_with_attributes.return_value = None
        adapter = NordpoolAdapter(mock_api, NordpoolConfig(entity="sensor.np"))
        prices = await adapter.get_today_prices()
        assert prices == {}

    async def test_read_prices_not_list_returns_empty(self, mock_api: AsyncMock) -> None:
        mock_api.get_state_with_attributes.return_value = {
            "state": "0.45",
            "attributes": {"today": "not a list"},
        }
        adapter = NordpoolAdapter(mock_api, NordpoolConfig(entity="sensor.np"))
        prices = await adapter.get_today_prices()
        assert prices == {}


class TestNordpoolClassification:
    """Price classification."""

    def test_cheap(self) -> None:
        adapter = NordpoolAdapter(AsyncMock(), NordpoolConfig())
        assert adapter.is_cheap(20.0) is True
        assert adapter.is_cheap(50.0) is False

    def test_expensive(self) -> None:
        adapter = NordpoolAdapter(AsyncMock(), NordpoolConfig())
        assert adapter.is_expensive(100.0) is True
        assert adapter.is_expensive(50.0) is False
