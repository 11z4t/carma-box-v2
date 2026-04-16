"""EV presence — detects whether the EV is home given time + optional device_tracker.

PLAT-1673: Used by NightPlanner to decide if PV-surplus tomorrow can go to EV
or only to bat. Workday mornings the EV is typically away (commute), weekends
the EV stays home.

Pure module — no I/O. Caller provides current time + optional device_tracker
state. Defaults are conservative: assume EV away during weekday office hours
unless device_tracker says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, unique


@unique
class PresenceSource(Enum):
    """Where the presence decision came from — for explainability."""

    DEVICE_TRACKER_HOME = "device_tracker_home"
    DEVICE_TRACKER_AWAY = "device_tracker_away"
    DEVICE_TRACKER_UNKNOWN_FALLBACK = "device_tracker_unknown_fallback"
    SCHEDULE_WEEKEND = "schedule_weekend"
    SCHEDULE_WEEKDAY_HOME_HOURS = "schedule_weekday_home_hours"
    SCHEDULE_WEEKDAY_AWAY_HOURS = "schedule_weekday_away_hours"


@dataclass(frozen=True)
class EVPresenceConfig:
    """Configuration for EV presence detection.

    Hours are local-time integers 0..23. Weekday "home hours" defines the
    window when EV is expected at home on weekdays. Convention: home from
    `weekday_home_from_hour` (evening) wrapping past midnight to
    `weekday_home_until_hour` (morning).

    Example: from_hour=17, until_hour=8 → home 17:00-23:59 + 00:00-07:59.
    """

    weekday_home_from_hour: int = 17
    weekday_home_until_hour: int = 8
    weekend_home_full_day: bool = True
    saturday_weekday: int = 5  # ISO: Saturday=6 but Python weekday()==5
    sunday_weekday: int = 6


@dataclass(frozen=True)
class EVPresenceResult:
    """Result of presence evaluation — explainable."""

    is_home: bool
    source: PresenceSource
    reason: str = ""


def is_weekend(now: datetime, cfg: EVPresenceConfig) -> bool:
    """Return True if `now` is on a weekend day (Sat or Sun)."""
    wd = now.weekday()
    return wd == cfg.saturday_weekday or wd == cfg.sunday_weekday


def evaluate(
    now: datetime,
    *,
    config: EVPresenceConfig,
    device_tracker_state: str | None = None,
) -> EVPresenceResult:
    """Decide whether the EV is currently home.

    Order of precedence:
      1. device_tracker_state if provided and definitive ('home' or 'not_home')
      2. Weekend → always home (configurable)
      3. Weekday → home only inside weekday_home_from_hour..weekday_home_until_hour

    Unknown device_tracker states fall through to the schedule (defensive).
    """
    if device_tracker_state == "home":
        return EVPresenceResult(
            is_home=True,
            source=PresenceSource.DEVICE_TRACKER_HOME,
            reason="device_tracker says home",
        )
    if device_tracker_state in ("not_home", "away"):
        return EVPresenceResult(
            is_home=False,
            source=PresenceSource.DEVICE_TRACKER_AWAY,
            reason=f"device_tracker={device_tracker_state}",
        )

    # Schedule-based fallback
    if config.weekend_home_full_day and is_weekend(now, config):
        return EVPresenceResult(
            is_home=True,
            source=PresenceSource.SCHEDULE_WEEKEND,
            reason=f"weekday={now.weekday()} (weekend)",
        )

    hour = now.hour
    from_h = config.weekday_home_from_hour
    until_h = config.weekday_home_until_hour
    # Window wraps past midnight: home if hour >= from_h OR hour < until_h
    is_home = hour >= from_h or hour < until_h

    if is_home:
        source = PresenceSource.SCHEDULE_WEEKDAY_HOME_HOURS
        reason = f"weekday hour={hour} inside [{from_h}-24)+[0-{until_h})"
    else:
        source = PresenceSource.SCHEDULE_WEEKDAY_AWAY_HOURS
        reason = f"weekday hour={hour} outside home window"

    fallback = (
        PresenceSource.DEVICE_TRACKER_UNKNOWN_FALLBACK
        if device_tracker_state is not None
        else source
    )
    return EVPresenceResult(is_home=is_home, source=fallback, reason=reason)
