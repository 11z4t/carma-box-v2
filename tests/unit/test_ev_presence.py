"""Unit tests for core.ev_presence (PLAT-1674)."""

from __future__ import annotations

from datetime import datetime

import pytest

from core.ev_presence import (
    EVPresenceConfig,
    PresenceSource,
    evaluate,
    is_weekend,
)


# Fixed dates for deterministic tests
WEEKDAY_MORNING = datetime(2026, 4, 16, 10, 0, 0)   # Thursday 10:00
WEEKDAY_EVENING = datetime(2026, 4, 16, 19, 0, 0)   # Thursday 19:00
WEEKDAY_NIGHT   = datetime(2026, 4, 17, 2, 0, 0)    # Friday 02:00 (still weeknight)
SATURDAY_MORNING = datetime(2026, 4, 18, 10, 0, 0)  # Saturday 10:00
SUNDAY_EVENING   = datetime(2026, 4, 19, 19, 0, 0)  # Sunday 19:00


@pytest.fixture
def cfg() -> EVPresenceConfig:
    return EVPresenceConfig()


def test_is_weekend_saturday(cfg: EVPresenceConfig) -> None:
    assert is_weekend(SATURDAY_MORNING, cfg) is True


def test_is_weekend_sunday(cfg: EVPresenceConfig) -> None:
    assert is_weekend(SUNDAY_EVENING, cfg) is True


def test_is_weekend_thursday(cfg: EVPresenceConfig) -> None:
    assert is_weekend(WEEKDAY_MORNING, cfg) is False


def test_weekday_morning_away_no_tracker(cfg: EVPresenceConfig) -> None:
    res = evaluate(WEEKDAY_MORNING, config=cfg)
    assert res.is_home is False
    assert res.source == PresenceSource.SCHEDULE_WEEKDAY_AWAY_HOURS


def test_weekday_evening_home_no_tracker(cfg: EVPresenceConfig) -> None:
    res = evaluate(WEEKDAY_EVENING, config=cfg)
    assert res.is_home is True
    assert res.source == PresenceSource.SCHEDULE_WEEKDAY_HOME_HOURS


def test_weekday_night_home_no_tracker(cfg: EVPresenceConfig) -> None:
    """Friday 02:00 should be home (still in night window)."""
    res = evaluate(WEEKDAY_NIGHT, config=cfg)
    assert res.is_home is True


def test_weekend_morning_home_no_tracker(cfg: EVPresenceConfig) -> None:
    res = evaluate(SATURDAY_MORNING, config=cfg)
    assert res.is_home is True
    assert res.source == PresenceSource.SCHEDULE_WEEKEND


def test_device_tracker_home_overrides_schedule(cfg: EVPresenceConfig) -> None:
    """device_tracker=home wins even at weekday 10:00."""
    res = evaluate(
        WEEKDAY_MORNING, config=cfg, device_tracker_state="home",
    )
    assert res.is_home is True
    assert res.source == PresenceSource.DEVICE_TRACKER_HOME


def test_device_tracker_not_home_overrides_schedule(cfg: EVPresenceConfig) -> None:
    """device_tracker=not_home wins even at weekend 10:00."""
    res = evaluate(
        SATURDAY_MORNING, config=cfg, device_tracker_state="not_home",
    )
    assert res.is_home is False
    assert res.source == PresenceSource.DEVICE_TRACKER_AWAY


def test_unknown_tracker_falls_through_to_schedule(cfg: EVPresenceConfig) -> None:
    """Unknown tracker state should NOT override — fall through to schedule."""
    res = evaluate(
        WEEKDAY_EVENING, config=cfg, device_tracker_state="unknown",
    )
    # Schedule says home at 19:00 weekday
    assert res.is_home is True


def test_weekend_full_day_disabled() -> None:
    """If weekend_home_full_day=False, weekend follows weekday window."""
    cfg = EVPresenceConfig(weekend_home_full_day=False)
    res = evaluate(SATURDAY_MORNING, config=cfg)
    # Saturday 10:00 — outside weekday home window (17 to 8)
    assert res.is_home is False


def test_custom_weekday_home_hours() -> None:
    """Custom weekday window from 16:00 to 9:00."""
    cfg = EVPresenceConfig(
        weekday_home_from_hour=16, weekday_home_until_hour=9,
    )
    # 16:00 should be home
    assert evaluate(
        datetime(2026, 4, 16, 16, 0, 0), config=cfg,
    ).is_home is True
    # 15:59 (15:00) outside
    assert evaluate(
        datetime(2026, 4, 16, 15, 0, 0), config=cfg,
    ).is_home is False
    # 08:59 (08:00) home
    assert evaluate(
        datetime(2026, 4, 16, 8, 0, 0), config=cfg,
    ).is_home is True
    # 09:00 outside
    assert evaluate(
        datetime(2026, 4, 16, 9, 0, 0), config=cfg,
    ).is_home is False
