"""Day Plan models for CARMA Box PV Surplus Optimizer.

Pure data models — no side effects, no I/O.
HourSlot represents a single hour's allocation of PV surplus.
DayPlan aggregates HourSlots with energy balance invariant.

PLAT-1629: Part of EPIC PLAT-1618 (PV Surplus Optimizer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tolerance for energy balance validation (W).
# Allows ±1W rounding error in the invariant:
# pv_forecast_w ≈ bat_alloc_w + ev_alloc_w + dispatch_alloc_w
#                 + house_load_w + expected_export_w
_ENERGY_BALANCE_TOLERANCE_W: float = 1.0

# Watts-to-kilowatts conversion factor.
_W_TO_KW: float = 1000.0


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


@unique
class BatMode(str, Enum):
    """Battery mode allocation for a single hour in the day plan.

    These are abstract planning modes, not hardware-specific EMS modes.
    The engine translates BatMode to concrete EMSMode commands.
    """

    CHARGE = "charge"
    DISCHARGE = "discharge"
    STANDBY = "standby"


# ---------------------------------------------------------------------------
# Hourly Forecast (from Solcast adapter)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HourlyForecast:
    """PV forecast for a single hour with confidence bands.

    p10 <= p50 <= p90 invariant enforced at construction.
    All values in kWh for the given hour.
    """

    p10_kwh: float = 0.0
    p50_kwh: float = 0.0
    p90_kwh: float = 0.0

    def __post_init__(self) -> None:
        if self.p10_kwh > self.p50_kwh:
            # Clamp bad data: p10 must not exceed p50
            object.__setattr__(self, "p10_kwh", self.p50_kwh)
        if self.p90_kwh < self.p50_kwh:
            # Clamp bad data: p90 must not be below p50
            object.__setattr__(self, "p90_kwh", self.p50_kwh)


# Sentinel for missing hours — always returns 0 kWh.
ZERO_HOURLY_FORECAST = HourlyForecast(p10_kwh=0.0, p50_kwh=0.0, p90_kwh=0.0)


# ---------------------------------------------------------------------------
# Hour Slot — single hour allocation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HourSlot:
    """Allocation plan for a single hour.

    Energy balance invariant enforced at construction:
    pv_forecast_w ≈ bat_alloc_w + ev_alloc_w + dispatch_alloc_w
                    + house_load_w + expected_export_w
    (within _ENERGY_BALANCE_TOLERANCE_W)
    """

    hour: int
    pv_forecast_w: float
    house_load_w: float
    bat_alloc_w: float
    ev_alloc_w: float
    dispatch_alloc_w: float
    expected_export_w: float
    bat_mode: BatMode
    ev_amps: int = 0
    dispatch_devices: tuple[str, ...] = ()
    projected_bat_soc_pct: float = 0.0

    def __post_init__(self) -> None:
        consumed = (
            self.bat_alloc_w
            + self.ev_alloc_w
            + self.dispatch_alloc_w
            + self.house_load_w
            + self.expected_export_w
        )
        diff = abs(self.pv_forecast_w - consumed)
        if diff > _ENERGY_BALANCE_TOLERANCE_W:
            raise ValueError(
                f"HourSlot energy balance violated at hour {self.hour}: "
                f"pv={self.pv_forecast_w:.1f}W vs consumed={consumed:.1f}W "
                f"(diff={diff:.1f}W > tolerance={_ENERGY_BALANCE_TOLERANCE_W}W). "
                f"bat={self.bat_alloc_w}, ev={self.ev_alloc_w}, "
                f"dispatch={self.dispatch_alloc_w}, house={self.house_load_w}, "
                f"export={self.expected_export_w}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for HA sensor attributes."""
        return {
            "hour": self.hour,
            "pv_w": self.pv_forecast_w,
            "house_w": self.house_load_w,
            "bat_w": self.bat_alloc_w,
            "ev_w": self.ev_alloc_w,
            "dispatch_w": self.dispatch_alloc_w,
            "export_w": self.expected_export_w,
            "bat_mode": self.bat_mode.value,
            "ev_amps": self.ev_amps,
            "dispatch_devices": list(self.dispatch_devices),
            "projected_soc": self.projected_bat_soc_pct,
        }


# ---------------------------------------------------------------------------
# Day Plan — full day allocation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayPlan:
    """Full day allocation plan.

    Contains HourSlots for the configured window (default 06-22).
    Created by generate_day_plan() in core/day_planner.py.
    """

    slots: dict[int, HourSlot]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    bat_target_soc_pct: float = 100.0
    ev_target_soc_pct: float = 75.0
    can_discharge_fm: bool = False
    total_expected_export_kwh: float = 0.0

    def __post_init__(self) -> None:
        # Validate total_expected_export_kwh matches sum of slots
        if self.slots:
            computed = sum(
                s.expected_export_w for s in self.slots.values()
            ) / _W_TO_KW
            diff = abs(self.total_expected_export_kwh - computed)
            if diff > _ENERGY_BALANCE_TOLERANCE_W / _W_TO_KW:
                raise ValueError(
                    f"DayPlan total_expected_export_kwh mismatch: "
                    f"declared={self.total_expected_export_kwh:.3f} vs "
                    f"computed={computed:.3f} (diff={diff:.3f})"
                )

    def get_slot(self, hour: int) -> HourSlot | None:
        """Get the HourSlot for a specific hour, or None if outside window."""
        return self.slots.get(hour)

    @property
    def window_hours(self) -> list[int]:
        """Sorted list of hours covered by this plan."""
        return sorted(self.slots.keys())

    def to_dict(self) -> dict[str, Any]:
        """Serialize for HA sensor attributes."""
        return {
            "slots": [s.to_dict() for s in sorted(
                self.slots.values(), key=lambda s: s.hour,
            )],
            "created_at": self.created_at.isoformat(),
            "bat_target_soc_pct": self.bat_target_soc_pct,
            "ev_target_soc_pct": self.ev_target_soc_pct,
            "can_discharge_fm": self.can_discharge_fm,
            "total_expected_export_kwh": self.total_expected_export_kwh,
        }
