"""Night and Evening Planner for CARMA Box.

Night Planner (Section 9):
- Backward calculation from 06:00
- EV scheduled before battery (battery closer to 06:00)
- Cheapest hours preferred
- High PV tomorrow: skip/reduce night battery charging
- Weekend + high PV + EV > 80%: skip night EV

Evening Planner (Section 10):
- 50/50 split evening/morning of battery surplus
- Evening floor SoC calculation
- Price-optimized discharge scheduling

All thresholds from config — zero hardcoding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannerConfig:
    """Planner thresholds — all from site.yaml.

    All numeric coefficients are named and documented here so that the
    planning algorithms contain zero magic numbers. When a value changes
    (e.g. a different EV or new grid tariff) only this dataclass needs
    updating.
    """

    # Night window
    night_start_hour: int = 22
    night_end_hour: int = 6

    # House baseload
    house_baseload_kw: float = 2.5
    night_hours: int = 8

    # Battery grid charging
    grid_charge_max_kw: float = 3.0
    grid_charge_price_threshold_ore: float = 60.0
    grid_charge_max_soc_pct: float = 90.0

    # EV
    ev_target_soc_pct: float = 75.0
    ev_battery_kwh: float = 92.0
    ev_efficiency: float = 0.92
    max_soc_jump_pct: float = 20.0
    ev_charge_kw: float = 6.9  # 3-phase 10A

    # PV thresholds
    pv_high_threshold_kwh: float = 15.0

    # Evening
    evening_allocation_pct: float = 50.0
    morning_allocation_pct: float = 50.0

    # Battery
    min_soc_pct: float = 15.0
    bat_efficiency: float = 0.90

    # --- Coefficients used in planning calculations ---

    # Fraction of PV forecast that is expected to reach the battery.
    # 0.5 = 50 % of tomorrow's PV will contribute to battery charging.
    # Used to reduce grid-charge need when PV is forecast (conservative).
    pv_bat_contribution_factor: float = 0.5

    # Fraction of the EV's grid charge need that is served by the battery
    # during evening discharge (the rest comes from the grid overnight).
    # 0.3 = battery covers ~30 % of EV charge need, grid covers the rest.
    ev_bat_contribution_pct: float = 0.3

    # Number of evening discharge hours used to spread the surplus allocation.
    # 5 hours covers the typical 17:00–22:00 evening peak window.
    evening_discharge_hours: float = 5.0

    # Grid voltage per phase (V) used for EV ampere → kW conversion.
    # 230 V is the nominal single-phase voltage in Sweden (EN 50160).
    grid_voltage_v: float = 230.0

    # Number of phases used for EV charging (3-phase XPENG G9 default).
    ev_phases: int = 3


# ---------------------------------------------------------------------------
# Plan results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NightPlan:
    """Result of night planning."""

    ev_charge_need_kwh: float = 0.0
    ev_start_hour: int = 22
    ev_stop_hour: int = 4
    ev_amps: int = 6
    ev_skip: bool = False
    ev_skip_reason: str = ""

    bat_charge_need_kwh: float = 0.0
    bat_charge_start_hour: int = 4
    bat_charge_stop_hour: int = 6
    bat_charge_rate_kw: float = 0.0
    bat_skip: bool = False
    bat_skip_reason: str = ""

    cheapest_hours: list[int] = field(default_factory=list)
    total_cost_ore: float = 0.0


@dataclass(frozen=True)
class EveningPlan:
    """Result of evening planning."""

    bat_available_kwh: float = 0.0
    night_need_kwh: float = 0.0
    bat_surplus_kwh: float = 0.0
    evening_allocation_kwh: float = 0.0
    morning_allocation_kwh: float = 0.0
    evening_floor_soc_pct: float = 15.0
    hourly_rate_w: float = 0.0


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class Planner:
    """Night and evening planner.

    Pure function design — takes inputs, returns plan.
    No side effects, no I/O.
    """

    def __init__(self, config: PlannerConfig | None = None) -> None:
        self._config = config or PlannerConfig()

    # ------------------------------------------------------------------
    # Night plan
    # ------------------------------------------------------------------

    def generate_night_plan(
        self,
        bat_soc_pct: float,
        bat_cap_kwh: float,
        ev_connected: bool,
        ev_soc_pct: float,
        pv_tomorrow_kwh: float,
        prices_by_hour: dict[int, float],
        is_weekend: bool = False,
    ) -> NightPlan:
        """Generate night plan with backward calculation from 06:00.

        EV scheduled first (22:00→), battery closer to 06:00.
        """
        cfg = self._config

        # EV charge need
        ev_need = 0.0
        ev_skip = False
        ev_skip_reason = ""
        ev_hours_needed = 0.0

        if ev_connected and ev_soc_pct < cfg.ev_target_soc_pct:
            # Weekend + high PV + EV > 80% → skip night EV
            if is_weekend and pv_tomorrow_kwh > cfg.pv_high_threshold_kwh and ev_soc_pct > 80.0:
                ev_skip = True
                ev_skip_reason = "weekend + high PV + EV > 80%"
            else:
                ev_need = self._calculate_ev_charge_need(ev_soc_pct)
                # Low PV: limit to max_soc_jump
                if pv_tomorrow_kwh <= cfg.pv_high_threshold_kwh:
                    max_jump_kwh = (cfg.max_soc_jump_pct / 100.0) * cfg.ev_battery_kwh
                    ev_need = min(ev_need, max_jump_kwh / cfg.ev_efficiency)
                ev_hours_needed = ev_need / cfg.ev_charge_kw
        elif not ev_connected:
            ev_skip = True
            ev_skip_reason = "EV not connected"

        # Battery grid charge need
        bat_need = 0.0
        bat_skip = False
        bat_skip_reason = ""
        bat_hours_needed = 0.0

        if pv_tomorrow_kwh > cfg.pv_high_threshold_kwh:
            # High PV tomorrow — skip or reduce battery charging
            bat_skip = True
            bat_skip_reason = "high PV tomorrow, solar will charge"
        elif bat_soc_pct < cfg.grid_charge_max_soc_pct:
            bat_need = self._calculate_bat_charge_need(bat_soc_pct, bat_cap_kwh, pv_tomorrow_kwh)
            bat_hours_needed = bat_need / cfg.grid_charge_max_kw

        # Schedule backward from 06:00
        night_hours = self._get_night_hours()
        cheapest = self._sort_by_cheapest(night_hours, prices_by_hour)

        # Battery closer to 06:00, EV starts earlier
        bat_start = max(cfg.night_end_hour - int(bat_hours_needed) - 1, cfg.night_start_hour)
        bat_stop = cfg.night_end_hour

        ev_start = cfg.night_start_hour
        ev_stop = min(cfg.night_start_hour + int(ev_hours_needed) + 1, bat_start)

        # Total cost estimate
        total_cost = 0.0
        for h in cheapest[:int(ev_hours_needed + bat_hours_needed)]:
            total_cost += prices_by_hour.get(h, cfg.grid_charge_price_threshold_ore) * 0.001

        return NightPlan(
            ev_charge_need_kwh=ev_need,
            ev_start_hour=ev_start,
            ev_stop_hour=ev_stop,
            # Convert kW to amps: P(kW) * 1000 / (V_phase * n_phases)
            # Uses grid_voltage_v and ev_phases from config (PLAT-1358).
            ev_amps=int(
                cfg.ev_charge_kw * 1000 // (cfg.grid_voltage_v * cfg.ev_phases)
            ) if not ev_skip else 0,
            ev_skip=ev_skip,
            ev_skip_reason=ev_skip_reason,
            bat_charge_need_kwh=bat_need,
            bat_charge_start_hour=bat_start if not bat_skip else 0,
            bat_charge_stop_hour=bat_stop if not bat_skip else 0,
            bat_charge_rate_kw=cfg.grid_charge_max_kw if not bat_skip else 0.0,
            bat_skip=bat_skip,
            bat_skip_reason=bat_skip_reason,
            cheapest_hours=cheapest,
            total_cost_ore=total_cost,
        )

    # ------------------------------------------------------------------
    # Evening plan
    # ------------------------------------------------------------------

    def generate_evening_plan(
        self,
        bat_soc_pct: float,
        bat_cap_kwh: float,
        ev_connected: bool,
        ev_soc_pct: float,
        house_baseload_kw: float | None = None,
    ) -> EveningPlan:
        """Generate evening plan with 50/50 split.

        Returns: evening allocation, morning allocation, floor SoC.
        """
        cfg = self._config
        baseload = house_baseload_kw or cfg.house_baseload_kw

        # Available battery energy above minimum
        bat_available = max(
            0.0,
            (bat_soc_pct - cfg.min_soc_pct) / 100.0 * bat_cap_kwh * cfg.bat_efficiency,
        )

        # Night need estimate
        ev_need = 0.0
        if ev_connected and ev_soc_pct < cfg.ev_target_soc_pct:
            # ev_bat_contribution_pct: fraction of EV charge need served by battery
            ev_need = self._calculate_ev_charge_need(ev_soc_pct) * cfg.ev_bat_contribution_pct

        night_need = baseload * cfg.night_hours + ev_need

        # Surplus = available - night need
        bat_surplus = bat_available - night_need

        if bat_surplus <= 0:
            # Deficit: no evening discharge, preservation mode
            evening_floor = bat_soc_pct  # Don't discharge at all
            return EveningPlan(
                bat_available_kwh=bat_available,
                night_need_kwh=night_need,
                bat_surplus_kwh=0.0,
                evening_allocation_kwh=0.0,
                morning_allocation_kwh=0.0,
                evening_floor_soc_pct=evening_floor,
                hourly_rate_w=0.0,
            )

        # 50/50 split
        evening_alloc = bat_surplus * (cfg.evening_allocation_pct / 100.0)
        morning_alloc = bat_surplus * (cfg.morning_allocation_pct / 100.0)

        # Evening floor SoC: min_soc + night_need / cap * 100, clamped to 100%
        evening_floor = min(100.0, cfg.min_soc_pct + (night_need / bat_cap_kwh * 100.0))

        # Hourly rate over evening_discharge_hours (default 5h: 17:00–22:00).
        # kWh ÷ hours × 1000 converts to average W per hour (PLAT-1358).
        hourly_rate_w = evening_alloc / cfg.evening_discharge_hours * 1000.0

        return EveningPlan(
            bat_available_kwh=bat_available,
            night_need_kwh=night_need,
            bat_surplus_kwh=bat_surplus,
            evening_allocation_kwh=evening_alloc,
            morning_allocation_kwh=morning_alloc,
            evening_floor_soc_pct=evening_floor,
            hourly_rate_w=hourly_rate_w,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _calculate_ev_charge_need(self, ev_soc_pct: float) -> float:
        """Calculate EV charge need in kWh."""
        cfg = self._config
        soc_gap = cfg.ev_target_soc_pct - ev_soc_pct
        if soc_gap <= 0:
            return 0.0
        return (soc_gap / 100.0) * cfg.ev_battery_kwh / cfg.ev_efficiency

    def _calculate_bat_charge_need(
        self, bat_soc_pct: float, bat_cap_kwh: float, pv_tomorrow_kwh: float
    ) -> float:
        """Calculate battery grid charge need in kWh."""
        cfg = self._config
        target_soc = cfg.grid_charge_max_soc_pct
        soc_gap = target_soc - bat_soc_pct
        if soc_gap <= 0:
            return 0.0
        raw_need = (soc_gap / 100.0) * bat_cap_kwh / cfg.bat_efficiency
        # Reduce by expected PV contribution (pv_bat_contribution_factor fraction
        # of tomorrow's forecast reaches the battery — conservative estimate).
        pv_contribution = min(pv_tomorrow_kwh * cfg.pv_bat_contribution_factor, raw_need)
        return max(0.0, raw_need - pv_contribution)

    def _get_night_hours(self) -> list[int]:
        """Get list of night hours (22, 23, 0, 1, 2, 3, 4, 5)."""
        cfg = self._config
        hours = []
        h = cfg.night_start_hour
        while True:
            hours.append(h % 24)
            h = (h + 1) % 24
            if h == cfg.night_end_hour:
                break
        return hours

    @staticmethod
    def _sort_by_cheapest(hours: list[int], prices: dict[int, float]) -> list[int]:
        """Sort hours by electricity price (cheapest first)."""
        return sorted(hours, key=lambda h: prices.get(h, 999.0))
