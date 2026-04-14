"""Home Assistant REST API client for CARMA Box.

Single long-lived aiohttp session with retry logic, batch state reading,
and health checks. This is the sole communication channel to all hardware.

All methods return None/False on failure — never raise to callers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

import aiohttp

from config.schema import HAConfig

logger = logging.getLogger(__name__)

# H5: Cache TTL — one fetch per control cycle (30 s interval).
# All adapters sharing the client will reuse this within the same cycle.
_DEFAULT_BATCH_CACHE_TTL_S: float = 25.0


class HAApiClient:
    """Async Home Assistant REST API client.

    Features:
    - Long-lived aiohttp session (reused across calls)
    - Configurable retry with delay on connection errors and timeouts
    - Bearer token from environment variable (never hardcoded)
    - Batch state reading with client-side filtering
    - All public methods catch exceptions and return safe defaults
    """

    def __init__(self, config: HAConfig) -> None:
        self._config = config
        self._base_url = config.url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=config.timeout_s)
        self._retry_count = config.retry_count
        self._retry_delay_s = config.retry_delay_s
        self._batch_cache_ttl_s = getattr(
            config, "batch_cache_ttl_s", _DEFAULT_BATCH_CACHE_TTL_S,
        )
        self._input_text_max_len = getattr(config, "input_text_max_len", 255)
        self._session: Optional[aiohttp.ClientSession] = None

        # Resolve token from environment
        token = os.environ.get(config.token_env, "")
        if not token:
            logger.warning(
                "HA token environment variable %s is empty", config.token_env
            )
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # H5: Per-cycle batch cache — avoids fetching all ~2000 HA entities
        # multiple times when several adapters call get_states_batch() in one cycle.
        # PLAT-1354: asyncio.Lock prevents concurrent refreshes from racing.
        self._batch_cache: Optional[list[Any]] = None
        self._batch_cache_ts: float = 0.0
        self._batch_cache_lock: asyncio.Lock = asyncio.Lock()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Create or return the long-lived session."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                ssl=self._config.verify_ssl,
                limit=10,
            )
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                timeout=self._timeout,
                connector=connector,
            )
        return self._session

    # PLAT-1354: status codes that must never be retried
    _NO_RETRY_STATUSES: frozenset[int] = frozenset({401, 403, 404})

    async def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[dict[str, Any]] = None,
    ) -> Optional[Any]:
        """Execute an HTTP request with retry logic.

        Returns parsed JSON on success, None on failure after all retries.

        PLAT-1354: 401/403 auth errors are never retried — retrying won't fix
        a bad token. 404 is also never retried (entity doesn't exist).
        """
        url = f"{self._base_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(1, self._retry_count + 1):
            try:
                session = await self._ensure_session()
                async with session.request(
                    method, url, json=json_data
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    # PLAT-1354: auth errors and 404 must never be retried
                    if resp.status in self._NO_RETRY_STATUSES:
                        if resp.status in (401, 403):
                            logger.error(
                                "HA API %s %s auth error %d — check token (not retrying)",
                                method, path, resp.status,
                            )
                        else:
                            logger.debug("404 for %s", path)
                        return None
                    # Server error — retry
                    body = await resp.text()
                    last_error = aiohttp.ClientResponseError(
                        request_info=resp.request_info,
                        history=resp.history,
                        status=resp.status,
                        message=body[:200],
                    )
                    logger.warning(
                        "HA API %s %s returned %d (attempt %d/%d)",
                        method,
                        path,
                        resp.status,
                        attempt,
                        self._retry_count,
                    )
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
            ) as exc:
                last_error = exc
                logger.warning(
                    "HA API %s %s failed: %s (attempt %d/%d)",
                    method,
                    path,
                    exc,
                    attempt,
                    self._retry_count,
                )

            if attempt < self._retry_count:
                await asyncio.sleep(self._retry_delay_s)

        logger.error(
            "HA API %s %s failed after %d retries: %s",
            method,
            path,
            self._retry_count,
            last_error,
        )
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_state(self, entity_id: str) -> Optional[str]:
        """Get the state value of a single entity.

        Returns the state string, or None if the entity doesn't exist
        or the request fails.
        """
        data = await self._request("GET", f"/api/states/{entity_id}")
        if data is None:
            return None
        state: str = data.get("state", "")
        if state in ("unavailable", "unknown"):
            logger.debug("Entity %s is %s", entity_id, state)
            return None
        return state

    async def get_state_with_attributes(
        self, entity_id: str
    ) -> Optional[dict[str, Any]]:
        """Get full state dict (state + attributes) for a single entity.

        Returns {"state": "...", "attributes": {...}, ...} or None.
        """
        data = await self._request("GET", f"/api/states/{entity_id}")
        if data is None:
            return None
        result: dict[str, Any] = data
        return result

    async def get_states_batch(
        self, entity_ids: list[str]
    ) -> dict[str, Any]:
        """Read multiple entity states in a single API call.

        Fetches all states from /api/states and filters client-side
        to only return the requested entities.

        H5: The full-state response is cached for _BATCH_CACHE_TTL_S seconds
        so that multiple adapters calling this within the same 30-second cycle
        share one HTTP round-trip instead of each issuing their own request.

        Returns a dict mapping entity_id -> full state dict.
        Missing entities are omitted from the result.
        """
        if not entity_ids:
            return {}

        # PLAT-1354: Lock prevents concurrent coroutines from issuing duplicate
        # /api/states fetches when the cache has expired simultaneously.
        async with self._batch_cache_lock:
            now = time.monotonic()
            age = now - self._batch_cache_ts
            if self._batch_cache is None or age >= self._batch_cache_ttl_s:
                all_states = await self._request("GET", "/api/states")
                if all_states is None:
                    return {}
                self._batch_cache = all_states
                self._batch_cache_ts = now
                logger.debug("H5: batch cache refreshed (age=%.1fs)", age)
            else:
                logger.debug("H5: batch cache hit (age=%.1fs)", age)
                all_states = self._batch_cache

        wanted = set(entity_ids)
        result: dict[str, Any] = {}
        for state_obj in all_states:
            eid = state_obj.get("entity_id", "")
            if eid in wanted:
                result[eid] = state_obj
        return result

    def invalidate_batch_cache(self) -> None:
        """Force the next get_states_batch() call to fetch fresh data.

        Call this after writing state to HA so the cache does not serve
        stale values within the same cycle.
        """
        self._batch_cache = None
        self._batch_cache_ts = 0.0

    async def call_service(
        self,
        domain: str,
        service: str,
        data: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Call a Home Assistant service.

        Returns True on success (HA returned a list of affected states),
        False on failure after retries or when HA returns an error dict.
        Never raises.

        PLAT-1354: A successful HA service call returns a JSON list of states.
        An error response is a JSON object (dict) with a "message" field.
        Returning True for any non-None response was a bug — we now check the
        response type to detect HA-level errors (e.g. unknown service).
        """
        path = f"/api/services/{domain}/{service}"
        result = await self._request("POST", path, json_data=data or {})
        if result is None:
            return False
        # HA success: list of state objects. HA error: {"message": "..."}.
        if isinstance(result, dict) and "message" in result:
            logger.warning(
                "HA service %s/%s returned error: %s",
                domain, service, result.get("message", ""),
            )
            return False
        return True

    async def set_state(
        self,
        entity_id: str,
        state: str,
        attributes: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Set a sensor/entity state via POST /api/states/{entity_id}.

        Used for dashboard write-back: plan sensors, rules, decision reason.
        Creates the entity if it doesn't exist.
        Returns True on success, False on failure.
        """
        path = f"/api/states/{entity_id}"
        payload: dict[str, Any] = {"state": state}
        if attributes:
            payload["attributes"] = attributes
        result = await self._request("POST", path, json_data=payload)
        if result is None:
            return False
        self.invalidate_batch_cache()
        return True

    async def set_input_text(
        self,
        entity_id: str,
        value: str,
    ) -> bool:
        """Set an input_text helper value via service call.

        Used for plan text fields (v6_battery_plan_today etc).
        Truncates to 255 chars (HA input_text limit).
        """
        return await self.call_service(
            "input_text", "set_value",
            {"entity_id": entity_id, "value": value[:self._input_text_max_len]},
        )

    async def health_check(self) -> bool:
        """Check if Home Assistant is reachable.

        Returns True if /api/ responds with 200, False otherwise.
        """
        try:
            session = await self._ensure_session()
            url = f"{self._base_url}/api/"
            async with session.get(url) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
