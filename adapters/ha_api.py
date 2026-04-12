"""Home Assistant REST API client for CARMA Box.

Single long-lived aiohttp session with retry logic, batch state reading,
and health checks. This is the sole communication channel to all hardware.

All methods return None/False on failure — never raise to callers.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import aiohttp

from config.schema import HAConfig

logger = logging.getLogger(__name__)


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

    async def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[dict[str, Any]] = None,
    ) -> Optional[Any]:
        """Execute an HTTP request with retry logic.

        Returns parsed JSON on success, None on failure after all retries.
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
                    if resp.status == 404:
                        # Entity not found — no point retrying
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

        Returns a dict mapping entity_id -> full state dict.
        Missing entities are omitted from the result.
        """
        if not entity_ids:
            return {}

        all_states = await self._request("GET", "/api/states")
        if all_states is None:
            return {}

        wanted = set(entity_ids)
        result: dict[str, Any] = {}
        for state_obj in all_states:
            eid = state_obj.get("entity_id", "")
            if eid in wanted:
                result[eid] = state_obj
        return result

    async def call_service(
        self,
        domain: str,
        service: str,
        data: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Call a Home Assistant service.

        Returns True on success, False on failure after retries.
        Never raises.
        """
        path = f"/api/services/{domain}/{service}"
        result = await self._request("POST", path, json_data=data or {})
        if result is None:
            return False
        return True

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
