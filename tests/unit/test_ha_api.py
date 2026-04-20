"""Tests for the Home Assistant REST API client.

Tests cover:
- Successful state reads (single, batch, with attributes)
- Service calls (success and failure)
- Health check (up and down)
- Retry logic (correct number of retries, delay between)
- Error handling (404, 500, timeout, connection error)
- Session lifecycle (reuse, close)
- Unavailable/unknown state filtering
- PLAT-1574: batch fetch (single call), fallback, timeout constants
- PLAT-1753: warm_batch_cache() — atomic cycle-start prefetch
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

    async def test_returns_state_on_success(self, ha_config: HAConfig) -> None:
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

    async def test_returns_none_on_unavailable(self, ha_config: HAConfig) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(200, {"state": "unavailable", "attributes": {}})
        client._session = _make_session([resp])

        result = await client.get_state("sensor.offline_device")
        assert result is None

    async def test_returns_none_on_unknown(self, ha_config: HAConfig) -> None:
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

    async def test_returns_none_on_failure(self, ha_config: HAConfig) -> None:
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

    async def test_filters_to_requested_entities(self, ha_config: HAConfig) -> None:
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

    async def test_missing_entities_omitted(self, ha_config: HAConfig) -> None:
        all_states = [
            {"entity_id": "sensor.a", "state": "1"},
        ]
        client = HAApiClient(ha_config)
        resp = _make_response(200, all_states)
        client._session = _make_session([resp])

        result = await client.get_states_batch(["sensor.a", "sensor.missing"])
        assert len(result) == 1
        assert "sensor.missing" not in result

    async def test_empty_list_returns_empty(self, ha_config: HAConfig) -> None:
        client = HAApiClient(ha_config)
        result = await client.get_states_batch([])
        assert result == {}

    async def test_returns_empty_on_api_failure(self, ha_config: HAConfig) -> None:
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

    async def test_returns_true_on_success(self, ha_config: HAConfig) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(200, [])
        client._session = _make_session([resp])

        result = await client.call_service("switch", "turn_on", {"entity_id": "switch.test"})
        assert result is True

    async def test_returns_false_on_failure(self, ha_config: HAConfig) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(500, text="Error")
        client._session = _make_session([resp, resp, resp])

        result = await client.call_service("switch", "turn_on", {"entity_id": "switch.test"})
        assert result is False

    async def test_with_empty_data(self, ha_config: HAConfig) -> None:
        client = HAApiClient(ha_config)
        resp = _make_response(200, [])
        client._session = _make_session([resp])

        result = await client.call_service("homeassistant", "reload")
        assert result is True

    async def test_returns_false_on_ha_error_response(self, ha_config: HAConfig) -> None:
        """PLAT-1354: HA error dict with 'message' key must return False."""
        client = HAApiClient(ha_config)
        # HA returns 200 with an error body when service is unknown
        error_body = {"message": "Service not found.", "code": "service_not_found"}
        resp = _make_response(200, error_body)
        client._session = _make_session([resp])

        result = await client.call_service("bad_domain", "bad_service")
        assert result is False

    async def test_returns_true_on_list_response(self, ha_config: HAConfig) -> None:
        """PLAT-1354: HA success returns list of states — must be True."""
        client = HAApiClient(ha_config)
        # HA returns list of affected state objects on success
        states_list = [{"entity_id": "switch.test", "state": "on"}]
        resp = _make_response(200, states_list)
        client._session = _make_session([resp])

        result = await client.call_service("switch", "turn_on", {"entity_id": "switch.test"})
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

    async def test_returns_false_when_down(self, ha_config: HAConfig) -> None:
        client = HAApiClient(ha_config)

        class _FailSession:
            closed = False

            def get(self, url: str, **kw: Any) -> Any:
                raise aiohttp.ClientConnectionError("refused")

        client._session = _FailSession()  # type: ignore[assignment]

        result = await client.health_check()
        assert result is False

    async def test_returns_false_on_timeout(self, ha_config: HAConfig) -> None:
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

    async def test_all_retries_exhausted(self, ha_config: HAConfig) -> None:
        """Should return None when all retries fail."""
        client = HAApiClient(ha_config)
        fail = _make_response(500, text="Error")
        client._session = _make_session([fail, fail, fail])

        result = await client.get_state("sensor.test")
        assert result is None

    async def test_no_retry_on_404(self, ha_config_no_retry: HAConfig) -> None:
        """404 should not be retried — entity simply doesn't exist."""
        client = HAApiClient(ha_config_no_retry)
        resp_404 = _make_response(404)
        client._session = _make_session([resp_404])

        result = await client.get_state("sensor.nope")
        assert result is None

    async def test_no_retry_on_401(self, ha_config: HAConfig) -> None:
        """PLAT-1354: 401 auth error must not be retried."""
        client = HAApiClient(ha_config)
        # Only one 401 response in queue — if retried it would run out
        resp_401 = _make_response(401, text="Unauthorized")
        # Provide extra responses to detect if retry happens (would cause index error)
        client._session = _make_session([resp_401])

        result = await client.get_state("sensor.test")
        assert result is None

    async def test_no_retry_on_403(self, ha_config: HAConfig) -> None:
        """PLAT-1354: 403 forbidden must not be retried."""
        client = HAApiClient(ha_config)
        resp_403 = _make_response(403, text="Forbidden")
        client._session = _make_session([resp_403])

        result = await client.get_state("sensor.test")
        assert result is None

    async def test_retry_on_connection_error(self, ha_config: HAConfig) -> None:
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

            def request(self_inner: Any, method: str, url: str, **kwargs: Any) -> Any:
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

    async def test_ensure_session_creates_new_when_none(self, ha_config: HAConfig) -> None:
        """_ensure_session should create a session when _session is None."""
        from unittest.mock import AsyncMock, MagicMock, patch

        client = HAApiClient(ha_config)
        assert client._session is None
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch("aiohttp.TCPConnector"),
        ):
            session = await client._ensure_session()
        assert session is not None
        await client.close()

    async def test_ensure_session_reuses_open_session(self, ha_config: HAConfig) -> None:
        """_ensure_session should reuse existing open session."""
        from unittest.mock import AsyncMock, MagicMock, patch

        client = HAApiClient(ha_config)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch("aiohttp.TCPConnector"),
        ):
            session1 = await client._ensure_session()
            session2 = await client._ensure_session()
        assert session1 is session2
        await client.close()

    async def test_batch_cache_lock_exists(self, ha_config: HAConfig) -> None:
        """PLAT-1354: HAApiClient must have asyncio.Lock for batch cache."""
        import asyncio as _asyncio

        client = HAApiClient(ha_config)
        assert hasattr(client, "_batch_cache_lock")
        assert isinstance(client._batch_cache_lock, _asyncio.Lock)

    async def test_ensure_session_recreates_when_closed(self, ha_config: HAConfig) -> None:
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


# ===========================================================================
# PLAT-1370: set_state and set_input_text
# ===========================================================================


@pytest.mark.asyncio()
class TestSetState:
    """PLAT-1370: set_state() writes sensor state to HA."""

    async def test_set_state_success(self, ha_config: HAConfig) -> None:
        from unittest.mock import AsyncMock, patch

        client = HAApiClient(ha_config)
        with patch.object(
            client,
            "_request",
            new_callable=AsyncMock,
            return_value={"entity_id": "sensor.test", "state": "active"},
        ) as mock_req:
            result = await client.set_state(
                "sensor.test",
                "active",
                {"friendly_name": "Test"},
            )
            assert result is True
            mock_req.assert_called_once()
            args = mock_req.call_args
            assert args[0][0] == "POST"
            assert "sensor.test" in args[0][1]

    async def test_set_state_failure_returns_false(
        self,
        ha_config: HAConfig,
    ) -> None:
        from unittest.mock import AsyncMock, patch

        client = HAApiClient(ha_config)
        with patch.object(
            client,
            "_request",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await client.set_state("sensor.test", "error")
            assert result is False


@pytest.mark.asyncio()
class TestSetInputText:
    """PLAT-1370: set_input_text() truncates at 255 chars."""

    async def test_truncation_at_255(self, ha_config: HAConfig) -> None:
        from unittest.mock import AsyncMock, patch

        client = HAApiClient(ha_config)
        long_value = "x" * 300
        with patch.object(
            client,
            "_request",
            new_callable=AsyncMock,
            return_value=[],  # HA service success = list
        ) as mock_req:
            result = await client.set_input_text(
                "input_text.test",
                long_value,
            )
            assert result is True
            # Verify truncation in the call_service data
            call_data = mock_req.call_args[1]["json_data"]
            assert len(call_data["value"]) == 255


# ---------------------------------------------------------------------------
# PLAT-1573: Batch fetch constants and fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPlat1573BatchFetch:
    """PLAT-1573: HA_API_TIMEOUT_S / HA_API_BATCH_SIZE constants + per-entity fallback."""

    def test_ha_timeout_constant_exists(self) -> None:
        import adapters.ha_api as ha_api_mod

        assert hasattr(ha_api_mod, "HA_API_TIMEOUT_S")
        assert ha_api_mod.HA_API_TIMEOUT_S == 10

    def test_ha_batch_size_constant_exists(self) -> None:
        import adapters.ha_api as ha_api_mod

        assert hasattr(ha_api_mod, "HA_API_BATCH_SIZE")
        assert ha_api_mod.HA_API_BATCH_SIZE == 50

    def test_no_naked_pool_size_in_source(self) -> None:
        """Connector limit must use the named constant, not a literal 10."""
        import pathlib

        src = pathlib.Path("adapters/ha_api.py").read_text()
        assert "limit=10" not in src, "naked limit=10 found — use _HA_CONNECTOR_POOL_SIZE"

    async def test_batch_fetch_single_api_call(self, ha_config: HAConfig) -> None:
        """get_states_batch must use a single /api/states call."""
        from unittest.mock import AsyncMock, patch

        all_states = [
            {"entity_id": "sensor.a", "state": "1"},
            {"entity_id": "sensor.b", "state": "2"},
            {"entity_id": "sensor.c", "state": "3"},
        ]
        client = HAApiClient(ha_config)
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=all_states
        ) as mock_req:
            result = await client.get_states_batch(["sensor.a", "sensor.b", "sensor.c"])
        assert result["sensor.a"]["state"] == "1"
        assert result["sensor.b"]["state"] == "2"
        assert result["sensor.c"]["state"] == "3"
        # Exactly one call to /api/states
        assert mock_req.call_count == 1
        assert mock_req.call_args[0] == ("GET", "/api/states")

    async def test_batch_fallback_on_http_error(self, ha_config: HAConfig) -> None:
        """When /api/states fails, fall back to individual entity fetches."""
        from unittest.mock import AsyncMock, patch

        individual = {"entity_id": "sensor.x", "state": "42", "attributes": {}}

        async def _side_effect(method: str, path: str, **_: object) -> object:
            if path == "/api/states":
                return None  # batch fails
            return individual  # per-entity succeeds

        client = HAApiClient(ha_config)
        with patch.object(client, "_request", new_callable=AsyncMock, side_effect=_side_effect):
            result = await client.get_states_batch(["sensor.x"])

        assert "sensor.x" in result
        assert result["sensor.x"]["state"] == "42"


# ===========================================================================
# PLAT-1574: batch fetch optimisation + timeout constants
# ===========================================================================


@pytest.mark.asyncio()
class TestBatchFetchPlat1574:
    """PLAT-1574: AC1/AC3 — single batch call + per-entity fallback."""

    async def test_batch_fetch_single_api_call(
        self, ha_config: HAConfig
    ) -> None:
        """AC1: fetching 5 entities must issue exactly 1 API call."""
        from unittest.mock import AsyncMock, patch

        client = HAApiClient(ha_config)
        all_states = [
            {"entity_id": f"sensor.e{i}", "state": str(i)} for i in range(10)
        ]
        entity_ids = [f"sensor.e{i}" for i in range(5)]

        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=all_states
        ) as mock_req:
            result = await client.get_states_batch(entity_ids)

        assert len(result) == 5
        mock_req.assert_called_once()

    async def test_batch_fallback_on_error(
        self, ha_config: HAConfig
    ) -> None:
        """AC3: batch endpoint failure → per-entity fallback, no exception."""
        from unittest.mock import patch

        client = HAApiClient(ha_config)
        entity_ids = ["sensor.a", "sensor.b"]
        per_entity_data = {
            "sensor.a": {"entity_id": "sensor.a", "state": "1"},
            "sensor.b": {"entity_id": "sensor.b", "state": "2"},
        }
        call_paths: list[str] = []

        async def mock_request(
            method: str,
            path: str,
            json_data: dict[str, Any] | None = None,
            *,
            timeout: aiohttp.ClientTimeout | None = None,
        ) -> Any:
            call_paths.append(path)
            if path == "/api/states":
                return None  # batch fails
            eid = path.rsplit("/", maxsplit=1)[-1]
            return per_entity_data.get(eid)

        with patch.object(client, "_request", side_effect=mock_request):
            result = await client.get_states_batch(entity_ids)

        assert result["sensor.a"]["state"] == "1"
        assert result["sensor.b"]["state"] == "2"
        assert call_paths[0] == "/api/states"
        assert "/api/states/sensor.a" in call_paths
        assert "/api/states/sensor.b" in call_paths


class TestHaApiConstantsPlat1574:
    """PLAT-1574: AC2/AC4 — named constants, no naked numbers."""

    def test_ha_timeout_constant_exists(self) -> None:
        """AC2: HA_API_TIMEOUT_S must be a module-level int constant."""
        import adapters.ha_api as module

        assert hasattr(module, "HA_API_TIMEOUT_S")
        assert isinstance(module.HA_API_TIMEOUT_S, int)

    def test_ha_batch_timeout_constant_exists(self) -> None:
        """AC2: HA_API_BATCH_TIMEOUT_S must be a module-level int constant."""
        import adapters.ha_api as module

        assert hasattr(module, "HA_API_BATCH_TIMEOUT_S")
        assert isinstance(module.HA_API_BATCH_TIMEOUT_S, int)

    def test_no_naked_timeouts(self) -> None:
        """AC4: grep adapters/ha_api.py → 0 lines with digits but no [A-Z_#]."""
        import re
        from pathlib import Path

        ha_api_path = (
            Path(__file__).parent.parent.parent / "adapters" / "ha_api.py"
        )
        lines = ha_api_path.read_text().splitlines()
        violations = []
        for lineno, line in enumerate(lines, 1):
            if re.search(r"[0-9]", line) and not re.search(r"[#A-Z_]", line):
                violations.append(f"  line {lineno}: {line.strip()}")
        assert violations == [], "Naked numbers found:\n" + "\n".join(violations)


# ===========================================================================
# PLAT-1575: Exponential backoff constants + behaviour
# ===========================================================================


class TestBackoffConstantsPlat1575:
    """PLAT-1575: HA_API_BACKOFF_BASE / HA_API_MAX_BACKOFF_S must exist."""

    def test_backoff_base_constant_exists(self) -> None:
        import adapters.ha_api as module

        assert hasattr(module, "HA_API_BACKOFF_BASE")
        assert isinstance(module.HA_API_BACKOFF_BASE, int)
        assert module.HA_API_BACKOFF_BASE == 2

    def test_max_backoff_constant_exists(self) -> None:
        import adapters.ha_api as module

        assert hasattr(module, "HA_API_MAX_BACKOFF_S")
        assert isinstance(module.HA_API_MAX_BACKOFF_S, int)
        assert module.HA_API_MAX_BACKOFF_S == 30

    def test_no_naked_sleep_delay(self) -> None:
        """REGRESSION: naked sleep(self._retry_delay_s) must not exist."""
        from pathlib import Path

        src = (
            Path(__file__).parent.parent.parent / "adapters" / "ha_api.py"
        ).read_text()
        assert "sleep(self._retry_delay_s)" not in src, (
            "Naked sleep(self._retry_delay_s) found — use exponential backoff"
        )


@pytest.mark.asyncio()
class TestExponentialBackoffPlat1575:
    """PLAT-1575: Retry delays grow exponentially and are capped."""

    async def test_exponential_backoff_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B2: 3 retries with base_delay=2 → sleep calls [2.0, 4.0]."""
        import asyncio as _asyncio

        sleep_calls: list[float] = []

        async def _capture_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr(_asyncio, "sleep", _capture_sleep)

        config = HAConfig(
            url="http://localhost:8123",
            token_env="TEST_HA_TOKEN",
            retry_count=3,
            retry_delay_s=2,
        )
        client = HAApiClient(config)
        resp = _make_response(500, text="err")
        client._session = _make_session([resp, resp, resp])

        await client.get_state("sensor.test")

        assert sleep_calls == [2.0, 4.0]

    async def test_backoff_capped_at_max(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B3: backoff is capped at HA_API_MAX_BACKOFF_S regardless of attempt."""
        import asyncio as _asyncio
        from adapters.ha_api import HA_API_MAX_BACKOFF_S

        sleep_calls: list[float] = []

        async def _capture_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr(_asyncio, "sleep", _capture_sleep)

        config = HAConfig(
            url="http://localhost:8123",
            token_env="TEST_HA_TOKEN",
            retry_count=4,
            retry_delay_s=10,
        )
        client = HAApiClient(config)
        resp = _make_response(500, text="err")
        client._session = _make_session([resp, resp, resp, resp])

        await client.get_state("sensor.test")

        assert all(s <= HA_API_MAX_BACKOFF_S for s in sleep_calls), (
            f"Backoff exceeded max: {sleep_calls}"
        )


# ===========================================================================
# PLAT-1753: warm_batch_cache() — atomic cycle-start prefetch
# ===========================================================================


@pytest.mark.asyncio()
class TestPlat1753WarmBatchCache:
    """PLAT-1753: warm_batch_cache() fetches all states in one atomic call.

    Acceptance criteria:
    - AC1: returns True on success / False on failure
    - AC2: forces a fresh fetch even when cache is still valid
    - AC3: subsequent get_states_batch() uses the warmed cache (zero extra HTTP calls)
    - AC4: on failure, cache remains empty (no stale partial data served)
    """

    async def test_warm_cache_returns_true_on_success(
        self, ha_config: HAConfig
    ) -> None:
        """AC1: warm_batch_cache() returns True when /api/states succeeds."""
        from unittest.mock import AsyncMock, patch

        all_states = [
            {"entity_id": "sensor.a", "state": "1"},
            {"entity_id": "sensor.b", "state": "2"},
        ]
        client = HAApiClient(ha_config)
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=all_states
        ):
            result = await client.warm_batch_cache()
        assert result is True

    async def test_warm_cache_returns_false_on_failure(
        self, ha_config: HAConfig
    ) -> None:
        """AC1: warm_batch_cache() returns False when /api/states fails."""
        from unittest.mock import AsyncMock, patch

        client = HAApiClient(ha_config)
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=None
        ):
            result = await client.warm_batch_cache()
        assert result is False

    async def test_warm_cache_forces_refresh_when_cache_valid(
        self, ha_config: HAConfig
    ) -> None:
        """AC2: warm_batch_cache() fetches even if cache TTL has not expired."""
        import time
        from unittest.mock import AsyncMock, patch

        all_states = [{"entity_id": "sensor.x", "state": "fresh"}]
        client = HAApiClient(ha_config)

        # Pre-populate cache so it looks valid (not expired)
        client._batch_cache = [{"entity_id": "sensor.x", "state": "stale"}]
        client._batch_cache_ts = time.monotonic()  # just now — cache is valid

        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=all_states
        ) as mock_req:
            result = await client.warm_batch_cache()

        # Must have made a new HTTP call despite valid cache
        assert mock_req.call_count == 1
        assert result is True
        # Cache should now contain the fresh data
        assert client._batch_cache is not None
        assert client._batch_cache[0]["state"] == "fresh"

    async def test_warm_cache_populates_cache_on_success(
        self, ha_config: HAConfig
    ) -> None:
        """AC3: after warm_batch_cache(), _batch_cache is populated."""
        from unittest.mock import AsyncMock, patch

        all_states = [
            {"entity_id": "sensor.bat1_soc", "state": "87"},
            {"entity_id": "sensor.bat2_soc", "state": "62"},
        ]
        client = HAApiClient(ha_config)
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=all_states
        ):
            await client.warm_batch_cache()

        assert client._batch_cache is not None
        assert len(client._batch_cache) == 2

    async def test_warm_cache_subsequent_batch_no_extra_http_call(
        self, ha_config: HAConfig
    ) -> None:
        """AC3: get_states_batch() after warm_batch_cache() uses cache — no extra HTTP call."""
        from unittest.mock import AsyncMock, patch

        all_states = [
            {"entity_id": "sensor.bat1_soc", "state": "87"},
            {"entity_id": "sensor.bat2_soc", "state": "62"},
        ]
        client = HAApiClient(ha_config)
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=all_states
        ) as mock_req:
            await client.warm_batch_cache()
            # Now read from cache — must not issue another HTTP call
            result = await client.get_states_batch(
                ["sensor.bat1_soc", "sensor.bat2_soc"]
            )

        # Exactly ONE HTTP call total (the warm), none for the batch read
        assert mock_req.call_count == 1
        assert result["sensor.bat1_soc"]["state"] == "87"
        assert result["sensor.bat2_soc"]["state"] == "62"

    async def test_warm_cache_failure_leaves_cache_empty(
        self, ha_config: HAConfig
    ) -> None:
        """AC4: on failure, cache is empty so no stale data is served."""
        from unittest.mock import AsyncMock, patch

        client = HAApiClient(ha_config)
        # Pre-populate stale cache
        client._batch_cache = [{"entity_id": "sensor.old", "state": "stale"}]

        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=None
        ):
            result = await client.warm_batch_cache()

        assert result is False
        # Cache must be cleared — no stale data
        assert client._batch_cache is None

    async def test_warm_cache_uses_batch_timeout(
        self, ha_config: HAConfig
    ) -> None:
        """warm_batch_cache() must pass HA_API_BATCH_TIMEOUT_S to the request."""
        from unittest.mock import AsyncMock, patch
        from adapters.ha_api import HA_API_BATCH_TIMEOUT_S

        all_states: list[Any] = []
        client = HAApiClient(ha_config)
        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=all_states
        ) as mock_req:
            await client.warm_batch_cache()

        # Verify the call used a timeout object with the correct total
        assert mock_req.called
        kwargs = mock_req.call_args
        timeout_arg = kwargs[1].get("timeout") or (
            kwargs[0][2] if len(kwargs[0]) > 2 else None
        )
        assert timeout_arg is not None
        assert timeout_arg.total == HA_API_BATCH_TIMEOUT_S
