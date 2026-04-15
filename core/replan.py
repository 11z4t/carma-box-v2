"""Replan Trigger for CARMA Box Day Planner.

Decides when to regenerate the DayPlan based on significant changes:
- PV forecast change > threshold
- EV connect/disconnect
- Battery SoC deviation from projection
- Cooldown prevents rapid replanning (flapping)

PLAT-1625: Part of EPIC PLAT-1618 (PV Surplus Optimizer).
All thresholds from config — zero naked literals.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from core.day_plan import DayPlan, HourlyForecast

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplanConfig:
    """All thresholds for replan triggers."""

    # PV forecast change threshold (fraction, e.g. 0.20 = 20%)
    pv_change_threshold: float = 0.20

    # Battery SoC deviation from projection (percentage points)
    soc_deviation_pct: float = 10.0

    # Minimum time between replans (seconds)
    cooldown_s: int = 900  # 15 minutes


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


class ReplanTrigger:
    """Evaluates whether a DayPlan replan is needed.

    Stateful: tracks last replan time and previous state for comparison.
    """

    def __init__(self, config: ReplanConfig) -> None:
        self._cfg = config
        self._last_replan_time: float = 0.0
        self._last_pv_total_kwh: float = 0.0
        self._last_ev_connected: bool = False

    def should_replan(
        self,
        current_plan: DayPlan | None,
        pv_hourly: dict[int, HourlyForecast],
        ev_connected: bool,
        bat_soc_pct: float,
        current_hour: int,
    ) -> tuple[bool, str]:
        """Check if replan is needed.

        Returns (should_replan, reason).
        Returns (False, "") if no replan needed or cooldown active.
        """
        # No plan → always replan
        if current_plan is None:
            return self._do_replan("No existing plan")

        # Cooldown check
        now = time.monotonic()
        elapsed = now - self._last_replan_time
        if elapsed < self._cfg.cooldown_s:
            return False, ""

        # Trigger 1: PV forecast change
        current_pv_total = sum(
            f.p50_kwh for f in pv_hourly.values()
        )
        if self._last_pv_total_kwh > 0:
            change = abs(current_pv_total - self._last_pv_total_kwh) / self._last_pv_total_kwh
            if change > self._cfg.pv_change_threshold:
                return self._do_replan(
                    f"PV change {change:.0%} > threshold {self._cfg.pv_change_threshold:.0%} "
                    f"({self._last_pv_total_kwh:.1f} → {current_pv_total:.1f} kWh)"
                )

        # Trigger 2: EV connect/disconnect
        if ev_connected != self._last_ev_connected:
            event = "connected" if ev_connected else "disconnected"
            return self._do_replan(f"EV {event}")

        # Trigger 3: SoC deviation from projection
        slot = current_plan.get_slot(current_hour)
        if slot is not None:
            deviation = abs(bat_soc_pct - slot.projected_bat_soc_pct)
            if deviation > self._cfg.soc_deviation_pct:
                return self._do_replan(
                    f"SoC deviation {deviation:.1f}% > threshold "
                    f"{self._cfg.soc_deviation_pct:.1f}% "
                    f"(actual={bat_soc_pct:.1f}% vs plan={slot.projected_bat_soc_pct:.1f}%)"
                )

        # Update tracking state (no replan needed)
        self._last_pv_total_kwh = current_pv_total
        self._last_ev_connected = ev_connected
        return False, ""

    def _do_replan(self, reason: str) -> tuple[bool, str]:
        """Record replan and return trigger result."""
        self._last_replan_time = time.monotonic()
        logger.info("Replan triggered: %s", reason)
        return True, reason

    def update_tracking(
        self,
        pv_hourly: dict[int, HourlyForecast],
        ev_connected: bool,
    ) -> None:
        """Update tracking state after a successful replan."""
        self._last_pv_total_kwh = sum(f.p50_kwh for f in pv_hourly.values())
        self._last_ev_connected = ev_connected
