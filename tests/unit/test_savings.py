"""Tests for Savings Calculator — peak, discharge, grid charge tracking."""

from __future__ import annotations

from datetime import datetime

from core.savings import (
    SavingsState,
    calculate_peak_savings,
    record_daily_snapshot,
    record_discharge,
    record_grid_charge,
    record_peak,
    reset_if_new_month,
    savings_breakdown,
    state_from_dict,
    state_to_dict,
    total_savings,
)


class TestResetMonth:
    def test_resets_on_new_month(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.discharge_savings_kr = 100.0
        new = reset_if_new_month(state, datetime(2026, 4, 1))
        assert new.month == 4
        assert new.discharge_savings_kr == 0.0

    def test_keeps_same_month(self) -> None:
        state = SavingsState(month=4, year=2026)
        state.discharge_savings_kr = 50.0
        same = reset_if_new_month(state, datetime(2026, 4, 15))
        assert same.discharge_savings_kr == 50.0


class TestRecordPeak:
    def test_keeps_top_3(self) -> None:
        state = SavingsState()
        for kw in [1.0, 5.0, 3.0, 2.0, 4.0]:
            record_peak(state, kw, kw + 1.0)
        assert state.peak_samples == [5.0, 4.0, 3.0]
        assert state.baseline_peak_samples == [6.0, 5.0, 4.0]


class TestPeakSavings:
    def test_calculates_reduction(self) -> None:
        state = SavingsState()
        state.peak_samples = [2.0, 1.5, 1.0]
        state.baseline_peak_samples = [5.0, 4.0, 3.0]
        savings = calculate_peak_savings(state, cost_per_kw=80.0)
        # baseline mean=4.0, actual mean=1.5, reduction=2.5
        assert abs(savings - 200.0) < 0.1


class TestDischarge:
    def test_positive_savings(self) -> None:
        state = SavingsState()
        record_discharge(state, 2.0, 150.0, 100.0)
        # 2kWh × (150-100)/100 = 1.0 kr
        assert abs(state.discharge_savings_kr - 1.0) < 0.01

    def test_zero_kwh_ignored(self) -> None:
        state = SavingsState()
        record_discharge(state, 0.0, 150.0, 100.0)
        assert state.discharge_savings_kr == 0.0


class TestGridCharge:
    def test_positive_savings(self) -> None:
        state = SavingsState()
        record_grid_charge(state, 3.0, 30.0, 100.0)
        # 3kWh × (100-30)/100 = 2.1 kr
        assert abs(state.grid_charge_savings_kr - 2.1) < 0.01


class TestTotalSavings:
    def test_sums_all(self) -> None:
        state = SavingsState()
        state.peak_samples = [2.0]
        state.baseline_peak_samples = [4.0]
        state.discharge_savings_kr = 10.0
        state.grid_charge_savings_kr = 5.0
        t = total_savings(state, cost_per_kw=80.0)
        # peak: (4-2)*80=160, total=160+10+5=175
        assert abs(t - 175.0) < 0.1


class TestSerialization:
    def test_roundtrip(self) -> None:
        state = SavingsState(month=4, year=2026)
        record_peak(state, 3.0, 5.0)
        record_discharge(state, 1.0, 120.0, 80.0)
        record_daily_snapshot(state, "2026-04-13")

        d = state_to_dict(state)
        restored = state_from_dict(d)
        assert restored.month == 4
        assert restored.peak_samples == state.peak_samples
        assert abs(restored.discharge_savings_kr - state.discharge_savings_kr) < 0.01
        assert len(restored.daily_savings) == 1

    def test_from_empty_dict(self) -> None:
        state = state_from_dict({})
        assert state.month == 0

    def test_from_invalid(self) -> None:
        state = state_from_dict(None)  # type: ignore[arg-type]
        assert state.month == 0


class TestBreakdown:
    def test_returns_all_fields(self) -> None:
        state = SavingsState()
        state.peak_samples = [2.0]
        state.baseline_peak_samples = [3.0]
        state.discharge_savings_kr = 5.0
        b = savings_breakdown(state, cost_per_kw=80.0)
        assert "peak_reduction_kr" in b
        assert "discharge_savings_kr" in b
        assert "total_kr" in b
