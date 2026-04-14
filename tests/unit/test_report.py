"""Tests for report.py — MonthlyReport generation."""

from __future__ import annotations

from datetime import datetime

from core.report import (
    DailySample,
    MonthlyReport,
    ReportCollector,
    generate_report,
    record_daily_sample,
    report_to_dict,
    reset_if_new_month,
)


class TestReportCollector:
    def test_reset_on_new_month(self) -> None:
        c = ReportCollector(month=3, year=2026)
        c.samples.append(DailySample(date="2026-03-15"))
        new = reset_if_new_month(c, datetime(2026, 4, 1))
        assert new.month == 4
        assert len(new.samples) == 0

    def test_no_reset_same_month(self) -> None:
        c = ReportCollector(month=4, year=2026)
        c.samples.append(DailySample(date="2026-04-01"))
        same = reset_if_new_month(c, datetime(2026, 4, 15))
        assert len(same.samples) == 1

    def test_record_daily_sample(self) -> None:
        c = ReportCollector(month=4, year=2026)
        s = DailySample(
            date="2026-04-14",
            peak_kw=3.5,
            baseline_peak_kw=5.0,
            discharge_kwh=10.0,
        )
        record_daily_sample(c, s)
        assert len(c.samples) == 1


class TestGenerateReport:
    def test_generates_with_samples(self) -> None:
        c = ReportCollector(month=4, year=2026)
        for day in range(1, 8):
            record_daily_sample(c, DailySample(
                date=f"2026-04-{day:02d}",
                peak_kw=2.5,
                baseline_peak_kw=4.0,
                discharge_kwh=10.0,
                grid_charge_kwh=3.0,
                ev_kwh=5.0,
                ev_charged=True,
                ev_target_reached=True,
            ))
        report = generate_report(c)
        assert report.month == 4
        assert report.year == 2026
        assert report.days_tracked == 7
        assert report.total_discharge_kwh > 0

    def test_generates_empty(self) -> None:
        c = ReportCollector(month=4, year=2026)
        report = generate_report(c)
        assert report.days_tracked == 0


class TestReportToDict:
    def test_serializes(self) -> None:
        r = MonthlyReport(month=4, year=2026, total_savings_kr=150.0)
        d = report_to_dict(r)
        assert d["month"] == 4
        assert d["total_savings_kr"] == 150.0
        assert isinstance(d, dict)
