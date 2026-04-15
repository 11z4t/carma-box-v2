"""EV PV Surplus Charging Controller for CARMA Box.

Manages EV charging from PV surplus with smooth ramp-up/ramp-down.
Start at min_amps, ramp up when exporting, ramp down on grid import.
NEVER import from grid for EV charging during daytime.

PLAT-1623: Part of EPIC PLAT-1618 (PV Surplus Optimizer).
All thresholds from config — zero naked literals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.models import Command, CommandType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EVSurplusConfig:
    """All thresholds for EV PV surplus charging.

    Values come from EVChargerConfig + EVChargerRampConfig in site.yaml.
    """

    min_amps: int = 6
    max_amps: int = 10
    phases: int = 3
    voltage_v: int = 230
    step_amps: int = 1

    # Ramp thresholds (W)
    ramp_up_export_threshold_w: float = 500.0
    ramp_down_import_threshold_w: float = 100.0

    # Cloud / low-surplus stop thresholds
    cloud_stop_surplus_w: float = 2000.0
    cloud_stop_cycles: int = 3

    @property
    def min_surplus_w(self) -> float:
        """Minimum PV surplus to start EV charging."""
        return float(self.min_amps * self.phases * self.voltage_v)

    @property
    def max_charge_w(self) -> float:
        """Maximum EV charging power."""
        return float(self.max_amps * self.phases * self.voltage_v)

    @property
    def w_per_amp(self) -> float:
        """Watts per ampere step change."""
        return float(self.phases * self.voltage_v)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class EVSurplusState:
    """Mutable state for EV surplus charging controller."""

    current_amps: int = 0
    is_charging: bool = False
    low_surplus_cycles: int = 0


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class EVSurplusController:
    """Controls EV charging from PV surplus.

    Called every 30s cycle by engine._compute_charge_plan().
    Pure logic — emits Command objects, no I/O.
    """

    def __init__(self, config: EVSurplusConfig) -> None:
        self._cfg = config
        self._state = EVSurplusState()

    @property
    def is_charging(self) -> bool:
        """Whether EV surplus charging is currently active."""
        return self._state.is_charging

    @property
    def current_amps(self) -> int:
        """Current EV charging amperage."""
        return self._state.current_amps

    def evaluate(
        self,
        surplus_w: float,
        grid_power_w: float,
        ev_connected: bool,
        ev_soc_pct: float,
        ev_target_soc_pct: float,
    ) -> list[Command]:
        """Evaluate and return EV commands for this cycle.

        Args:
            surplus_w: Available PV surplus (W) after bat allocation.
            grid_power_w: Current grid power (negative=export, positive=import).
            ev_connected: Whether EV is connected to charger.
            ev_soc_pct: Current EV SoC (%).
            ev_target_soc_pct: Target EV SoC (%).

        Returns:
            List of Command objects (SET_EV_CURRENT, START/STOP_EV_CHARGING).
        """
        cmds: list[Command] = []

        # Preconditions: EV must be connected and below target
        if not ev_connected or ev_soc_pct >= ev_target_soc_pct:
            if self._state.is_charging:
                cmds.extend(self._stop_charging("EV disconnected or at target"))
            return cmds

        if self._state.is_charging:
            cmds.extend(self._regulate(surplus_w, grid_power_w))
        else:
            cmds.extend(self._try_start(surplus_w))

        return cmds

    def _try_start(self, surplus_w: float) -> list[Command]:
        """Try to start EV charging if surplus is sufficient."""
        if surplus_w < self._cfg.min_surplus_w:
            return []

        self._state.is_charging = True
        self._state.current_amps = self._cfg.min_amps
        self._state.low_surplus_cycles = 0

        logger.info(
            "EV surplus START: %dA (surplus=%.0fW, min=%.0fW)",
            self._cfg.min_amps, surplus_w, self._cfg.min_surplus_w,
        )

        return [
            Command(
                command_type=CommandType.START_EV_CHARGING,
                target_id="ev",
                value=None,
                rule_id="EV_SURPLUS",
                reason=f"PV surplus {surplus_w:.0f}W >= min {self._cfg.min_surplus_w:.0f}W",
            ),
            Command(
                command_type=CommandType.SET_EV_CURRENT,
                target_id="ev",
                value=self._cfg.min_amps,
                rule_id="EV_SURPLUS",
                reason=f"Start at {self._cfg.min_amps}A",
            ),
        ]

    def _regulate(self, surplus_w: float, grid_power_w: float) -> list[Command]:
        """Regulate charging current based on grid power."""
        cmds: list[Command] = []

        # Check for sustained low surplus → stop
        if surplus_w < self._cfg.cloud_stop_surplus_w:
            self._state.low_surplus_cycles += 1
            if self._state.low_surplus_cycles >= self._cfg.cloud_stop_cycles:
                return self._stop_charging(
                    f"Low surplus {surplus_w:.0f}W for "
                    f"{self._state.low_surplus_cycles} cycles"
                )
        else:
            self._state.low_surplus_cycles = 0

        # Ramp up: exporting → increase current
        if grid_power_w < -self._cfg.ramp_up_export_threshold_w:
            new_amps = min(
                self._state.current_amps + self._cfg.step_amps,
                self._cfg.max_amps,
            )
            if new_amps != self._state.current_amps:
                self._state.current_amps = new_amps
                logger.info(
                    "EV ramp UP → %dA (export=%.0fW)",
                    new_amps, -grid_power_w,
                )
                cmds.append(Command(
                    command_type=CommandType.SET_EV_CURRENT,
                    target_id="ev",
                    value=new_amps,
                    rule_id="EV_SURPLUS",
                    reason=f"Ramp up: export {-grid_power_w:.0f}W > threshold",
                ))

        # Ramp down: importing → decrease current
        elif grid_power_w > self._cfg.ramp_down_import_threshold_w:
            new_amps = max(
                self._state.current_amps - self._cfg.step_amps,
                self._cfg.min_amps,
            )
            if new_amps != self._state.current_amps:
                self._state.current_amps = new_amps
                logger.info(
                    "EV ramp DOWN → %dA (import=%.0fW)",
                    new_amps, grid_power_w,
                )
                cmds.append(Command(
                    command_type=CommandType.SET_EV_CURRENT,
                    target_id="ev",
                    value=new_amps,
                    rule_id="EV_SURPLUS",
                    reason=f"Ramp down: import {grid_power_w:.0f}W > threshold",
                ))
            elif self._state.current_amps == self._cfg.min_amps:
                # Already at min — stop if still importing significantly
                _STOP_IMPORT_FACTOR: float = 2.0
                if grid_power_w > self._cfg.ramp_down_import_threshold_w * _STOP_IMPORT_FACTOR:
                    return self._stop_charging(
                        f"At min amps, still importing {grid_power_w:.0f}W"
                    )

        return cmds

    def _stop_charging(self, reason: str) -> list[Command]:
        """Stop EV charging and reset state."""
        self._state.is_charging = False
        self._state.current_amps = 0
        self._state.low_surplus_cycles = 0
        logger.info("EV surplus STOP: %s", reason)
        return [
            Command(
                command_type=CommandType.STOP_EV_CHARGING,
                target_id="ev",
                value=None,
                rule_id="EV_SURPLUS",
                reason=reason,
            ),
        ]

    def reset(self) -> None:
        """Reset controller state (e.g. on EV disconnect)."""
        self._state = EVSurplusState()
