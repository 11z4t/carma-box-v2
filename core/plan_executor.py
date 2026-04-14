"""PlanExecutor — energy plan generation extracted from main.py (PLAT-1558).

Encapsulates _generate_plan() and _generate_48h_plan() from CarmaBoxService,
keeping plan logic in core/ and out of the entry-point module.

GuardPolicy is consulted before each plan generation cycle.
If guard level is ALARM or FREEZE, plan generation is skipped to avoid
wasting resources during emergency states.
"""

from __future__ import annotations

import asyncio
import logging
import zoneinfo
from datetime import datetime, timedelta
from typing import Any, Optional

from adapters.ha_api import HAApiClient
from config.schema import CarmaConfig
from core.guards import GuardLevel, GuardPolicy
from core.models import MAX_SOC_PCT, SystemSnapshot
from core.planner import Planner, PlannerConfig

logger = logging.getLogger(__name__)

# Guard levels that block plan generation
_BLOCKING_GUARD_LEVELS: frozenset[GuardLevel] = frozenset(
    {GuardLevel.ALARM, GuardLevel.FREEZE}
)

# Watts-to-kilowatts conversion factor
_W_TO_KW: float = 1000.0


class PlanExecutor:
    """Generates forward-looking energy plans at key hours (06/12/17/22).

    Calls GuardPolicy before generating — plans are skipped during
    ALARM or FREEZE guard states to avoid interfering with emergency handling.

    Attributes:
        active_night_plan: Most recently generated night plan (or None).
        active_evening_plan: Most recently generated evening plan (or None).
    """

    def __init__(
        self,
        planner: Planner,
        ha_api: Optional[HAApiClient],
        config: CarmaConfig,
        guard_policy: GuardPolicy,
    ) -> None:
        """Initialise PlanExecutor.

        Args:
            planner: EnergyPlanner instance for night/evening plan generation.
            ha_api: Home Assistant API client for writing plan text to HA.
                    May be None in dry-run/test mode — HA writes are skipped.
            config: Full site configuration (dashboard, EV, guards, site).
            guard_policy: Composed guard pipeline; consulted before each plan.
        """
        self._planner = planner
        self._ha_api = ha_api
        self._config = config
        self._guard_policy = guard_policy
        self.active_night_plan: Optional[Any] = None
        self.active_evening_plan: Optional[Any] = None

    async def generate(self, snapshot: SystemSnapshot) -> None:
        """Generate energy plan for the current hour.

        No-op when the current hour is not a plan hour or guard is blocking.

        Args:
            snapshot: Current system state snapshot.
        """
        # Consult GuardPolicy — skip plan during emergency states
        guard_eval = self._guard_policy.evaluate(
            batteries=snapshot.batteries,
            current_scenario=snapshot.current_scenario,
            weighted_avg_kw=snapshot.grid.weighted_avg_kw,
            hour=snapshot.hour,
            ha_connected=True,
            pv_kw=snapshot.grid.pv_total_w / _W_TO_KW,
            spot_price_ore=snapshot.grid.price_ore,
        )
        if guard_eval.level in _BLOCKING_GUARD_LEVELS:
            logger.warning(
                "PlanExecutor: skipping plan at hour %d — guard level %s",
                snapshot.hour,
                guard_eval.level.value,
            )
            return

        pc = self._planner._config
        night_h = pc.night_start_hour
        morning_h = pc.night_end_hour
        evening_h = night_h - pc.evening_offset_h
        midday_h = pc.daylight_end_hour

        hour = snapshot.hour
        bat_soc = snapshot.total_battery_soc_pct
        bat_cap = sum(b.cap_kwh for b in snapshot.batteries)
        ev = snapshot.ev
        pv_tomorrow = snapshot.grid.pv_forecast_tomorrow_kwh

        try:
            plan_text: str

            if hour == night_h:
                plan = self._planner.generate_night_plan(
                    bat_soc_pct=bat_soc,
                    bat_cap_kwh=bat_cap,
                    ev_connected=ev.connected,
                    ev_soc_pct=ev.soc_pct,
                    pv_tomorrow_kwh=pv_tomorrow,
                    prices_by_hour={},
                )
                plan_text = (
                    f"Night: EV {plan.ev_charge_need_kwh:.0f}kWh "
                    f"({plan.ev_start_hour}-{plan.ev_stop_hour}h {plan.ev_amps}A) "
                    f"Bat {plan.bat_charge_need_kwh:.0f}kWh "
                    f"({plan.bat_charge_start_hour}-{plan.bat_charge_stop_hour}h) "
                    f"{'skip EV: ' + plan.ev_skip_reason if plan.ev_skip else ''}"
                )
                logger.info("PLAN 22:00 — %s", plan_text)
                self.active_night_plan = plan

            elif hour == evening_h:
                eve_plan = self._planner.generate_evening_plan(
                    bat_soc_pct=bat_soc,
                    bat_cap_kwh=bat_cap,
                    ev_connected=ev.connected,
                    ev_soc_pct=ev.soc_pct,
                )
                plan_text = (
                    f"Evening: alloc {eve_plan.evening_allocation_kwh:.1f}kWh "
                    f"floor {eve_plan.evening_floor_soc_pct:.0f}%"
                )
                logger.info("PLAN 17:00 — %s", plan_text)
                self.active_evening_plan = eve_plan

            elif hour == morning_h:
                plan_text = (
                    f"Morning: bat {bat_soc:.0f}% "
                    f"EV {ev.soc_pct:.0f}% "
                    f"PV today {snapshot.grid.pv_forecast_today_kwh:.0f}kWh"
                )
                logger.info("PLAN 06:00 — %s", plan_text)

            elif hour == midday_h:
                plan_text = (
                    f"Midday: bat {bat_soc:.0f}% "
                    f"PV remaining {snapshot.grid.pv_forecast_today_kwh:.0f}kWh "
                    f"tomorrow {pv_tomorrow:.0f}kWh"
                )
                logger.info("PLAN 12:00 — %s", plan_text)

            else:
                return

            # Generate 48h hourly plan at 22:00 and 06:00
            if hour in (night_h, morning_h):
                today_plan, tomorrow_plan = self.generate_48h(snapshot, hour)
                if self._ha_api is not None:
                    dash = self._config.dashboard
                    await self._ha_api.set_input_text(
                        dash.entity_plan_today, today_plan,
                    )
                    await self._ha_api.set_input_text(
                        dash.entity_plan_tomorrow, tomorrow_plan,
                    )

            # Write summary to HA
            if self._ha_api is not None and hour in (night_h, evening_h):
                dash = self._config.dashboard
                await self._ha_api.set_input_text(
                    dash.entity_plan_today, plan_text,
                )

        except (asyncio.TimeoutError, OSError) as exc:
            logger.error(
                "Plan generation failed at %d:00 (I/O): %s",
                hour, exc, exc_info=True,
            )
        except (ValueError, KeyError) as exc:
            logger.error(
                "Plan generation failed at %d:00 (data): %s",
                hour, exc, exc_info=True,
            )

    def generate_48h(
        self,
        snapshot: SystemSnapshot,
        current_hour: int,
    ) -> tuple[str, str]:
        """Generate 48-hour hourly plan split by day.

        Args:
            snapshot: Current system state.
            current_hour: Starting hour for the 48-hour projection.

        Returns:
            (today_plan, tomorrow_plan) as pipe-separated hour strings.
            Each entry has format HH:action:SoC%.
        """
        cfg = self._config
        planner_cfg: PlannerConfig = self._planner._config
        bat_soc = snapshot.total_battery_soc_pct
        bat_cap = sum(b.cap_kwh for b in snapshot.batteries) or 1.0
        ev_soc = snapshot.ev.soc_pct
        pv_today = snapshot.grid.pv_forecast_today_kwh
        pv_tomorrow = snapshot.grid.pv_forecast_tomorrow_kwh
        night_start = cfg.grid.ellevio.night_start_hour
        night_end = cfg.grid.ellevio.night_end_hour
        ev_target = cfg.ev.daily_target_soc_pct
        min_soc = cfg.guards.g1_soc_floor.floor_pct
        grid_max_soc = cfg.night_plan.grid_charge_max_soc_pct
        pc = planner_cfg
        pv_high_kwh = pc.pv_high_threshold_kwh
        evening_start_h = night_start - pc.evening_offset_h

        ev_night_pct_h = pc.ev_night_charge_pct_per_h
        ev_weekend_pct_h = pc.ev_weekend_charge_pct_per_h
        grid_charge_pct_h = pc.grid_charge_pct_per_h
        discharge_pct_h = pc.discharge_pct_per_h
        ev_daily_drop = pc.ev_daily_usage_drop_pct
        daylight_start = pc.daylight_start_hour
        daylight_end = pc.daylight_end_hour
        daylight_hours = max(daylight_end - daylight_start, 1)
        pv_charge_min_kwh = pc.pv_min_kwh_per_h
        weekend_ev_start = pc.night_end_hour
        weekend_ev_end = pc.weekend_ev_end_hour

        today_hours: list[str] = []
        tomorrow_hours: list[str] = []
        soc = bat_soc
        ev = ev_soc
        today_date = datetime.now(
            tz=zoneinfo.ZoneInfo(cfg.site.timezone),
        ).date()

        _HOURS_PER_DAY: int = 24
        _PLAN_HORIZON_H: int = 48
        _WEEKEND_START_WEEKDAY: int = 5

        for offset in range(_PLAN_HORIZON_H):
            h = (current_hour + offset) % _HOURS_PER_DAY
            day_offset = offset // _HOURS_PER_DAY
            is_night = h >= night_start or h < night_end

            pv_h = 0.0
            if daylight_start <= h <= daylight_end:
                pv_src = pv_today if day_offset == 0 else pv_tomorrow
                pv_h = pv_src / max(daylight_hours, 1)

            if h == night_start and day_offset >= 1:
                ev = ev_target - ev_daily_drop

            plan_date = today_date + timedelta(days=day_offset)
            is_weekend = plan_date.weekday() >= _WEEKEND_START_WEEKDAY
            day_pv = pv_today if day_offset == 0 else pv_tomorrow
            action: str
            if (
                is_weekend
                and day_pv > pv_high_kwh
                and weekend_ev_start <= h < weekend_ev_end
                and ev < ev_target
            ):
                action = "EV"
                ev = min(ev + ev_weekend_pct_h, ev_target)
            elif is_night and ev < MAX_SOC_PCT:
                action = "EV"
                ev = min(ev + ev_night_pct_h, MAX_SOC_PCT)
            elif is_night and soc < grid_max_soc:
                action = "GRD"
                soc = min(soc + grid_charge_pct_h, grid_max_soc)
            elif pv_h > pv_charge_min_kwh and soc < MAX_SOC_PCT:
                action = "CHG"
                soc = min(soc + pv_h / bat_cap * MAX_SOC_PCT, MAX_SOC_PCT)
            elif evening_start_h <= h < night_start and soc > min_soc:
                action = "DIS"
                soc = max(soc - discharge_pct_h, min_soc)
            else:
                action = "STB"

            entry = f"{h:02d}:{action}:{soc:.0f}%"
            if day_offset == 0:
                today_hours.append(entry)
            else:
                tomorrow_hours.append(entry)

        return "|".join(today_hours), "|".join(tomorrow_hours)
