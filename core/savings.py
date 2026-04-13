"""CARMA Box — Savings Calculator.

Pure Python. No HA imports. Fully testable.

Calculates estimated monthly savings from:
1. Peak reduction (Ellevio top-3 weighted peaks × kr/kW)
2. Price optimization (discharge at expensive hours, charge at cheap)

All values are accumulated over the current month and reset on month change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class DailySavings:
    """One day's savings for trend tracking."""

    date: str  # ISO format YYYY-MM-DD
    peak_kr: float = 0.0
    discharge_kr: float = 0.0
    grid_charge_kr: float = 0.0
    total_kr: float = 0.0


@dataclass
class SavingsState:
    """Running savings state for the current month."""

    month: int = 0
    year: int = 0

    # Peak tracking — top-N weighted hourly peaks this month
    peak_samples: list[float] = field(default_factory=list)

    # Baseline peaks — what grid import would have been without battery
    baseline_peak_samples: list[float] = field(default_factory=list)

    # Accumulated price optimization (kr)
    discharge_savings_kr: float = 0.0
    grid_charge_savings_kr: float = 0.0

    # Counters
    total_discharge_kwh: float = 0.0
    total_grid_charge_kwh: float = 0.0

    # Daily savings trend (last 30 days)
    daily_savings: list[DailySavings] = field(default_factory=list)

    # What-if: estimated total electricity cost without CARMA Box
    baseline_cost_kr: float = 0.0
    actual_cost_kr: float = 0.0

    # PLAT-924: Battery efficiency tracking
    charge_from_grid_kwh: float = 0.0
    charge_from_grid_cost_ore: float = 0.0
    discharge_offset_kwh: float = 0.0
    discharge_offset_value_ore: float = 0.0

    # PLAT-926: Grid charge price samples
    grid_charge_prices: list[float] = field(default_factory=list)


def reset_if_new_month(state: SavingsState, now: datetime) -> SavingsState:
    """Reset savings if a new month has started."""
    if state.month != now.month or state.year != now.year:
        return SavingsState(month=now.month, year=now.year)
    return state


def record_peak(
    state: SavingsState,
    weighted_kw: float,
    baseline_kw: float,
    top_n: int = 3,
) -> None:
    """Record a weighted peak sample.

    Args:
        state: Current savings state.
        weighted_kw: Actual weighted grid import (with CARMA Box).
        baseline_kw: What grid import would have been without battery.
        top_n: Number of top peaks to track (Ellevio = 3).
    """
    state.peak_samples.append(weighted_kw)
    state.peak_samples.sort(reverse=True)
    state.peak_samples = state.peak_samples[:top_n]

    state.baseline_peak_samples.append(baseline_kw)
    state.baseline_peak_samples.sort(reverse=True)
    state.baseline_peak_samples = state.baseline_peak_samples[:top_n]


def record_discharge(
    state: SavingsState,
    discharge_kwh: float,
    price_ore: float,
    avg_price_ore: float,
) -> None:
    """Record a discharge event (battery → house instead of grid).

    Savings = discharged energy × (current_price - average_price) / 100.
    The idea: we discharge at expensive hours and saved vs average cost.

    Args:
        state: Current savings state.
        discharge_kwh: Energy discharged this period (kWh).
        price_ore: Current electricity price (öre/kWh).
        avg_price_ore: Average daily price (öre/kWh).
    """
    if discharge_kwh > 0:
        savings = discharge_kwh * (price_ore - avg_price_ore) / 100  # öre → kr
        state.discharge_savings_kr += savings  # Can be negative if discharged at cheap price
        state.total_discharge_kwh += discharge_kwh


def record_grid_charge(
    state: SavingsState,
    charge_kwh: float,
    price_ore: float,
    avg_price_ore: float,
) -> None:
    """Record a grid charge event (cheap grid → battery for later).

    Savings = charged energy × (average_price - charge_price) / 100.

    Args:
        state: Current savings state.
        charge_kwh: Energy charged from grid (kWh).
        price_ore: Price during charging (öre/kWh).
        avg_price_ore: Average daily price (öre/kWh).
    """
    if charge_kwh > 0 and avg_price_ore > price_ore:
        savings = charge_kwh * (avg_price_ore - price_ore) / 100  # öre → kr
        state.grid_charge_savings_kr += savings
        state.total_grid_charge_kwh += charge_kwh


def calculate_peak_savings(
    state: SavingsState,
    cost_per_kw: float = 80.0,
    top_n: int = 3,
) -> float:
    """Calculate peak reduction savings (kr/month).

    Ellevio charges: mean(top-N peaks) × cost_per_kw per month.
    Savings = (baseline_mean - actual_mean) × cost_per_kw.

    Args:
        state: Current savings state.
        cost_per_kw: Grid operator peak cost (kr/kW/month).
        top_n: Number of top peaks (default 3 for Ellevio).

    Returns:
        Estimated savings in kr for this month.
    """
    if not state.peak_samples or not state.baseline_peak_samples:
        return 0.0

    actual_peaks = state.peak_samples[:top_n]
    baseline_peaks = state.baseline_peak_samples[:top_n]

    actual_mean = sum(actual_peaks) / len(actual_peaks)
    baseline_mean = sum(baseline_peaks) / len(baseline_peaks)

    reduction = max(0, baseline_mean - actual_mean)
    return round(reduction * cost_per_kw, 1)


def total_savings(
    state: SavingsState,
    cost_per_kw: float = 80.0,
    top_n: int = 3,
) -> float:
    """Total estimated savings this month (kr).

    Sum of peak reduction + discharge optimization + grid charge optimization.
    """
    peak = calculate_peak_savings(state, cost_per_kw, top_n)
    return round(peak + state.discharge_savings_kr + state.grid_charge_savings_kr, 1)


def record_daily_snapshot(
    state: SavingsState,
    date_str: str,
    cost_per_kw: float = 80.0,
    top_n: int = 3,
) -> None:
    """Snapshot today's savings into the daily trend list.

    Call once per day (or idempotently — same date updates in place).
    Keeps max 30 days of history.

    Args:
        state: Current savings state.
        date_str: ISO date string (YYYY-MM-DD).
        cost_per_kw: Grid operator peak cost (kr/kW/month).
        top_n: Number of top peaks.
    """
    peak_kr = calculate_peak_savings(state, cost_per_kw, top_n)
    total = round(peak_kr + state.discharge_savings_kr + state.grid_charge_savings_kr, 1)

    entry = DailySavings(
        date=date_str,
        peak_kr=round(peak_kr, 1),
        discharge_kr=round(state.discharge_savings_kr, 1),
        grid_charge_kr=round(state.grid_charge_savings_kr, 1),
        total_kr=total,
    )

    # Update or append
    for i, ds in enumerate(state.daily_savings):
        if ds.date == date_str:
            state.daily_savings[i] = entry
            return
    state.daily_savings.append(entry)
    # Keep max 30 days
    if len(state.daily_savings) > 30:
        state.daily_savings = state.daily_savings[-30:]


def record_cost_estimate(
    state: SavingsState,
    consumption_kwh: float,
    price_ore: float,
    battery_discharge_kwh: float,
) -> None:
    """Track what-if cost comparison.

    Accumulates estimated electricity cost with and without CARMA Box.

    Args:
        state: Current savings state.
        consumption_kwh: Household consumption this interval (kWh).
        price_ore: Current electricity price (öre/kWh).
        battery_discharge_kwh: Energy discharged from battery this interval (kWh).
    """
    cost_per_kwh = price_ore / 100  # öre → kr

    # Without CARMA Box: all consumption from grid
    state.baseline_cost_kr += consumption_kwh * cost_per_kwh

    # With CARMA Box: consumption minus battery discharge from grid
    grid_consumption = max(0, consumption_kwh - battery_discharge_kwh)
    state.actual_cost_kr += grid_consumption * cost_per_kwh


def peak_comparison(
    state: SavingsState,
    top_n: int = 3,
) -> dict[str, list[float]]:
    """Return top-N peaks with vs without CARMA Box.

    Returns:
        Dict with 'actual' and 'baseline' peak lists (kW), sorted descending.
    """
    actual = [round(p, 1) for p in state.peak_samples[:top_n]]
    baseline = [round(p, 1) for p in state.baseline_peak_samples[:top_n]]
    return {"actual": actual, "baseline": baseline}


def savings_breakdown(
    state: SavingsState,
    cost_per_kw: float = 80.0,
    top_n: int = 3,
) -> dict[str, float]:
    """Detailed savings breakdown."""
    peak = calculate_peak_savings(state, cost_per_kw, top_n)
    return {
        "peak_reduction_kr": peak,
        "discharge_savings_kr": round(state.discharge_savings_kr, 1),
        "grid_charge_savings_kr": round(state.grid_charge_savings_kr, 1),
        "total_kr": round(peak + state.discharge_savings_kr + state.grid_charge_savings_kr, 1),
        "total_discharge_kwh": round(state.total_discharge_kwh, 1),
        "total_grid_charge_kwh": round(state.total_grid_charge_kwh, 1),
    }


def savings_whatif(
    state: SavingsState,
    cost_per_kw: float = 80.0,
    top_n: int = 3,
) -> dict[str, float]:
    """What-if comparison: cost with vs without CARMA Box.

    Returns:
        Dict with baseline_cost_kr, actual_cost_kr, peak costs, and totals.
    """
    # Peak cost component
    actual_peaks = state.peak_samples[:top_n]
    baseline_peaks = state.baseline_peak_samples[:top_n]
    peak_cost_actual = (
        round(sum(actual_peaks) / len(actual_peaks) * cost_per_kw, 1) if actual_peaks else 0.0
    )
    peak_cost_baseline = (
        round(sum(baseline_peaks) / len(baseline_peaks) * cost_per_kw, 1) if baseline_peaks else 0.0
    )

    without_carma = round(state.baseline_cost_kr + peak_cost_baseline, 0)
    with_carma = round(state.actual_cost_kr + peak_cost_actual, 0)

    return {
        "without_carma_kr": without_carma,
        "with_carma_kr": with_carma,
        "saved_kr": round(without_carma - with_carma, 0),
    }


def daily_trend(state: SavingsState) -> list[dict[str, object]]:
    """Return daily savings trend for the last 30 days.

    Returns:
        List of dicts with date, peak_kr, discharge_kr, grid_charge_kr, total_kr.
    """
    return [
        {
            "date": ds.date,
            "peak_kr": ds.peak_kr,
            "discharge_kr": ds.discharge_kr,
            "grid_charge_kr": ds.grid_charge_kr,
            "total_kr": ds.total_kr,
        }
        for ds in state.daily_savings
    ]


def state_to_dict(state: SavingsState) -> dict[str, object]:
    """Serialize SavingsState to dict for persistent storage."""
    return {
        "month": state.month,
        "year": state.year,
        "peak_samples": list(state.peak_samples),
        "baseline_peak_samples": list(state.baseline_peak_samples),
        "discharge_savings_kr": state.discharge_savings_kr,
        "grid_charge_savings_kr": state.grid_charge_savings_kr,
        "total_discharge_kwh": state.total_discharge_kwh,
        "total_grid_charge_kwh": state.total_grid_charge_kwh,
        "daily_savings": [
            {
                "date": ds.date,
                "peak_kr": ds.peak_kr,
                "discharge_kr": ds.discharge_kr,
                "grid_charge_kr": ds.grid_charge_kr,
                "total_kr": ds.total_kr,
            }
            for ds in state.daily_savings
        ],
        "baseline_cost_kr": state.baseline_cost_kr,
        "actual_cost_kr": state.actual_cost_kr,
        "charge_from_grid_kwh": state.charge_from_grid_kwh,
        "charge_from_grid_cost_ore": state.charge_from_grid_cost_ore,
        "discharge_offset_kwh": state.discharge_offset_kwh,
        "discharge_offset_value_ore": state.discharge_offset_value_ore,
        "grid_charge_prices": list(state.grid_charge_prices[-2000:]),
    }


def state_from_dict(data: dict[str, Any]) -> SavingsState:
    """Deserialize SavingsState from dict.

    Returns a fresh SavingsState if data is invalid or empty.
    """
    if not data or not isinstance(data, dict):
        return SavingsState()
    try:
        daily = [
            DailySavings(
                date=str(d["date"]),
                peak_kr=float(d.get("peak_kr", 0)),
                discharge_kr=float(d.get("discharge_kr", 0)),
                grid_charge_kr=float(d.get("grid_charge_kr", 0)),
                total_kr=float(d.get("total_kr", 0)),
            )
            for d in data.get("daily_savings", [])
            if isinstance(d, dict)
        ]
        return SavingsState(
            month=int(data.get("month", 0)),
            year=int(data.get("year", 0)),
            peak_samples=[float(x) for x in data.get("peak_samples", [])],
            baseline_peak_samples=[float(x) for x in data.get("baseline_peak_samples", [])],
            discharge_savings_kr=float(data.get("discharge_savings_kr", 0)),
            grid_charge_savings_kr=float(data.get("grid_charge_savings_kr", 0)),
            total_discharge_kwh=float(data.get("total_discharge_kwh", 0)),
            total_grid_charge_kwh=float(data.get("total_grid_charge_kwh", 0)),
            daily_savings=daily,
            baseline_cost_kr=float(data.get("baseline_cost_kr", 0)),
            actual_cost_kr=float(data.get("actual_cost_kr", 0)),
            charge_from_grid_kwh=float(data.get("charge_from_grid_kwh", 0)),
            charge_from_grid_cost_ore=float(data.get("charge_from_grid_cost_ore", 0)),
            discharge_offset_kwh=float(data.get("discharge_offset_kwh", 0)),
            discharge_offset_value_ore=float(data.get("discharge_offset_value_ore", 0)),
            grid_charge_prices=[float(x) for x in data.get("grid_charge_prices", [])],
        )
    except (KeyError, ValueError, TypeError):
        return SavingsState()
