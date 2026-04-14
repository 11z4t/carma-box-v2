"""Easee EV Charger Adapter for CARMA Box.

Handles all Easee-specific logic:
- charger_id (NOT device_id) for all service calls (B4)
- set_charger_dynamic_limit for current control (never max_limit — FLASH wear)
- waiting_in_fully auto-fix: override_schedule + toggle (B3)
- smart_charging guard: enforce OFF
- max_charger_current floor at 10A

Communicates via HA REST API through HAApiClient.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from adapters.base import EVChargerAdapter
from adapters.ha_api import HAApiClient
from config.schema import EVChargerConfig

logger = logging.getLogger(__name__)


class EaseeAdapter(EVChargerAdapter):
    """Easee Home charger adapter.

    Entity IDs and charger_id come from config — zero hardcoding.
    """

    def __init__(self, ha_api: HAApiClient, config: EVChargerConfig) -> None:
        self._api = ha_api
        self._config = config
        self._entities = config.entities
        self._fix_in_progress = False
        self._fix_task: Optional[asyncio.Task[None]] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def charger_id(self) -> str:
        return self._config.charger_id

    @property
    def max_amps(self) -> int:
        return self._config.max_amps

    @property
    def min_amps(self) -> int:
        return self._config.min_amps

    @property
    def phases(self) -> int:
        return self._config.phases

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    async def get_status(self) -> str:
        state = await self._api.get_state(self._entities.status)
        return state or "unknown"

    async def get_power(self) -> float:
        state = await self._api.get_state(self._entities.power)
        if state is None:
            return 0.0
        try:
            # Easee reports power in kW, convert to W
            return float(state) * 1000.0
        except (ValueError, TypeError):
            return 0.0

    async def get_current(self) -> float:
        state = await self._api.get_state(self._entities.current)
        if state is None:
            return 0.0
        try:
            return float(state)
        except (ValueError, TypeError):
            return 0.0

    async def is_connected(self) -> bool:
        status = await self.get_status()
        # Easee statuses indicating cable connected
        connected_statuses = {
            "awaiting_start", "charging", "completed",
            "ready_to_charge", "awaiting_authentication",
        }
        return status.lower() in connected_statuses

    async def get_reason_for_no_current(self) -> Optional[str]:
        state = await self._api.get_state(self._entities.reason_for_no_current)
        if state and state not in ("undefined", "none", ""):
            return state
        return None

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    async def set_current(self, amps: int) -> bool:
        """Set charging current via dynamicChargerCurrent.

        B4: Uses charger_id (NOT device_id).
        Never uses set_charger_max_limit (FLASH wear).
        """
        amps = max(self.min_amps, min(amps, self.max_amps))
        logger.info(
            "Setting EV current → %dA (charger_id=%s)",
            amps, self.charger_id,
        )
        return await self._api.call_service(
            "easee", "set_charger_dynamic_limit",
            {
                "charger_id": self.charger_id,
                "current": amps,
            },
        )

    async def start_charging(self) -> bool:
        """Enable charging by turning on the is_enabled switch."""
        logger.info("Starting EV charging")
        return await self._api.call_service(
            "switch", "turn_on",
            {"entity_id": self._entities.enabled},
        )

    async def stop_charging(self) -> bool:
        """Disable charging by turning off the is_enabled switch."""
        logger.info("Stopping EV charging")
        return await self._api.call_service(
            "switch", "turn_off",
            {"entity_id": self._entities.enabled},
        )

    async def fix_waiting_in_fully(self) -> bool:
        """Fix stuck charger state (B3: Easee reason 51).

        3-step sequence:
          1. Turn OFF is_enabled (10s wait)
          2. Press override_schedule button (5s wait)
          3. Turn ON is_enabled + set 6A

        PLAT-1355: This method intentionally blocks for 18 seconds total
        (10s + 5s + 3s) because the Easee firmware requires these delays
        between state transitions — firing steps without waiting causes the
        charger to ignore the subsequent commands. The sleeps are excluded
        from coverage (pragma: no cover) since they cannot be meaningfully
        tested without a real Easee device.

        PLAT-1408: Non-blocking. Spawns background task for 18s sequence.
        Returns True if spawned, False if already in progress.
        """
        if self._fix_in_progress:
            logger.info("waiting_in_fully fix already in progress")
            return False
        self._fix_in_progress = True
        self._fix_task = asyncio.create_task(self._run_fix_sequence())
        self._fix_task.add_done_callback(self._on_fix_done)
        return True

    def _on_fix_done(self, task: asyncio.Task[None]) -> None:
        """Callback when fix task completes — log exceptions."""
        self._fix_task = None
        if task.cancelled():
            logger.info("waiting_in_fully fix task cancelled")
        elif task.exception():
            logger.error(
                "waiting_in_fully fix task failed: %s", task.exception(),
            )

    async def _run_fix_sequence(self) -> None:
        """Background: 3-step fix (OFF, override, ON+6A). 18s total."""
        try:
            logger.warning("Attempting waiting_in_fully fix (B3)")
            await self._api.call_service(
                "switch", "turn_off",
                {"entity_id": self._entities.enabled},
            )
            await asyncio.sleep(self._config.easee.fix_off_delay_s)  # pragma: no cover

            override_entity = self._entities.override_schedule
            if override_entity:
                await self._api.call_service(
                    "button", "press",
                    {"entity_id": override_entity},
                )
                await asyncio.sleep(self._config.easee.fix_override_delay_s)  # pragma: no cover

            await self._api.call_service(
                "switch", "turn_on",
                {"entity_id": self._entities.enabled},
            )
            await asyncio.sleep(self._config.easee.fix_on_delay_s)  # pragma: no cover
            await self.set_current(self.min_amps)
            logger.info("waiting_in_fully fix sequence complete")
        except Exception as exc:
            logger.error("waiting_in_fully fix failed: %s", exc)
        finally:
            self._fix_in_progress = False

    async def enforce_smart_charging_off(self) -> bool:
        """Guard: ensure smart_charging is OFF.

        PLAT-1355: Previously a no-op. Now reads the smart_charging entity
        state (if configured) and calls the service to turn it off when ON.
        Easee smart_charging interferes with CARMA Box current control.
        Called during health checks.

        Returns True if smart_charging is already OFF or successfully turned OFF.
        Returns False if the service call to turn it off failed.
        """
        smart_charging_entity = self._entities.smart_charging
        if not smart_charging_entity:
            logger.debug(
                "smart_charging guard: no entity configured, skipping check"
            )
            return True

        state = await self._api.get_state(smart_charging_entity)
        if state != "on":
            # Already OFF (or unavailable/unknown — safe default)
            logger.debug("smart_charging guard: already OFF (state=%s)", state)
            return True

        logger.warning(
            "smart_charging is ON — turning off to restore CARMA Box control"
        )
        success = await self._api.call_service(
            "switch", "turn_off",
            {"entity_id": smart_charging_entity},
        )
        if not success:
            logger.error(
                "smart_charging guard: failed to turn off %s", smart_charging_entity
            )
        return success
