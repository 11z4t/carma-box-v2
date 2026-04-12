"""Abstract base classes for hardware adapters.

All hardware communication goes through adapters that implement these
interfaces. The decision engine and guards never talk to hardware directly.

Implementations: GoodWeAdapter, EaseeAdapter, ShellyAdapter, etc.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class AdapterReading:
    """A single sensor reading from any adapter."""

    entity_id: str
    value: Any
    timestamp: datetime
    unit: Optional[str] = None
    attributes: Optional[dict[str, Any]] = None


class InverterAdapter(ABC):
    """Abstract interface for battery inverter adapters.

    Implementations: GoodWeAdapter, HuaweiAdapter, SolarEdgeAdapter.
    Two instances per site when there are two inverters (Kontor + Forrad).
    """

    @abstractmethod
    async def get_battery_soc(self) -> float:
        """Get current battery State of Charge (%).

        Returns:
            SoC percentage (0.0 - 100.0). Returns 0.0 if unavailable.
        """
        ...

    @abstractmethod
    async def get_battery_power(self) -> float:
        """Get current battery power (W).

        Returns:
            Watts. Positive = discharge, negative = charge.
        """
        ...

    @abstractmethod
    async def get_cell_temperature(self) -> float:
        """Get minimum battery cell temperature (Celsius).

        Returns:
            Temperature in Celsius. Returns 20.0 if unavailable (safe default).
        """
        ...

    @abstractmethod
    async def get_pv_power(self) -> float:
        """Get current PV production (W).

        Returns:
            Watts (always >= 0).
        """
        ...

    @abstractmethod
    async def get_grid_power(self) -> float:
        """Get grid power at CT clamp (W).

        Returns:
            Watts. Positive = import, negative = export.
        """
        ...

    @abstractmethod
    async def get_load_power(self) -> float:
        """Get load power at CT clamp (W).

        Returns:
            Watts (always >= 0).
        """
        ...

    @abstractmethod
    async def get_ems_mode(self) -> str:
        """Get current EMS mode string.

        Returns:
            One of: 'charge_pv', 'discharge_pv', 'battery_standby',
            'import_ac', 'export_ac', 'conserve', 'auto'.
        """
        ...

    @abstractmethod
    async def set_ems_mode(self, mode: str) -> bool:
        """Set EMS mode.

        IMPORTANT: Does NOT touch fast_charging. Caller must handle
        fast_charging separately (INV-3 / B7).

        Args:
            mode: Target EMS mode string.

        Returns:
            True if the service call succeeded.
        """
        ...

    @abstractmethod
    async def get_ems_power_limit(self) -> int:
        """Get current EMS power limit (W).

        Returns:
            Watts. 0 = no limit.
        """
        ...

    @abstractmethod
    async def set_ems_power_limit(self, watts: int) -> bool:
        """Set EMS power limit (W).

        IMPORTANT: Sending 0 must actually write 0 to the entity
        (not be skipped by truthy-trap). Guard G0 depends on this.

        Args:
            watts: Target power limit in watts (0 = no limit).

        Returns:
            True if the service call succeeded.
        """
        ...

    @abstractmethod
    async def get_fast_charging(self) -> bool:
        """Get fast charging switch state.

        Returns:
            True if fast charging is ON.
        """
        ...

    @abstractmethod
    async def set_fast_charging(self, on: bool) -> bool:
        """Set fast charging switch.

        MUST be OFF before any discharge_pv command (INV-3 / B7).

        Args:
            on: Target state.

        Returns:
            True if the service call succeeded.
        """
        ...

    @abstractmethod
    async def get_soh(self) -> float:
        """Get State of Health (%).

        Returns:
            SoH percentage (0.0 - 100.0). Returns 100.0 if unavailable.
        """
        ...

    @abstractmethod
    async def get_all_readings(self) -> list[AdapterReading]:
        """Get all readings in one batch call (for efficiency).

        Returns:
            List of AdapterReading for all monitored entities.
        """
        ...

    @property
    @abstractmethod
    def battery_id(self) -> str:
        """Unique battery identifier (e.g. 'kontor', 'forrad')."""
        ...

    @property
    @abstractmethod
    def capacity_kwh(self) -> float:
        """Battery capacity in kWh."""
        ...

    @property
    @abstractmethod
    def ct_placement(self) -> str:
        """CT clamp placement: 'local_load' or 'house_grid'."""
        ...


class EVChargerAdapter(ABC):
    """Abstract interface for EV charger adapters.

    Implementations: EaseeAdapter, ZaptecAdapter, WallboxAdapter.
    """

    @abstractmethod
    async def get_status(self) -> str:
        """Get charger status string.

        Returns:
            Charger-specific status (e.g. 'charging', 'awaiting_start').
        """
        ...

    @abstractmethod
    async def get_power(self) -> float:
        """Get current charging power (W).

        Returns:
            Watts (always >= 0).
        """
        ...

    @abstractmethod
    async def get_current(self) -> float:
        """Get current charging amperage (A).

        Returns:
            Amps (always >= 0).
        """
        ...

    @abstractmethod
    async def is_connected(self) -> bool:
        """Is an EV connected (cable plugged in)?

        Returns:
            True if cable is connected.
        """
        ...

    @abstractmethod
    async def set_current(self, amps: int) -> bool:
        """Set charging current (A).

        Uses dynamicChargerCurrent (never static values to avoid flash wear).

        Args:
            amps: Target current (must be >= min_amps and <= max_amps).

        Returns:
            True if the service call succeeded.
        """
        ...

    @abstractmethod
    async def start_charging(self) -> bool:
        """Enable charging.

        Returns:
            True if the service call succeeded.
        """
        ...

    @abstractmethod
    async def stop_charging(self) -> bool:
        """Disable charging.

        Returns:
            True if the service call succeeded.
        """
        ...

    @abstractmethod
    async def get_reason_for_no_current(self) -> Optional[str]:
        """Get reason why charger is not providing current.

        Returns:
            Reason string or None if charging normally.
        """
        ...

    @abstractmethod
    async def fix_waiting_in_fully(self) -> bool:
        """Fix stuck charger state (B3: Easee reason 51).

        Performs: override_schedule + toggle after configured delay.

        Returns:
            True if fix was attempted.
        """
        ...

    @property
    @abstractmethod
    def charger_id(self) -> str:
        """Unique charger identifier (e.g. 'EH128405')."""
        ...

    @property
    @abstractmethod
    def max_amps(self) -> int:
        """Maximum charging current (A)."""
        ...

    @property
    @abstractmethod
    def min_amps(self) -> int:
        """Minimum charging current (A)."""
        ...

    @property
    @abstractmethod
    def phases(self) -> int:
        """Number of phases (1 or 3)."""
        ...


class LoadAdapter(ABC):
    """Abstract interface for controllable load adapters.

    Implementations: ShellyAdapter (ON/OFF), ClimateAdapter (setpoint).
    """

    @abstractmethod
    async def get_power(self) -> float:
        """Get current power draw (W).

        Returns:
            Watts (always >= 0).
        """
        ...

    @abstractmethod
    async def is_active(self) -> bool:
        """Is the load currently active?

        Returns:
            True if the load is drawing power.
        """
        ...

    @abstractmethod
    async def turn_on(self) -> bool:
        """Turn on the load.

        Returns:
            True if the service call succeeded.
        """
        ...

    @abstractmethod
    async def turn_off(self) -> bool:
        """Turn off the load.

        Returns:
            True if the service call succeeded.
        """
        ...

    @abstractmethod
    async def set_power(self, watts: int) -> bool:
        """Set target power (for variable loads).

        No-op for on_off loads.

        Args:
            watts: Target power in watts.

        Returns:
            True if the service call succeeded.
        """
        ...

    @property
    @abstractmethod
    def load_id(self) -> str:
        """Unique load identifier (e.g. 'miner', 'vp_kontor')."""
        ...

    @property
    @abstractmethod
    def load_type(self) -> str:
        """Load type: 'on_off', 'variable', or 'climate'."""
        ...
