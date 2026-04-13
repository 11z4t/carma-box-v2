"""CARMA Box — Monthly Report Data Collector.

Pure Python. No HA imports. Fully testable.

Collects and structures monthly performance data for:
- Monthly PDF report generation (via hub)
- Customer insights email
- What-if analysis (savings vs baseline)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MonthlyReport:
    """Structured monthly report data."""

    month: int
    year: int

    # Peak shaving
    peak_actual_kw: float = 0.0  # Mean of top-3 actual
    peak_baseline_kw: float = 0.0  # Mean of top-3 baseline (without CARMA)
    peak_reduction_pct: float = 0.0
    peak_savings_kr: float = 0.0

    # Price optimization
    total_discharge_kwh: float = 0.0
    discharge_savings_kr: float = 0.0
    total_grid_charge_kwh: float = 0.0
    grid_charge_savings_kr: float = 0.0

    # Battery usage
    avg_daily_cycles: float = 0.0
    max_soc_reached: float = 0.0
    min_soc_reached: float = 100.0
    days_tracked: int = 0

    # EV
    ev_nights_charged: int = 0
    ev_nights_target_reached: int = 0
    ev_target_hit_pct: float = 0.0
    ev_total_kwh: float = 0.0

    # System health
    safety_guard_blocks: int = 0
    plan_accuracy_pct: float = 0.0
    uptime_pct: float = 0.0
    total_plans_generated: int = 0

    # Totals
    total_savings_kr: float = 0.0


@dataclass
class DailySample:
    """One day's data for aggregation."""

    date: str  # ISO format
    peak_kw: float = 0.0
    baseline_peak_kw: float = 0.0
    discharge_kwh: float = 0.0
    grid_charge_kwh: float = 0.0
    battery_cycles: float = 0.0
    ev_charged: bool = False
    ev_target_reached: bool = False
    ev_kwh: float = 0.0
    safety_blocks: int = 0
    plans_generated: int = 0


@dataclass
class ReportCollector:
    """Collects daily samples throughout the month."""

    month: int = 0
    year: int = 0
    samples: list[DailySample] = field(default_factory=list)
    _current_date: str = ""


def reset_if_new_month(collector: ReportCollector, now: datetime) -> ReportCollector:
    """Reset collector if a new month has started."""
    if collector.month != now.month or collector.year != now.year:
        return ReportCollector(month=now.month, year=now.year)
    return collector


def record_daily_sample(collector: ReportCollector, sample: DailySample) -> None:
    """Add or update today's sample."""
    # Replace if same date, append if new
    for i, s in enumerate(collector.samples):
        if s.date == sample.date:
            collector.samples[i] = sample
            return
    collector.samples.append(sample)


def generate_report(
    collector: ReportCollector,
    cost_per_kw: float = 80.0,
    top_n: int = 3,
) -> MonthlyReport:
    """Generate monthly report from collected daily samples.

    Args:
        collector: Daily sample collector.
        cost_per_kw: Grid operator peak cost (kr/kW/month).
        top_n: Number of top peaks for peak cost calculation.

    Returns:
        Structured monthly report.
    """
    report = MonthlyReport(month=collector.month, year=collector.year)
    samples = collector.samples

    if not samples:
        return report

    report.days_tracked = len(samples)

    # Peak analysis
    actual_peaks = sorted([s.peak_kw for s in samples], reverse=True)[:top_n]
    baseline_peaks = sorted([s.baseline_peak_kw for s in samples], reverse=True)[:top_n]

    if actual_peaks:
        report.peak_actual_kw = round(sum(actual_peaks) / len(actual_peaks), 2)
    if baseline_peaks:
        report.peak_baseline_kw = round(sum(baseline_peaks) / len(baseline_peaks), 2)

    if report.peak_baseline_kw > 0:
        reduction = report.peak_baseline_kw - report.peak_actual_kw
        report.peak_reduction_pct = round(reduction / report.peak_baseline_kw * 100, 1)
        report.peak_savings_kr = round(max(0, reduction) * cost_per_kw, 1)

    # Price optimization
    report.total_discharge_kwh = round(sum(s.discharge_kwh for s in samples), 1)
    report.total_grid_charge_kwh = round(sum(s.grid_charge_kwh for s in samples), 1)

    # Battery
    cycles = [s.battery_cycles for s in samples if s.battery_cycles > 0]
    if cycles:
        report.avg_daily_cycles = round(sum(cycles) / len(cycles), 2)

    # EV
    ev_nights = [s for s in samples if s.ev_charged]
    report.ev_nights_charged = len(ev_nights)
    report.ev_nights_target_reached = sum(1 for s in ev_nights if s.ev_target_reached)
    if report.ev_nights_charged > 0:
        report.ev_target_hit_pct = round(
            report.ev_nights_target_reached / report.ev_nights_charged * 100, 1
        )
    report.ev_total_kwh = round(sum(s.ev_kwh for s in samples), 1)

    # System health
    report.safety_guard_blocks = sum(s.safety_blocks for s in samples)
    report.total_plans_generated = sum(s.plans_generated for s in samples)

    # Totals
    report.total_savings_kr = round(
        report.peak_savings_kr + report.discharge_savings_kr + report.grid_charge_savings_kr, 1
    )

    return report


def report_to_dict(report: MonthlyReport) -> dict[str, object]:
    """Convert report to dict for JSON serialization / hub sync."""
    return {
        "month": report.month,
        "year": report.year,
        "peak_actual_kw": report.peak_actual_kw,
        "peak_baseline_kw": report.peak_baseline_kw,
        "peak_reduction_pct": report.peak_reduction_pct,
        "peak_savings_kr": report.peak_savings_kr,
        "total_discharge_kwh": report.total_discharge_kwh,
        "discharge_savings_kr": report.discharge_savings_kr,
        "total_grid_charge_kwh": report.total_grid_charge_kwh,
        "grid_charge_savings_kr": report.grid_charge_savings_kr,
        "avg_daily_cycles": report.avg_daily_cycles,
        "ev_nights_charged": report.ev_nights_charged,
        "ev_target_hit_pct": report.ev_target_hit_pct,
        "ev_total_kwh": report.ev_total_kwh,
        "safety_guard_blocks": report.safety_guard_blocks,
        "total_plans_generated": report.total_plans_generated,
        "total_savings_kr": report.total_savings_kr,
        "days_tracked": report.days_tracked,
    }
