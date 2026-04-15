"""Day Planner for CARMA Box PV Surplus Optimizer.

Pure function: generate_day_plan(pv_hourly, state, cfg) → DayPlan.
Greedy per-hour surplus allocation: bat → EV → dispatch → export.
Minimizes export by consuming every available watt of PV surplus.

PLAT-1630: Part of EPIC PLAT-1618 (PV Surplus Optimizer).
All thresholds from config — zero naked literals in logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.day_plan import (
    BatMode,
    DayPlan,
    HourSlot,
    HourlyForecast,
    ZERO_HOURLY_FORECAST,
)
from core.models import MAX_SOC_PCT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Watts-to-kilowatts conversion.
_W_TO_KW: float = 1000.0

# Minimum SoC projection floor (%) — batteries cannot go below this.
_SOC_FLOOR_PCT: float = 0.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatteryPlanConfig:
    """Battery config subset needed for day planning."""

    battery_id: str = ""
    cap_kwh: float = 15.0
    max_charge_kw: float = 5.0
    max_discharge_kw: float = 5.0
    efficiency: float = 0.90
    min_soc_pct: float = 15.0
    current_soc_pct: float = 50.0


@dataclass(frozen=True)
class EVPlanConfig:
    """EV charger config subset needed for day planning."""

    min_amps: int = 6
    max_amps: int = 10
    phases: int = 3
    voltage_v: int = 230
    current_soc_pct: float = 50.0
    target_soc_pct: float = 75.0
    battery_kwh: float = 92.0
    efficiency: float = 0.92
    connected: bool = False

    @property
    def min_surplus_w(self) -> float:
        """Minimum PV surplus needed to start EV charging."""
        return float(self.min_amps * self.phases * self.voltage_v)

    @property
    def max_charge_w(self) -> float:
        """Maximum EV charging power."""
        return float(self.max_amps * self.phases * self.voltage_v)


@dataclass(frozen=True)
class DispatchDeviceConfig:
    """Dispatchable consumer config subset for planning."""

    device_id: str = ""
    power_w: int = 0
    priority: int = 99


@dataclass(frozen=True)
class DayPlanConfig:
    """All config needed to generate a day plan.

    All thresholds come from site.yaml via this config.
    No naked literals in the planning algorithm.
    """

    batteries: tuple[BatteryPlanConfig, ...] = ()
    ev: EVPlanConfig = field(default_factory=EVPlanConfig)
    dispatch_devices: tuple[DispatchDeviceConfig, ...] = ()
    baseload_kw: float = 2.5
    window_start_h: int = 6
    window_end_h: int = 22
    fm_discharge_safety_margin_kwh: float = 2.0
    morning_discharge_start_h: int = 6
    morning_discharge_end_h: int = 9
    midday_charge_start_h: int = 12
    midday_charge_end_h: int = 17
    bat_target_soc_pct: float = 100.0
    # Use p10 (conservative) for discharge decisions
    use_p10_for_discharge: bool = True


# ---------------------------------------------------------------------------
# FM Discharge Decision
# ---------------------------------------------------------------------------


def can_discharge_fm(
    pv_hourly: dict[int, HourlyForecast],
    cfg: DayPlanConfig,
) -> bool:
    """Decide if FM battery discharge is safe based on PV forecast.

    Uses p10 (conservative) estimate: can PV refill batteries to target
    during midday (12-17) after covering house load?

    PLAT-1622: Part of EPIC PLAT-1618.

    Formula:
        pv_net = sum(p10_kwh for midday hours) - house_load_kwh
        bat_deficit = sum((target - current_soc) / 100 * cap for each bat)
        safe = pv_net > bat_deficit + safety_margin
    """
    if not cfg.batteries:
        return False

    # Sum p10 PV for midday charge window
    pv_midday_kwh: float = 0.0
    midday_hours = range(cfg.midday_charge_start_h, cfg.midday_charge_end_h)
    for h in midday_hours:
        forecast = pv_hourly.get(h, ZERO_HOURLY_FORECAST)
        pv_midday_kwh += forecast.p10_kwh if cfg.use_p10_for_discharge else forecast.p50_kwh

    # House load during midday
    midday_hour_count = cfg.midday_charge_end_h - cfg.midday_charge_start_h
    house_load_kwh = cfg.baseload_kw * midday_hour_count

    # Battery deficit to reach target
    bat_deficit_kwh: float = 0.0
    for bat in cfg.batteries:
        soc_gap_pct = max(0.0, cfg.bat_target_soc_pct - bat.current_soc_pct)
        bat_deficit_kwh += (soc_gap_pct / MAX_SOC_PCT) * bat.cap_kwh / bat.efficiency

    pv_net = pv_midday_kwh - house_load_kwh
    safe = pv_net > bat_deficit_kwh + cfg.fm_discharge_safety_margin_kwh

    logger.info(
        "FM discharge decision: pv_midday=%.1fkWh house=%.1fkWh pv_net=%.1fkWh "
        "bat_deficit=%.1fkWh margin=%.1fkWh → %s",
        pv_midday_kwh, house_load_kwh, pv_net,
        bat_deficit_kwh, cfg.fm_discharge_safety_margin_kwh,
        "DISCHARGE" if safe else "STANDBY",
    )
    return safe


# ---------------------------------------------------------------------------
# Day Plan Generator
# ---------------------------------------------------------------------------


def generate_day_plan(
    pv_hourly: dict[int, HourlyForecast],
    cfg: DayPlanConfig,
) -> DayPlan:
    """Generate optimal day plan by allocating PV surplus per hour.

    Greedy sweep: for each hour, allocate surplus in priority order:
    1. Battery charging (up to max_charge_kw per bat, until SoC 100%)
    2. EV charging (if connected + under target, min_surplus threshold)
    3. Dispatch devices (priority-sorted, on/off)
    4. Export (last resort)

    Pure function — no side effects, no I/O.

    PLAT-1630: Part of EPIC PLAT-1618.
    """
    discharge_fm = can_discharge_fm(pv_hourly, cfg)
    house_load_w = cfg.baseload_kw * _W_TO_KW

    # Track projected SoC per battery across hours
    bat_soc: dict[str, float] = {
        bat.battery_id: bat.current_soc_pct for bat in cfg.batteries
    }

    # Sort dispatch devices by priority (ascending = higher priority first)
    sorted_dispatch = sorted(cfg.dispatch_devices, key=lambda d: d.priority)

    # EV state tracking
    ev_soc_pct = cfg.ev.current_soc_pct

    slots: dict[int, HourSlot] = {}

    for hour in range(cfg.window_start_h, cfg.window_end_h):
        forecast = pv_hourly.get(hour, ZERO_HOURLY_FORECAST)
        pv_w = forecast.p50_kwh * _W_TO_KW  # Use p50 for allocation

        surplus_w = max(0.0, pv_w - house_load_w)
        deficit_w = max(0.0, house_load_w - pv_w)

        bat_alloc_w: float = 0.0
        ev_alloc_w: float = 0.0
        dispatch_alloc_w: float = 0.0
        bat_mode = BatMode.STANDBY
        ev_amps: int = 0
        active_dispatch: list[str] = []

        # --- Deficit hours: discharge or standby ---
        if deficit_w > 0 and surplus_w == 0:
            is_morning = cfg.morning_discharge_start_h <= hour < cfg.morning_discharge_end_h
            if discharge_fm and is_morning:
                # Discharge battery to cover house load
                bat_mode = BatMode.DISCHARGE
                # Negative alloc = discharge (for energy balance: pv + discharge = house)
                bat_alloc_w = -deficit_w
            else:
                bat_mode = BatMode.STANDBY

            # Energy balance: pv_w + |bat_discharge| = house + export
            # pv_w = house_load_w + bat_alloc_w + export
            # If discharging: pv_w = house_load_w + (-deficit) + 0 → pv_w = pv_w ✓
            slots[hour] = HourSlot(
                hour=hour,
                pv_forecast_w=pv_w,
                house_load_w=pv_w - bat_alloc_w,  # Effective house load covered
                bat_alloc_w=0.0,  # No charging
                ev_alloc_w=0.0,
                dispatch_alloc_w=0.0,
                expected_export_w=0.0,
                bat_mode=bat_mode,
                ev_amps=0,
                projected_bat_soc_pct=_avg_soc(bat_soc),
            )
            # Update SoC for discharge
            if bat_mode == BatMode.DISCHARGE:
                _discharge_batteries(bat_soc, deficit_w, cfg.batteries)
            continue

        # --- Surplus hours: allocate bat → EV → dispatch → export ---
        remaining = surplus_w

        # 1. Battery charging
        if remaining > 0:
            bat_alloc_w, remaining = _allocate_batteries(
                remaining, bat_soc, cfg.batteries, cfg.bat_target_soc_pct,
            )
            if bat_alloc_w > 0:
                bat_mode = BatMode.CHARGE

        # 2. EV charging
        ev_needs_charge = (
            cfg.ev.connected
            and ev_soc_pct < cfg.ev.target_soc_pct
        )
        if ev_needs_charge and remaining >= cfg.ev.min_surplus_w:
            ev_alloc_w = min(remaining, cfg.ev.max_charge_w)
            ev_amps = int(ev_alloc_w / (cfg.ev.phases * cfg.ev.voltage_v))
            ev_amps = max(cfg.ev.min_amps, min(ev_amps, cfg.ev.max_amps))
            ev_alloc_w = float(ev_amps * cfg.ev.phases * cfg.ev.voltage_v)
            remaining -= ev_alloc_w
            # Update EV SoC projection
            ev_kwh = ev_alloc_w / _W_TO_KW * cfg.ev.efficiency
            ev_soc_pct += (ev_kwh / cfg.ev.battery_kwh) * MAX_SOC_PCT
            ev_soc_pct = min(ev_soc_pct, MAX_SOC_PCT)

        # 3. Dispatch devices (priority order)
        for device in sorted_dispatch:
            if remaining >= device.power_w and device.power_w > 0:
                dispatch_alloc_w += device.power_w
                remaining -= device.power_w
                active_dispatch.append(device.device_id)

        # 4. Export (last resort)
        export_w = max(0.0, remaining)

        slots[hour] = HourSlot(
            hour=hour,
            pv_forecast_w=pv_w,
            house_load_w=house_load_w,
            bat_alloc_w=bat_alloc_w,
            ev_alloc_w=ev_alloc_w,
            dispatch_alloc_w=dispatch_alloc_w,
            expected_export_w=export_w,
            bat_mode=bat_mode,
            ev_amps=ev_amps,
            dispatch_devices=tuple(active_dispatch),
            projected_bat_soc_pct=_avg_soc(bat_soc),
        )

    total_export_kwh = sum(s.expected_export_w for s in slots.values()) / _W_TO_KW

    plan = DayPlan(
        slots=slots,
        bat_target_soc_pct=cfg.bat_target_soc_pct,
        ev_target_soc_pct=cfg.ev.target_soc_pct,
        can_discharge_fm=discharge_fm,
        total_expected_export_kwh=total_export_kwh,
    )

    logger.info(
        "Day plan generated: %d slots, export=%.1fkWh, fm_discharge=%s",
        len(slots), total_export_kwh, discharge_fm,
    )
    return plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _allocate_batteries(
    available_w: float,
    bat_soc: dict[str, float],
    batteries: tuple[BatteryPlanConfig, ...],
    target_soc_pct: float,
) -> tuple[float, float]:
    """Allocate surplus to batteries proportionally by capacity.

    Returns (total_allocated_w, remaining_w).
    Updates bat_soc in-place with projected SoC after 1 hour of charging.
    """
    # Filter batteries that can still charge
    chargeable = [b for b in batteries if bat_soc.get(b.battery_id, MAX_SOC_PCT) < target_soc_pct]
    if not chargeable:
        return 0.0, available_w

    total_cap = sum(b.cap_kwh for b in chargeable)
    if total_cap <= 0:
        return 0.0, available_w

    total_alloc: float = 0.0
    remaining = available_w

    for bat in chargeable:
        share = bat.cap_kwh / total_cap
        max_w = bat.max_charge_kw * _W_TO_KW
        alloc = min(remaining * share, max_w)

        # Don't overshoot target SoC
        soc_gap = target_soc_pct - bat_soc.get(bat.battery_id, MAX_SOC_PCT)
        max_kwh_needed = (soc_gap / MAX_SOC_PCT) * bat.cap_kwh / bat.efficiency
        max_w_for_soc = max_kwh_needed * _W_TO_KW
        alloc = min(alloc, max(0.0, max_w_for_soc))

        total_alloc += alloc
        remaining -= alloc

        # Update projected SoC
        kwh_charged = alloc / _W_TO_KW * bat.efficiency
        soc_increase = (kwh_charged / bat.cap_kwh) * MAX_SOC_PCT
        bat_soc[bat.battery_id] = min(
            target_soc_pct,
            bat_soc.get(bat.battery_id, 0.0) + soc_increase,
        )

    return total_alloc, max(0.0, remaining)


def _discharge_batteries(
    bat_soc: dict[str, float],
    discharge_w: float,
    batteries: tuple[BatteryPlanConfig, ...],
) -> None:
    """Update bat_soc for discharge (proportional by capacity)."""
    dischargeable = [
        b for b in batteries
        if bat_soc.get(b.battery_id, 0.0) > b.min_soc_pct
    ]
    if not dischargeable:
        return

    total_cap = sum(b.cap_kwh for b in dischargeable)
    if total_cap <= 0:
        return

    for bat in dischargeable:
        share = bat.cap_kwh / total_cap
        alloc = discharge_w * share
        kwh_discharged = alloc / _W_TO_KW / bat.efficiency
        soc_decrease = (kwh_discharged / bat.cap_kwh) * MAX_SOC_PCT
        bat_soc[bat.battery_id] = max(
            bat.min_soc_pct,
            bat_soc.get(bat.battery_id, 0.0) - soc_decrease,
        )


def _avg_soc(bat_soc: dict[str, float]) -> float:
    """Average SoC across all batteries."""
    if not bat_soc:
        return 0.0
    return sum(bat_soc.values()) / len(bat_soc)
