"""Night Planner — allocates the 22:00-06:00 window between EV and BAT charging.

PLAT-1673: Holistic decision per night.

Inputs (all from snapshot/config):
  - bat current SoC + capacity + target (default 100%)
  - EV current SoC + capacity + target (from input_number.car_target_soc)
  - PV-forecast tomorrow + house consumption tomorrow → tomorrow's PV-excess
  - EV presence tomorrow morning (helg/vardag)
  - Ellevio tak (raw kW the night-window can use)

Outputs:
  - ev_window: (start_hour, end_hour) — when EV charges with BAT-support
  - bat_window: (start_hour, end_hour) — when BAT grid-charges
  - expected SoC at window-end + projected PV allocations for tomorrow

Pure function — no I/O, no side effects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.ev_presence import EVPresenceConfig, evaluate as ev_presence_eval
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PCT_FACTOR: float = 100.0
_W_TO_KW: float = 1000.0
_EV_PHASES: int = 3
_EV_VOLTAGE: int = 230
_HOURS_PER_DAY: int = 24
# Ellevio safety margin — leave 5% headroom under the night cap.
_GRID_SAFETY_MARGIN: float = 0.95


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NightPlannerConfig:
    """Configuration for the night planner."""

    night_start_hour: int = 22
    night_end_hour: int = 6
    bat_target_soc_pct: float = 100.0
    bat_min_soc_pct: float = 20.0
    house_consumption_kwh_per_day: float = 14.0
    house_baseload_night_kw: float = 1.0
    ev_max_amps: int = 10
    grid_tak_raw_night_kw: float = 6.0   # Ellevio 3 kW * 2 (night weight 0.5)
    grid_safety_margin: float = _GRID_SAFETY_MARGIN
    presence: EVPresenceConfig = EVPresenceConfig()


# ---------------------------------------------------------------------------
# Inputs / Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NightPlannerInput:
    """All facts needed to plan one night."""

    now: datetime
    bat_total_soc_pct: float
    bat_total_cap_kwh: float
    ev_soc_pct: float
    ev_target_soc_pct: float
    ev_battery_kwh: float
    pv_forecast_tomorrow_kwh: float
    ev_device_tracker_state: str | None = None


@dataclass(frozen=True)
class NightPlan:
    """Result of night planning."""

    ev_window_start_hour: int
    ev_window_end_hour: int            # may exceed 24 to indicate wrap (e.g. 25 = 01:00)
    bat_window_start_hour: int
    bat_window_end_hour: int
    ev_need_kwh: float
    bat_need_grid_kwh: float
    pv_excess_tomorrow_kwh: float
    pv_to_ev_tomorrow_kwh: float
    pv_to_bat_tomorrow_kwh: float
    ev_min_hours: float
    bat_min_hours: float
    overflow_warning: bool
    reason: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ev_max_kw(max_amps: int) -> float:
    """Maximum EV charging power for given amps (3-phase 230V)."""
    return float(max_amps * _EV_PHASES * _EV_VOLTAGE) / _W_TO_KW


def _night_window_hours(cfg: NightPlannerConfig) -> int:
    """Number of hours in the night window, handling midnight wrap."""
    if cfg.night_start_hour >= cfg.night_end_hour:
        return _HOURS_PER_DAY - cfg.night_start_hour + cfg.night_end_hour
    return cfg.night_end_hour - cfg.night_start_hour


def _bat_grid_rate_kw(cfg: NightPlannerConfig) -> float:
    """Power available for BAT grid-charge after subtracting baseload, with margin."""
    cap_with_margin = cfg.grid_tak_raw_night_kw * cfg.grid_safety_margin
    return max(0.0, cap_with_margin - cfg.house_baseload_night_kw)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def plan_night(inp: NightPlannerInput, cfg: NightPlannerConfig) -> NightPlan:
    """Compute the night allocation given current state + tomorrow's forecast."""
    # 1. EV / BAT energy needs
    ev_need_kwh = max(
        0.0,
        (inp.ev_target_soc_pct - inp.ev_soc_pct) * inp.ev_battery_kwh / _PCT_FACTOR,
    )
    bat_need_kwh = max(
        0.0,
        (cfg.bat_target_soc_pct - inp.bat_total_soc_pct)
        * inp.bat_total_cap_kwh / _PCT_FACTOR,
    )

    # 2. Tomorrow PV excess (after baseline household consumption)
    pv_excess_tomorrow = max(
        0.0,
        inp.pv_forecast_tomorrow_kwh - cfg.house_consumption_kwh_per_day,
    )

    # 3. EV presence tomorrow morning (08-12 typical PV peak)
    tomorrow_morning = (inp.now + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0,
    )
    presence = ev_presence_eval(
        tomorrow_morning,
        config=cfg.presence,
        device_tracker_state=inp.ev_device_tracker_state,
    )
    ev_home_tomorrow = presence.is_home

    # 4. Allocate tomorrow PV: EV first if home, else all to BAT
    pv_to_ev_tomorrow = min(pv_excess_tomorrow, ev_need_kwh) if ev_home_tomorrow else 0.0
    pv_to_bat_tomorrow = max(0.0, pv_excess_tomorrow - pv_to_ev_tomorrow)

    # 5. Remaining grid-need after tomorrow PV is accounted for
    ev_grid_need_kwh = max(0.0, ev_need_kwh - pv_to_ev_tomorrow)
    bat_grid_need_kwh = max(0.0, bat_need_kwh - pv_to_bat_tomorrow)

    # 6. Hours required
    ev_max_kw = _ev_max_kw(cfg.ev_max_amps)
    ev_min_hours = ev_grid_need_kwh / ev_max_kw if ev_max_kw > 0 else 0.0
    bat_grid_rate = _bat_grid_rate_kw(cfg)
    bat_min_hours = bat_grid_need_kwh / bat_grid_rate if bat_grid_rate > 0 else 0.0

    # 7. Window allocation
    window_hours = _night_window_hours(cfg)
    overflow = (ev_min_hours + bat_min_hours) > window_hours

    if overflow:
        # Proportional split — both get a share of the window
        total_need = ev_min_hours + bat_min_hours
        ev_share = (ev_min_hours / total_need) * window_hours if total_need > 0 else 0.0
        ev_window_len = ev_share
    else:
        ev_window_len = ev_min_hours

    ev_window_start = cfg.night_start_hour
    ev_window_end = cfg.night_start_hour + ev_window_len  # may wrap past 24
    bat_window_start = ev_window_end
    bat_window_end = cfg.night_start_hour + window_hours  # absolute hour-offset

    # Format reason
    reason_parts = [
        f"ev_need={ev_need_kwh:.1f}kWh ({ev_min_hours:.1f}h)",
        f"bat_need_grid={bat_grid_need_kwh:.1f}kWh ({bat_min_hours:.1f}h)",
        f"pv_excess_tomorrow={pv_excess_tomorrow:.1f}kWh",
        f"ev_home_tomorrow={ev_home_tomorrow}",
    ]
    if overflow:
        reason_parts.append("OVERFLOW: split proportionally")
    reason = " | ".join(reason_parts)

    return NightPlan(
        ev_window_start_hour=ev_window_start,
        ev_window_end_hour=int(ev_window_end + 0.999),  # round up to next whole hour
        bat_window_start_hour=int(bat_window_start),
        bat_window_end_hour=int(bat_window_end),
        ev_need_kwh=ev_need_kwh,
        bat_need_grid_kwh=bat_grid_need_kwh,
        pv_excess_tomorrow_kwh=pv_excess_tomorrow,
        pv_to_ev_tomorrow_kwh=pv_to_ev_tomorrow,
        pv_to_bat_tomorrow_kwh=pv_to_bat_tomorrow,
        ev_min_hours=ev_min_hours,
        bat_min_hours=bat_min_hours,
        overflow_warning=overflow,
        reason=reason,
    )


def is_in_window(now_hour: int, start_hour: int, end_hour: int) -> bool:
    """Check if `now_hour` is inside [start, end), handling midnight wrap.

    Hours can be 0-24+ where >=24 means next day.
    """
    # Normalize end_hour to 0-23 for comparison
    end_norm = end_hour % _HOURS_PER_DAY
    start_norm = start_hour % _HOURS_PER_DAY

    if end_hour < _HOURS_PER_DAY and end_hour > start_hour:
        # Same day window (e.g. 22..23)
        return start_norm <= now_hour < end_norm
    # Wraps past midnight (e.g. 22..28 = 22-04 next day)
    return now_hour >= start_norm or now_hour < end_norm
