"""GoodWe ET inverter adapter for CARMA Box.

Handles all GoodWe-specific quirks:
- Truthy-trap: ems_power_limit=0 must actually write 0 (not be skipped)
- fast_charging is caller's responsibility — set_ems_mode NEVER touches it (INV-3/B7)
- CT placement awareness: local_load (Kontor) vs house_grid (Förråd)
- Batch reading via get_all_readings() for efficiency
- All reads return safe defaults when sensors are unavailable

Two instances per Sanduddsvagen 60: Kontor (15 kWh) and Förråd (5 kWh).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from adapters.base import AdapterReading, InverterAdapter
from adapters.ha_api import HAApiClient
from adapters.service_map import InverterServiceMap
from config.schema import BatteryConfig
from core.models import CTPlacement

logger = logging.getLogger(__name__)

# Safe defaults for unavailable sensors.
_CELL_TEMP_DEFAULT_C: float = 20.0
_SOH_DEFAULT_PCT: float = 100.0

# EMS mode strings as used by the GoodWe HACS integration
# Allowed EMS modes — "auto" intentionally excluded (B10/B14)
# PLAT-1714: charge_battery (mode 11) + discharge_battery added — RESPECT ems_power_limit
# (charge_pv in peak_shaving is UNCONTROLLABLE; charge_battery is correct for PV surplus)
_VALID_EMS_MODES = frozenset({
    "charge_pv",
    "discharge_pv",
    "battery_standby",
    "charge_battery",
    "discharge_battery",
    "import_ac",
    "export_ac",
    "conserve",
})


class GoodWeAdapter(InverterAdapter):
    """GoodWe ET series inverter adapter.

    Communicates exclusively via Home Assistant REST API.
    Entity IDs come from BatteryConfig — zero hardcoding.
    """

    def __init__(
        self,
        ha_api: HAApiClient,
        config: BatteryConfig,
        services: InverterServiceMap | None = None,
    ) -> None:
        self._api = ha_api
        self._config = config
        self._svc = services or InverterServiceMap()
        self._entities = config.entities
        self._log = logger.getChild(config.id)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def battery_id(self) -> str:
        return self._config.id

    @property
    def capacity_kwh(self) -> float:
        return self._config.cap_kwh

    @property
    def ct_placement(self) -> CTPlacement:
        return self._config.ct_placement

    @property
    def device_id(self) -> str:
        """GoodWe device ID for service calls requiring it."""
        return self._config.goodwe_device_id

    # ------------------------------------------------------------------
    # Read methods — all return safe defaults on failure
    # ------------------------------------------------------------------

    async def _read_float(
        self, entity_id: str, default: float = 0.0
    ) -> float:
        """Read a numeric entity as float, returning default if unavailable."""
        if not entity_id:
            return default
        state = await self._api.get_state(entity_id)
        if state is None:
            return default
        try:
            return float(state)
        except (ValueError, TypeError):
            self._log.warning(
                "Cannot parse %s=%r as float, using default %.1f",
                entity_id, state, default,
            )
            return default

    async def _read_bool(self, entity_id: str) -> bool:
        """Read a switch entity as bool."""
        if not entity_id:
            return False
        state = await self._api.get_state(entity_id)
        return state == "on"

    async def get_battery_soc(self) -> float:
        """Get battery SoC (0.0-100.0). Returns 0.0 if unavailable."""
        return await self._read_float(self._entities.soc, default=0.0)

    async def get_battery_power(self) -> float:
        """Get battery power (W). Positive=discharge, negative=charge."""
        return await self._read_float(self._entities.power, default=0.0)

    async def get_cell_temperature(self) -> float:
        """Get battery cell temperature (°C). Returns 20.0 if unavailable."""
        return await self._read_float(self._entities.cell_temp, default=_CELL_TEMP_DEFAULT_C)

    async def get_pv_power(self) -> float:
        """Get PV production (W, always >= 0)."""
        return max(0.0, await self._read_float(self._entities.pv_power))

    async def get_grid_power(self) -> float:
        """Get grid power at CT (W). Positive=import, negative=export."""
        return await self._read_float(self._entities.grid_power, default=0.0)

    async def get_load_power(self) -> float:
        """Get load power (W, always >= 0)."""
        return max(0.0, await self._read_float(self._entities.load_power))

    async def get_ems_mode(self) -> str:
        """Get current EMS mode string."""
        if not self._entities.ems_mode:
            return "battery_standby"
        state = await self._api.get_state(self._entities.ems_mode)
        if state is None:
            return "battery_standby"
        return state

    async def get_ems_power_limit(self) -> int:
        """Get EMS power limit (W). 0 = no limit."""
        value = await self._read_float(self._entities.ems_power_limit)
        return int(value)

    async def get_fast_charging(self) -> bool:
        """Get fast charging switch state."""
        return await self._read_bool(self._entities.fast_charging)

    async def get_soh(self) -> float:
        """Get State of Health (%). Returns 100.0 if unavailable."""
        return await self._read_float(self._entities.soh, default=_SOH_DEFAULT_PCT)

    async def get_export_limit(self) -> int:
        """Get grid export limit (W)."""
        return int(await self._read_float(self._entities.export_limit))

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    async def set_ems_mode(self, mode: str) -> bool:
        """Set EMS mode via HA select entity.

        IMPORTANT: Does NOT touch fast_charging. The caller MUST ensure
        fast_charging is OFF before setting discharge_pv (INV-3/B7).

        Returns True if the service call succeeded.
        """
        if mode not in _VALID_EMS_MODES:
            self._log.error(
                "Unknown EMS mode '%s' for %s", mode, self.battery_id,
            )
            return False

        self._log.info("Setting EMS mode → %s on %s", mode, self.battery_id)
        svc = self._svc.set_mode
        return await self._api.call_service(
            svc.domain, svc.service,
            {
                "entity_id": self._entities.ems_mode,
                "option": mode,
            },
        )

    async def set_ems_power_limit(self, watts: int) -> bool:
        """Set EMS power limit (W).

        CRITICAL: This method ALWAYS writes the value, even when watts=0.
        The GoodWe firmware has a truthy-trap where 0 might be skipped
        if not explicitly sent. Guard G0 depends on this being reliable.

        Uses number.set_value service (not goodwe lib) to write directly
        to the HA number entity (B9).
        """
        self._log.info(
            "Setting EMS power limit → %d W on %s", watts, self.battery_id,
        )
        # ALWAYS send the value — even 0. This is the truthy-trap defense.
        svc = self._svc.set_power_limit
        return await self._api.call_service(
            svc.domain, svc.service,
            {
                "entity_id": self._entities.ems_power_limit,
                "value": watts,  # Explicitly send 0, never skip
            },
        )

    async def set_fast_charging(self, on: bool) -> bool:
        """Set fast charging switch.

        MUST be OFF before any discharge_pv command (INV-3/B7).
        """
        svc = self._svc.set_fast_charging_on if on else self._svc.set_fast_charging_off
        self._log.info(
            "Setting fast_charging → %s on %s", svc.service, self.battery_id,
        )
        return await self._api.call_service(
            svc.domain, svc.service,
            {"entity_id": self._entities.fast_charging},
        )

    async def set_export_limit(self, watts: int) -> bool:
        """Set grid export limit (W)."""
        self._log.info(
            "Setting export limit → %d W on %s", watts, self.battery_id,
        )
        svc = self._svc.set_export_limit
        return await self._api.call_service(
            svc.domain, svc.service,
            {
                "entity_id": self._entities.export_limit,
                "value": watts,
            },
        )

    # ------------------------------------------------------------------
    # Batch reading
    # ------------------------------------------------------------------

    async def get_all_readings(self) -> list[AdapterReading]:
        """Get all sensor readings in one batch API call.

        Returns a list of AdapterReading for all configured entities.
        Missing or unavailable entities are excluded.
        """
        entity_ids = [
            self._entities.soc,
            self._entities.power,
            self._entities.cell_temp,
            self._entities.pv_power,
            self._entities.grid_power,
            self._entities.load_power,
            self._entities.ems_mode,
            self._entities.ems_power_limit,
            self._entities.fast_charging,
            self._entities.soh,
        ]
        # Filter out empty/unconfigured entity IDs
        entity_ids = [eid for eid in entity_ids if eid]

        batch = await self._api.get_states_batch(entity_ids)
        now = datetime.now(tz=timezone.utc)

        readings: list[AdapterReading] = []
        for eid, state_obj in batch.items():
            state_val = state_obj.get("state")
            if state_val in (None, "unavailable", "unknown"):
                continue
            attrs = state_obj.get("attributes", {})
            readings.append(AdapterReading(
                entity_id=eid,
                value=state_val,
                timestamp=now,
                unit=attrs.get("unit_of_measurement"),
                attributes=attrs,
            ))

        return readings
