"""Tests for core/forecast_integrator.py (PLAT-1790).

Covers: P10 integration, margin factor, sunset detection, bat SoC prediction,
window calculation, parse_detailed_hourly, error cases, timezone handling.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from core.forecast_integrator import (
    HourlyForecastEntry,
    bat_deficit_during_ev,
    get_sunset,
    integrate_p10,
    integrate_p10_with_margin,
    parse_detailed_hourly,
    planned_window_minutes,
    predict_bat_soc_at_sunset,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _make_forecast(
    base_date: datetime,
    hours: int = 24,
    p10: float = 1.0,
    p50: float = 1.5,
) -> list[HourlyForecastEntry]:
    """Generate `hours` hourly entries starting at base_date."""
    return [
        HourlyForecastEntry(
            period_start=base_date + timedelta(hours=i),
            pv_estimate_kw=p50,
            pv_estimate10_kw=p10,
        )
        for i in range(hours)
    ]


# ── Test 1: integrate full 24h ─────────────────────────────────────────────

def test_integrate_full_day() -> None:
    """Summing 24 entries of 1.0 kW P10 → 24 kWh."""
    base = _utc(2026, 4, 23, 0)
    forecast = _make_forecast(base, hours=24, p10=1.0)
    from_ts = _utc(2026, 4, 23, 0)
    to_ts = _utc(2026, 4, 24, 0)
    result = integrate_p10(forecast, from_ts, to_ts)
    assert result == pytest.approx(24.0)


# ── Test 2: integrate partial window ──────────────────────────────────────

def test_integrate_partial_window() -> None:
    """from=12:00, to=18:00 → 6h x 1.0 kW = 6.0 kWh."""
    base = _utc(2026, 4, 23, 0)
    forecast = _make_forecast(base, hours=24, p10=1.0)
    result = integrate_p10(
        forecast,
        from_ts=_utc(2026, 4, 23, 12),
        to_ts=_utc(2026, 4, 23, 18),
    )
    assert result == pytest.approx(6.0)


# ── Test 3: P10 always <= P50 in raw entries ──────────────────────────────

def test_p10_lower_than_p50() -> None:
    """P10 (pv_estimate10_kw) must be <= P50 (pv_estimate_kw) in test data."""
    base = _utc(2026, 4, 23, 0)
    forecast = _make_forecast(base, hours=8, p10=0.8, p50=1.2)
    for entry in forecast:
        assert entry.pv_estimate10_kw <= entry.pv_estimate_kw


# ── Test 4: margin factor applied ─────────────────────────────────────────

def test_margin_factor_applied() -> None:
    """integrate_p10_with_margin returns raw x margin_factor."""
    base = _utc(2026, 4, 23, 6)
    forecast = _make_forecast(base, hours=12, p10=2.0)
    raw = integrate_p10(forecast, _utc(2026, 4, 23, 6), _utc(2026, 4, 23, 18))
    with_margin = integrate_p10_with_margin(
        forecast, _utc(2026, 4, 23, 6), _utc(2026, 4, 23, 18), margin_factor=1.2
    )
    assert with_margin == pytest.approx(raw * 1.2)


# ── Test 5: sunset detection summer ───────────────────────────────────────

def test_sunset_detection_summer() -> None:
    """Summer sunset in Stockholm should be after 20:00 local time."""
    pytest.importorskip("astral")
    sunset = get_sunset(59.33, 18.07, "Europe/Stockholm", date(2026, 6, 21))
    # Convert to local time for assertion
    import zoneinfo
    local = sunset.astimezone(zoneinfo.ZoneInfo("Europe/Stockholm"))
    assert local.hour >= 20, f"Expected summer sunset after 20:00, got {local}"


# ── Test 6: sunset detection winter ──────────────────────────────────────

def test_sunset_detection_winter() -> None:
    """Winter sunset in Stockholm should be before 16:00 local time."""
    pytest.importorskip("astral")
    sunset = get_sunset(59.33, 18.07, "Europe/Stockholm", date(2026, 12, 21))
    import zoneinfo
    local = sunset.astimezone(zoneinfo.ZoneInfo("Europe/Stockholm"))
    assert local.hour < 16, f"Expected winter sunset before 16:00, got {local}"


# ── Test 7: timezone handling ─────────────────────────────────────────────

def test_timezone_handling_stockholm_utc_offset() -> None:
    """Forecast entries in Europe/Stockholm offset are handled correctly."""
    pytest.importorskip("astral")
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Europe/Stockholm")
    # Create entry in local time (UTC+2 in summer)
    local_start = datetime(2026, 4, 23, 12, 0, tzinfo=tz)
    entry = HourlyForecastEntry(
        period_start=local_start,
        pv_estimate_kw=2.0,
        pv_estimate10_kw=1.0,
    )
    # Query in UTC: 12:00 Stockholm = 10:00 UTC
    result = integrate_p10(
        [entry],
        from_ts=datetime(2026, 4, 23, 10, 0, tzinfo=UTC),
        to_ts=datetime(2026, 4, 23, 11, 0, tzinfo=UTC),
    )
    assert result == pytest.approx(1.0)


# ── Test 8: bat deficit calculation — charging ────────────────────────────

def test_bat_deficit_calculation_during_ev_charge() -> None:
    """With EV=3kW, household=0.5kW, PV=1kW for 60min: deficit = 2.5 kWh."""
    deficit = bat_deficit_during_ev(
        ev_power_kw=3.0,
        session_duration_min=60.0,
        pv_power_kw=1.0,
        household_kw=0.5,
    )
    assert deficit == pytest.approx(2.5)


# ── Test 9: bat deficit — no EV ───────────────────────────────────────────

def test_bat_deficit_no_ev() -> None:
    """With no EV, deficit = max(0, household - PV) x duration."""
    # household=2kW, PV=3kW → net = 1kW surplus → deficit = 0
    assert bat_deficit_during_ev(0.0, 60.0, 3.0, 2.0) == pytest.approx(0.0)
    # household=3kW, PV=1kW → gap = 2kW → deficit = 2 kWh for 1h
    assert bat_deficit_during_ev(0.0, 60.0, 1.0, 3.0) == pytest.approx(2.0)


# ── Test 10: forecast error if list empty ────────────────────────────────

def test_integrate_empty_forecast_returns_zero() -> None:
    """Empty forecast list returns 0.0, not an error."""
    result = integrate_p10([], _utc(2026, 4, 23, 6), _utc(2026, 4, 23, 18))
    assert result == 0.0


# ── Test 11: bat_soc_at_sunset decreases with EV ─────────────────────────

def test_bat_soc_at_sunset_decreases_with_ev() -> None:
    """Charging EV should reduce predicted SoC at sunset compared to no EV."""
    base = _utc(2026, 4, 23, 12)
    forecast = _make_forecast(base, hours=8, p10=2.0)  # 2 kW P10 each hour
    sunset = _utc(2026, 4, 23, 20)
    now = _utc(2026, 4, 23, 12)

    soc_no_ev = predict_bat_soc_at_sunset(
        bat_soc_now=80.0,
        bat_capacity_kwh=20.0,
        forecast=forecast,
        now=now,
        sunset=sunset,
        household_kw=1.0,
        ev_power_kw=0.0,
    )
    soc_with_ev = predict_bat_soc_at_sunset(
        bat_soc_now=80.0,
        bat_capacity_kwh=20.0,
        forecast=forecast,
        now=now,
        sunset=sunset,
        household_kw=1.0,
        ev_power_kw=3.0,  # EV draws 3kW extra
    )
    assert soc_with_ev < soc_no_ev, "EV load should lower SoC at sunset"


# ── Test 12: full PV day → SoC near 100% at sunset ───────────────────────

def test_bat_soc_at_sunset_full_pv_day() -> None:
    """With strong PV and no loads, SoC should approach 100% at sunset."""
    base = _utc(2026, 4, 23, 6)
    # 8kW P10 for 14h — far exceeds any load
    forecast = _make_forecast(base, hours=14, p10=8.0)
    sunset = _utc(2026, 4, 23, 20)
    now = _utc(2026, 4, 23, 6)

    soc = predict_bat_soc_at_sunset(
        bat_soc_now=50.0,
        bat_capacity_kwh=20.0,
        forecast=forecast,
        now=now,
        sunset=sunset,
        household_kw=0.5,
    )
    assert soc == pytest.approx(100.0), f"Expected 100% but got {soc:.1f}%"


# ── Test 13 (bonus): planned_window_minutes ───────────────────────────────

def test_planned_window_minutes_viable_hours() -> None:
    """Only hours where P10 - household >= min_surplus count."""
    base = _utc(2026, 4, 23, 10)
    forecast = [
        HourlyForecastEntry(base + timedelta(hours=i), 2.0, p10)
        for i, p10 in enumerate([0.5, 2.0, 3.0, 1.5, 0.3])  # hours 10-14
    ]
    sunset = _utc(2026, 4, 23, 20)
    now = _utc(2026, 4, 23, 10)

    # min_surplus=1.0 kW, household=0.8 kW → viable if P10 >= 1.8
    # P10 values: 0.5, 2.0, 3.0, 1.5, 0.3 → viable: 2.0, 3.0 → 2 hours = 120 min
    minutes = planned_window_minutes(forecast, now, sunset, min_surplus_kw=1.0, household_kw=0.8)
    assert minutes == pytest.approx(120.0)


# ── Test 14 (bonus): parse_detailed_hourly ──────────────────────────────

def test_parse_detailed_hourly_happy_path() -> None:
    """Correctly parse Solcast detailedHourly attribute format."""
    raw = [
        {"period_start": "2026-04-23T10:00:00+00:00", "pv_estimate": 2.0, "pv_estimate10": 1.2},
        {"period_start": "2026-04-23T11:00:00+00:00", "pv_estimate": 2.5, "pv_estimate10": 1.5},
    ]
    entries = parse_detailed_hourly(raw)
    assert len(entries) == 2
    assert entries[0].pv_estimate10_kw == pytest.approx(1.2)
    assert entries[1].pv_estimate_kw == pytest.approx(2.5)


def test_parse_detailed_hourly_skips_invalid_period() -> None:
    """Entries with invalid period_start are silently skipped."""
    raw = [
        {"period_start": "not-a-date", "pv_estimate": 2.0, "pv_estimate10": 1.0},
        {"period_start": "2026-04-23T12:00:00+00:00", "pv_estimate": 3.0, "pv_estimate10": 2.0},
    ]
    entries = parse_detailed_hourly(raw)
    assert len(entries) == 1
    assert entries[0].pv_estimate10_kw == pytest.approx(2.0)


def test_parse_detailed_hourly_no_tz_assumes_utc() -> None:
    """ISO string without timezone assumes UTC."""
    raw = [{"period_start": "2026-04-23T10:00:00", "pv_estimate": 1.0, "pv_estimate10": 0.8}]
    entries = parse_detailed_hourly(raw)
    assert len(entries) == 1
    assert entries[0].period_start.tzinfo == UTC


def test_parse_detailed_hourly_fallback_to_p50() -> None:
    """If pv_estimate10 missing, fall back to pv_estimate."""
    raw = [{"period_start": "2026-04-23T10:00:00+00:00", "pv_estimate": 2.0}]
    entries = parse_detailed_hourly(raw)
    assert entries[0].pv_estimate10_kw == pytest.approx(2.0)
