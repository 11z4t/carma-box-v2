"""CARMA Box — EV forecast integrator (PLAT-1790).

Reads Solcast P10 hourly data and computes EV session viability metrics:
- integrate_solcast_p10(): total kWh available in a time window
- predict_bat_soc_at_sunset(): estimated battery SoC when PV stops
- bat_deficit_during_ev(): kWh battery must cover if EV charges for a window

All public functions are pure (take data as arguments, no HA calls) except
ForecastIntegrator.fetch_from_ha() which requires HA state access.

No HA imports in this file — HA-specific reading is done in adapters/solcast.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

_LOGGER = logging.getLogger(__name__)

# Default voltage per phase (V)
_PHASE_VOLTAGE = 230.0


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class HourlyForecastEntry:
    """One hour of PV forecast data from Solcast detailedHourly."""

    period_start: datetime
    """UTC-aware start of the forecast period."""

    pv_estimate_kw: float
    """P50 (median) PV production in kW."""

    pv_estimate10_kw: float
    """P10 (pessimistic / worst-case) PV production in kW."""


@dataclass
class ForecastIntegratorConfig:
    """Configuration for ForecastIntegrator.

    Lat/lon/timezone used for astral sunset calculation.
    """

    latitude: float
    """Site latitude (degrees N)."""

    longitude: float
    """Site longitude (degrees E)."""

    timezone: str
    """IANA timezone string, e.g. 'Europe/Stockholm'."""

    margin_factor: float = 1.2
    """Safety buffer multiplier applied to P10 energy before comparing to deficit."""

    bat_capacity_kwh: float = 20.0
    """Total battery capacity (kWh), used for SoC calculations."""

    bat_efficiency: float = 0.90
    """Round-trip battery efficiency."""


class ForecastIntegrationError(Exception):
    """Raised when forecast data is missing or unusable."""


# ── Sunset helper ────────────────────────────────────────────────────────────

def get_sunset(
    latitude: float,
    longitude: float,
    timezone_str: str,
    for_date: date,
) -> datetime:
    """Compute sunset time for a given date and location.

    Uses astral library for accurate astronomical calculation.

    Args:
        latitude: Site latitude in degrees N.
        longitude: Site longitude in degrees E.
        timezone_str: IANA timezone string (e.g. 'Europe/Stockholm').
        for_date: The date to compute sunset for.

    Returns:
        Timezone-aware datetime of sunset in the local timezone.

    Raises:
        ForecastIntegrationError: If astral is unavailable.
    """
    try:
        from astral import LocationInfo
        from astral.sun import sun
    except ImportError as exc:
        raise ForecastIntegrationError(
            "astral package required for sunset calculation"
        ) from exc

    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(timezone_str)
    except Exception as exc:
        raise ForecastIntegrationError(
            f"Invalid timezone: {timezone_str!r}"
        ) from exc

    location = LocationInfo(
        name="site",
        region="",
        timezone=timezone_str,
        latitude=latitude,
        longitude=longitude,
    )
    from typing import cast
    sun_times = sun(location.observer, date=for_date, tzinfo=tz)
    return cast(datetime, sun_times["sunset"])


# ── Forecast integration ─────────────────────────────────────────────────────

def integrate_p10(
    forecast: list[HourlyForecastEntry],
    from_ts: datetime,
    to_ts: datetime,
) -> float:
    """Sum P10 PV production in kWh over the [from_ts, to_ts) window.

    Each entry covers exactly one hour (kW → kWh implicitly).
    Entries with period_start outside [from_ts, to_ts) are excluded.

    Args:
        forecast: List of hourly forecast entries (any timezone).
        from_ts: Window start (timezone-aware).
        to_ts: Window end (timezone-aware).

    Returns:
        Total P10 kWh in the window. Returns 0.0 if no entries match.
    """
    if not forecast:
        return 0.0

    # Normalise bounds to UTC for comparison
    from_utc = from_ts.astimezone(UTC)
    to_utc = to_ts.astimezone(UTC)

    total_kwh = 0.0
    for entry in forecast:
        entry_start = entry.period_start.astimezone(UTC)
        # Include if period starts in [from_utc, to_utc)
        if from_utc <= entry_start < to_utc:
            total_kwh += max(0.0, entry.pv_estimate10_kw)

    return total_kwh


def integrate_p10_with_margin(
    forecast: list[HourlyForecastEntry],
    from_ts: datetime,
    to_ts: datetime,
    margin_factor: float,
) -> float:
    """Integrate P10 and apply safety margin (R10).

    Returns P10 total x margin_factor.
    """
    raw = integrate_p10(forecast, from_ts, to_ts)
    return raw * margin_factor


# ── Battery deficit calculation ──────────────────────────────────────────────

def bat_deficit_during_ev(
    ev_power_kw: float,
    session_duration_min: float,
    pv_power_kw: float,
    household_kw: float,
) -> float:
    """Estimate battery kWh that must be discharged to keep grid ≤ 0 during EV charging.

    The grid invariant (R1) says grid must stay within ±100W.
    If PV cannot cover (household + EV), the battery must cover the gap.

    deficit = max(0, household_kw + ev_power_kw - pv_power_kw) x duration_h

    Args:
        ev_power_kw: EV charging power in kW.
        session_duration_min: Planned session duration in minutes.
        pv_power_kw: Expected average PV production during session (kW).
        household_kw: Expected average household load during session (kW).

    Returns:
        Battery deficit in kWh (always >= 0.0).
    """
    duration_h = session_duration_min / 60.0
    gap_kw = max(0.0, household_kw + ev_power_kw - pv_power_kw)
    return gap_kw * duration_h


def predict_bat_soc_at_sunset(
    bat_soc_now: float,
    bat_capacity_kwh: float,
    forecast: list[HourlyForecastEntry],
    now: datetime,
    sunset: datetime,
    household_kw: float,
    ev_power_kw: float = 0.0,
    bat_efficiency: float = 0.90,
) -> float:
    """Predict battery SoC (%) at sunset given current conditions.

    Simulates hourly energy flow from now to sunset:
      - PV charges battery (P10 estimate — conservative)
      - Household load discharges battery
      - EV load discharges battery (if ev_power_kw > 0)
      - Clamps result to [0, 100]

    Args:
        bat_soc_now: Current battery SoC (%).
        bat_capacity_kwh: Battery capacity (kWh).
        forecast: Hourly P10 forecast entries.
        now: Current UTC-aware time.
        sunset: UTC-aware sunset time.
        household_kw: Average household load (kW).
        ev_power_kw: EV charging load (kW, 0 if not charging).
        bat_efficiency: Round-trip efficiency for charging.

    Returns:
        Predicted SoC at sunset (%). Clamped to [0.0, 100.0].
    """
    if bat_capacity_kwh <= 0:
        return bat_soc_now

    now_utc = now.astimezone(UTC)
    sunset_utc = sunset.astimezone(UTC)

    if sunset_utc <= now_utc:
        return bat_soc_now

    bat_kwh = bat_soc_now / 100.0 * bat_capacity_kwh

    # Walk hour-by-hour from now to sunset
    cursor = now_utc.replace(minute=0, second=0, microsecond=0)
    if cursor < now_utc:
        cursor = cursor + timedelta(hours=1)

    while cursor < sunset_utc:
        next_hour = cursor + timedelta(hours=1)
        # Fraction of the hour that falls before sunset
        effective_end = min(next_hour, sunset_utc)
        frac = (effective_end - cursor).total_seconds() / 3600.0

        # PV for this hour (P10 — conservative)
        pv_kw = 0.0
        for entry in forecast:
            entry_utc = entry.period_start.astimezone(UTC)
            if entry_utc <= cursor < entry_utc + timedelta(hours=1):
                pv_kw = max(0.0, entry.pv_estimate10_kw)
                break

        # Energy balance for this fraction of the hour
        net_kw = pv_kw - household_kw - ev_power_kw
        delta_kwh = net_kw * frac

        if delta_kwh > 0:
            bat_kwh += delta_kwh * bat_efficiency
        else:
            bat_kwh += delta_kwh  # discharge (efficiency loss already in delivery)

        cursor = next_hour

    soc = (bat_kwh / bat_capacity_kwh) * 100.0
    return max(0.0, min(100.0, soc))


def planned_window_minutes(
    forecast: list[HourlyForecastEntry],
    now: datetime,
    sunset: datetime,
    min_surplus_kw: float,
    household_kw: float,
) -> float:
    """Estimate how many minutes of EV-viable surplus remain until sunset.

    A hour is "viable" if pv_estimate10_kw - household_kw >= min_surplus_kw.

    Args:
        forecast: Hourly P10 forecast entries.
        now: Current time (UTC-aware).
        sunset: Sunset time (UTC-aware).
        min_surplus_kw: Minimum net surplus to qualify as viable (kW).
        household_kw: Average household load (kW).

    Returns:
        Total viable minutes remaining (0.0 if none).
    """
    now_utc = now.astimezone(UTC)
    sunset_utc = sunset.astimezone(UTC)

    viable_h = 0.0
    for entry in forecast:
        entry_utc = entry.period_start.astimezone(UTC)
        entry_end = entry_utc + timedelta(hours=1)

        if entry_end <= now_utc:
            continue  # past
        if entry_utc >= sunset_utc:
            break  # after sunset

        net = entry.pv_estimate10_kw - household_kw
        if net < min_surplus_kw:
            continue

        # Clip to [now, sunset]
        start = max(entry_utc, now_utc)
        end = min(entry_end, sunset_utc)
        viable_h += (end - start).total_seconds() / 3600.0

    return viable_h * 60.0


# ── Convenience helper ───────────────────────────────────────────────────────

def parse_detailed_hourly(raw: list[dict[str, object]]) -> list[HourlyForecastEntry]:
    """Parse Solcast detailedHourly attribute into HourlyForecastEntry list.

    Args:
        raw: List of dicts from HA state attribute 'detailedHourly'.

    Returns:
        Parsed entries. Entries with unparseable period_start are skipped.
    """
    result: list[HourlyForecastEntry] = []
    for item in raw:
        period_str = item.get("period_start", "")
        if not isinstance(period_str, str):
            continue
        try:
            period_start = datetime.fromisoformat(period_str)
            if period_start.tzinfo is None:
                # Assume UTC if no timezone info
                period_start = period_start.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            _LOGGER.debug("Skipping invalid period_start: %r", period_str)
            continue

        pv_estimate10_kw = float(item.get("pv_estimate10", item.get("pv_estimate", 0.0)))  # type: ignore[arg-type]
        pv_estimate_kw = float(item.get("pv_estimate", 0.0))  # type: ignore[arg-type]
        result.append(
            HourlyForecastEntry(
                period_start=period_start,
                pv_estimate_kw=max(0.0, pv_estimate_kw),
                pv_estimate10_kw=max(0.0, pv_estimate10_kw),
            )
        )
    return result
