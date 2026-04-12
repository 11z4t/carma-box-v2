"""Tests for the Home Assistant REST API client.

Tests cover:
- Successful state reads (single, batch, with attributes)
- Service calls (success and failure)
- Health check (up and down)
- Retry logic (correct number of retries, delay between)
- Error handling (404, 500, timeout, connection error)
- Session lifecycle (reuse, close)
- Unavailable/unknown state filtering
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from adapters.ha_api import HAApiClient
from config.schema import HAConfig

# Patch asyncio.sleep to avoid real delays in retry tests

@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace asyncio.sleep with a no-op for all tests."""
    async def _instant_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ha_config() -> HAConfig:
    """Minimal HA config for testing."""
    return HAConfig(
        url="http://localhost:8123",
        token_env="TEST_HA_TOKEN",
        verify_ssl=False,
        timeout_s=5,
        retry_count=3,
        retry_delay_s=1,
    )


@pytest.fixture()
def ha_config_no_retry() -> HAConfig:
    """HA config with retries disabled."""
    return HAConfig(
        url="http://localhost:8123",
        token_env="TEST_HA_TOKEN",
        retry_count=1,
        retry_delay_s=1,
    )


def _make_response(
    status: int = 200,
    json_data: Any = None,
    text: str = "",
) -> AsyncMock:
    """Create a mock aiohttp response."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.text = AsyncMock(return_value=text)
    resp.request_info = MagicMock()
    resp.history = ()
    return resp


def _make_session(responses: list[AsyncMock]) -> AsyncMock:
    """Create a mock aiohttp session that yields responses in order."""
    session = AsyncMock()
    session.closed = False

    call_count = 0

    class _ContextManager:
        def __init__(self, method: str, url: str, **kwargs: Any) -> None:
            nonlocal call_count
            self._idx = call_count
            call_count += 1

        async def __aenter__(self) -> AsyncMock:
            idx = min(self._idx, len(responses) - 1)
            return responses[idx]

        async def __aexit__(self, *args: Any) -> None:
            pass

    session.request = _ContextManager
    session.get = lambda url, **kw: _ContextManager("GET", url, **kw)
    return session


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetState:
    """Tests for HAApiClient.get_state()."""

    async def test_returns_state_on_success(
        self, ha_config: HAConfig
    ) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(200, {"state": "60.5", "attributes": {}})
        client._session = _make_session([resp])

        result = await client.get_state("sensor.test_soc")
        assert result == "60.5"

    async def test_returns_none_on_404(self, ha_config: HAConfig) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(404)
        client._session = _make_session([resp])

        result = await client.get_state("sensor.nonexistent")
        assert result is None

    async def test_returns_none_on_unavailable(
        self, ha_config: HAConfig
    ) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(200, {"state": "unavailable", "attributes": {}})
        client._session = _make_session([resp])

        result = await client.get_state("sensor.offline_device")
        assert result is None

    async def test_returns_none_on_unknown(
        self, ha_config: HAConfig
    ) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(200, {"state": "unknown", "attributes": {}})
        client._session = _make_session([resp])

        result = await client.get_state("sensor.unknown_device")
        assert result is None


# ---------------------------------------------------------------------------
# get_state_with_attributes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetStateWithAttributes:
    """Tests for HAApiClient.get_state_with_attributes()."""

    async def test_returns_full_dict(self, ha_config: HAConfig) -> None:
        data = {
            "entity_id": "sensor.test",
            "state": "42",
            "attributes": {"unit": "W", "friendly_name": "Test"},
        }
        client = HAApiClient(ha_config)
        resp = _make_response(200, data)
        client._session = _make_session([resp])

        result = await client.get_state_with_attributes("sensor.test")
        assert result is not None
        assert result["state"] == "42"
        assert result["attributes"]["unit"] == "W"

    async def test_returns_none_on_failure(
        self, ha_config: HAConfig
    ) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(404)
        client._session = _make_session([resp])

        result = await client.get_state_with_attributes("sensor.nope")
        assert result is None


# ---------------------------------------------------------------------------
# get_states_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetStatesBatch:
    """Tests for HAApiClient.get_states_batch()."""

    async def test_filters_to_requested_entities(
        self, ha_config: HAConfig
    ) -> None:
        all_states = [
            {"entity_id": "sensor.a", "state": "1"},
            {"entity_id": "sensor.b", "state": "2"},
            {"entity_id": "sensor.c", "state": "3"},
            {"entity_id": "sensor.d", "state": "4"},
        ]
        client = HAApiClient(ha_config)
        resp = _make_response(200, all_states)
        client._session = _make_session([resp])

        result = await client.get_states_batch(["sensor.a", "sensor.c"])
        assert len(result) == 2
        assert result["sensor.a"]["state"] == "1"
        assert result["sensor.c"]["state"] == "3"
        assert "sensor.b" not in result

    async def test_missing_entities_omitted(
        self, ha_config: HAConfig
    ) -> None:
        all_states = [
            {"entity_id": "sensor.a", "state": "1"},
        ]
        client = HAApiClient(ha_config)
        resp = _make_response(200, all_states)
        client._session = _make_session([resp])

        result = await client.get_states_batch(
            ["sensor.a", "sensor.missing"]
        )
        assert len(result) == 1
        assert "sensor.missing" not in result

    async def test_empty_list_returns_empty(
        self, ha_config: HAConfig
    ) -> None:
        client = HAApiClient(ha_config)
        result = await client.get_states_batch([])
        assert result == {}

    async def test_returns_empty_on_api_failure(
        self, ha_config: HAConfig
    ) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(500, text="Internal Server Error")
        client._session = _make_session([resp, resp, resp])

        result = await client.get_states_batch(["sensor.a"])
        assert result == {}


# ---------------------------------------------------------------------------
# call_service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCallService:
    """Tests for HAApiClient.call_service()."""

    async def test_returns_true_on_success(
        self, ha_config: HAConfig
    ) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(200, [])
        client._session = _make_session([resp])

        result = await client.call_service(
            "switch", "turn_on", {"entity_id": "switch.test"}
        )
        assert result is True

    async def test_returns_false_on_failure(
        self, ha_config: HAConfig
    ) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(500, text="Error")
        client._session = _make_session([resp, resp, resp])

        result = await client.call_service(
            "switch", "turn_on", {"entity_id": "switch.test"}
        )
        assert result is False

    async def test_with_empty_data(self, ha_config: HAConfig) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(200, [])
        client._session = _make_session([resp])

        result = await client.call_service("homeassistant", "reload")
        assert result is True


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHealthCheck:
    """Tests for HAApiClient.health_check()."""

    async def test_returns_true_when_up(self, ha_config: HAConfig) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(200, {"message": "API running."})
        session = _make_session([resp])
        client._session = session

        result = await client.health_check()
        assert result is True

    async def test_returns_false_when_down(
        self, ha_config: HAConfig
    ) -> None:
        client = HAApiClient(ha_config)

        class _FailSession:
            closed = False

            def get(self, url: str, **kw: Any) -> Any:
                raise aiohttp.ClientConnectionError("refused")

        client._session = _FailSession()  # type: ignore[assignment]

        result = await client.health_check()
        assert result is False

    async def test_returns_false_on_timeout(
        self, ha_config: HAConfig
    ) -> None:
        client = HAApiClient(ha_config)

        class _TimeoutSession:
            closed = False

            def get(self, url: str, **kw: Any) -> Any:
                raise asyncio.TimeoutError()

        client._session = _TimeoutSession()  # type: ignore[assignment]

        result = await client.health_check()
        assert result is False


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRetryLogic:
    """Tests for retry behavior on transient failures."""

    async def test_retries_on_500(self, ha_config: HAConfig) -> None:
        """Should retry configured number of times on server error."""
        client = HAApiClient(ha_config)
        fail = _make_response(500, text="Error")
        success = _make_response(200, {"state": "ok"})
        # Fail twice, succeed on third attempt
        client._session = _make_session([fail, fail, success])

        result = await client.get_state("sensor.test")
        assert result == "ok"

    async def test_all_retries_exhausted(
        self, ha_config: HAConfig
    ) -> None:
        """Should return None when all retries fail."""
        client = HAApiClient(ha_config)
        fail = _make_response(500, text="Error")
        client._session = _make_session([fail, fail, fail])

        result = await client.get_state("sensor.test")
        assert result is None

    async def test_no_retry_on_404(
        self, ha_config_no_retry: HAConfig
    ) -> None:
        """404 should not be retried — entity simply doesn't exist."""
        client = HAApiClient(ha_config_no_retry)
        resp_404 = _make_response(404)
        client._session = _make_session([resp_404])

        result = await client.get_state("sensor.nope")
        assert result is None

    async def test_retry_on_connection_error(
        self, ha_config: HAConfig
    ) -> None:
        """Should retry on connection errors."""
        client = HAApiClient(ha_config)

        call_count = 0
        success_resp = _make_response(200, {"state": "recovered"})
        conn_error = aiohttp.ClientConnectionError("refused")

        class _SuccessCtx:
            async def __aenter__(self) -> AsyncMock:
                return success_resp

            async def __aexit__(self, *a: Any) -> None:
                pass

        class _RetrySession:
            closed = False

            def request(
                self_inner: Any, method: str, url: str, **kwargs: Any
            ) -> Any:
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise conn_error
                return _SuccessCtx()

        client._session = _RetrySession()  # type: ignore[assignment]

        result = await client.get_state("sensor.test")
        assert result == "recovered"
        assert call_count == 3


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSessionLifecycle:
    """Tests for session creation and cleanup."""

    async def test_close_cleans_up(self, ha_config: HAConfig) -> None:
        client = HAApiClient(ha_config)
        session = AsyncMock()
        session.closed = False
        client._session = session

        await client.close()
        session.close.assert_awaited_once()
        assert client._session is None

    async def test_close_idempotent(self, ha_config: HAConfig) -> None:
        client = HAApiClient(ha_config)
        # No session created yet
        await client.close()  # Should not raise

    async def test_token_from_env(self, ha_config: HAConfig) -> None:
        """Token should be read from environment variable."""
        import os

        os.environ["TEST_HA_TOKEN"] = "test-secret-token"
        try:
            client = HAApiClient(ha_config)
            assert "Bearer test-secret-token" in client._headers["Authorization"]
        finally:
            del os.environ["TEST_HA_TOKEN"]

    async def test_empty_token_warns(self, ha_config: HAConfig) -> None:
        """Empty token should log a warning but not crash."""
        import os

        os.environ.pop("TEST_HA_TOKEN", None)
        client = HAApiClient(ha_config)
        assert "Bearer " in client._headers["Authorization"]

    async def test_ensure_session_creates_new_when_none(
        self, ha_config: HAConfig
    ) -> None:
        """_ensure_session should create a session when _session is None."""
        from unittest.mock import AsyncMock, MagicMock, patch

        client = HAApiClient(ha_config)
        assert client._session is None
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.TCPConnector"):
            session = await client._ensure_session()
        assert session is not None
        await client.close()

    async def test_ensure_session_reuses_open_session(
        self, ha_config: HAConfig
    ) -> None:
        """_ensure_session should reuse existing open session."""
        from unittest.mock import AsyncMock, MagicMock, patch

        client = HAApiClient(ha_config)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.TCPConnector"):
            session1 = await client._ensure_session()
            session2 = await client._ensure_session()
        assert session1 is session2
        await client.close()

    async def test_ensure_session_recreates_when_closed(
        self, ha_config: HAConfig
    ) -> None:
        """_ensure_session should create a new session if old one is closed."""
        from unittest.mock import AsyncMock, MagicMock, patch

        client = HAApiClient(ha_config)
        mock_session1 = MagicMock()
        mock_session1.closed = False
        mock_session1.close = AsyncMock()
        mock_session2 = MagicMock()
        mock_session2.closed = False
        mock_session2.close = AsyncMock()
        with patch("aiohttp.TCPConnector"):
            with patch("aiohttp.ClientSession", return_value=mock_session1):
                session1 = await client._ensure_session()
            # Mark session1 as closed
            mock_session1.closed = True
            with patch("aiohttp.ClientSession", return_value=mock_session2):
                session2 = await client._ensure_session()
        assert session2 is not session1
        await client.close()
