"""Tests for Ellevio peak tracking."""

from __future__ import annotations

from datetime import datetime

from core.ellevio import EllevioConfig, EllevioTracker


class TestEllevioTracker:
    def test_accumulates_samples(self) -> None:
        t = EllevioTracker()
        now = datetime(2026, 4, 14, 12, 0, 0)
        t.update(2.5, now)
        t.update(3.0, now.replace(second=30))
        assert t.current_weighted_avg_kw > 0

    def test_hour_close_records_peak(self) -> None:
        t = EllevioTracker()
        # Fill hour 12
        for s in range(0, 60, 1):
            t.update(2.0, datetime(2026, 4, 14, 12, s, 0))
        # Move to hour 13 → closes hour 12
        t.update(1.0, datetime(2026, 4, 14, 13, 0, 0))
        assert len(t.state.top_peaks) == 1
        assert t.state.top_peaks[0] > 0

    def test_top_3_sorted(self) -> None:
        t = EllevioTracker()
        # Simulate 4 hours with different loads
        for h, load in [(10, 1.0), (11, 3.0), (12, 2.0), (13, 4.0)]:
            for m in range(60):
                t.update(load, datetime(2026, 4, 14, h, m, 0))
        # Close last by advancing
        t.update(0, datetime(2026, 4, 14, 14, 0, 0))
        assert len(t.state.top_peaks) == 3
        assert t.state.top_peaks[0] >= t.state.top_peaks[1]
        assert t.state.top_peaks[1] >= t.state.top_peaks[2]

    def test_night_weight(self) -> None:
        cfg = EllevioConfig(night_weight=0.5)
        t = EllevioTracker(cfg)
        # Night hour (23:00)
        for m in range(60):
            t.update(4.0, datetime(2026, 4, 14, 23, m, 0))
        t.update(0, datetime(2026, 4, 15, 0, 0, 0))
        # 4.0 * 0.5 = 2.0 weighted
        assert abs(t.state.top_peaks[0] - 2.0) < 0.1

    def test_day_weight(self) -> None:
        cfg = EllevioConfig(day_weight=1.0)
        t = EllevioTracker(cfg)
        for m in range(60):
            t.update(3.0, datetime(2026, 4, 14, 14, m, 0))
        t.update(0, datetime(2026, 4, 14, 15, 0, 0))
        assert abs(t.state.top_peaks[0] - 3.0) < 0.1

    def test_month_reset(self) -> None:
        t = EllevioTracker()
        t.state.month = 3
        t.state.year = 2026
        t.state.top_peaks = [5.0, 4.0, 3.0]
        # New month
        t.update(1.0, datetime(2026, 4, 1, 0, 0, 0))
        assert t.state.month == 4
        assert len(t.state.top_peaks) == 0

    def test_hit_rate(self) -> None:
        t = EllevioTracker(EllevioConfig(tak_kw=3.0))
        t.state.hours_total = 10
        t.state.hours_under_target = 8
        assert abs(t.state.hit_rate_pct - 80.0) < 0.1

    def test_serialization(self) -> None:
        t = EllevioTracker()
        t.state.month = 4
        t.state.year = 2026
        t.state.top_peaks = [3.5, 2.8, 2.1]
        t.state.hours_total = 100
        t.state.hours_under_target = 90
        d = t.to_dict()
        t2 = EllevioTracker()
        t2.from_dict(d)
        assert t2.state.top_peaks == [3.5, 2.8, 2.1]
        assert t2.state.hit_rate_pct == 90.0

    def test_negative_import_ignored(self) -> None:
        t = EllevioTracker()
        for m in range(60):
            t.update(-2.0, datetime(2026, 4, 14, 12, m, 0))
        t.update(0, datetime(2026, 4, 14, 13, 0, 0))
        assert t.state.top_peaks[0] == 0.0

    def test_cost_per_kw_from_config(self) -> None:
        """monthly_cost_kr must use config cost_per_kw, not hardcoded 81.25."""
        cfg = EllevioConfig(cost_per_kw=100.0)
        t = EllevioTracker(cfg)
        t.state.top_peaks = [2.0, 3.0, 4.0]
        # avg = 3.0, cost = 3.0 * 100.0 = 300.0 (NOT 243.75 = 3.0 * 81.25)
        assert abs(t.monthly_cost_kr - 300.0) < 0.01
