"""Tests for Savings Calculator — peak, discharge, grid charge tracking."""

from __future__ import annotations

from datetime import datetime

from core.savings import (
    MAX_SAVINGS_HISTORY_DAYS,
    SavingsState,
    calculate_peak_savings,
    daily_trend,
    peak_comparison,
    record_cost_estimate,
    record_daily_snapshot,
    record_discharge,
    record_grid_charge,
    record_peak,
    reset_if_new_month,
    savings_breakdown,
    savings_whatif,
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


# ---------------------------------------------------------------------------
# Line 168: calculate_peak_savings early return on empty state
# ---------------------------------------------------------------------------


class TestPeakSavingsEdgeCases:
    """calculate_peak_savings returns 0.0 when samples are missing."""

    _COST_PER_KW: float = 80.0

    def test_empty_peak_samples_returns_zero(self) -> None:
        """No peak_samples recorded → 0.0 (line 168 early return)."""
        state = SavingsState()
        state.baseline_peak_samples = [5.0, 4.0]
        result = calculate_peak_savings(state, cost_per_kw=self._COST_PER_KW)
        assert result == 0.0

    def test_empty_baseline_returns_zero(self) -> None:
        """No baseline_peak_samples → 0.0 (line 168 early return)."""
        state = SavingsState()
        state.peak_samples = [2.0, 1.5]
        result = calculate_peak_savings(state, cost_per_kw=self._COST_PER_KW)
        assert result == 0.0


# ---------------------------------------------------------------------------
# Lines 223-225: record_daily_snapshot updates existing entry in place
# Lines 228-229: trim to MAX_SAVINGS_HISTORY_DAYS
# ---------------------------------------------------------------------------


class TestDailySnapshotEdgeCases:
    """record_daily_snapshot update-in-place and history trim."""

    _COST_PER_KW: float = 80.0
    _DATE: str = "2026-04-14"

    def test_updates_existing_entry(self) -> None:
        """Second call for same date replaces entry (lines 223-225)."""
        state = SavingsState()
        record_daily_snapshot(state, self._DATE, cost_per_kw=self._COST_PER_KW)
        state.discharge_savings_kr = 50.0
        record_daily_snapshot(state, self._DATE, cost_per_kw=self._COST_PER_KW)
        # Only one entry — not duplicated
        assert len(state.daily_savings) == 1
        assert state.daily_savings[0].discharge_kr == 50.0

    def test_trims_to_max_history(self) -> None:
        """History is trimmed to MAX_SAVINGS_HISTORY_DAYS (line 229)."""
        state = SavingsState()
        _EXTRA: int = 5
        for day in range(MAX_SAVINGS_HISTORY_DAYS + _EXTRA):
            record_daily_snapshot(state, f"2026-01-{day + 1:02d}", cost_per_kw=self._COST_PER_KW)
        assert len(state.daily_savings) == MAX_SAVINGS_HISTORY_DAYS


# ---------------------------------------------------------------------------
# Lines 248-255: record_cost_estimate
# ---------------------------------------------------------------------------


class TestRecordCostEstimate:
    """record_cost_estimate accumulates baseline and actual cost."""

    _PRICE_ORE: float = 120.0
    _CONSUMPTION_KWH: float = 5.0
    _DISCHARGE_KWH: float = 2.0

    def test_baseline_accumulates(self) -> None:
        """Baseline cost = consumption × price (lines 248-251)."""
        state = SavingsState()
        record_cost_estimate(state, self._CONSUMPTION_KWH, self._PRICE_ORE, 0.0)
        expected = self._CONSUMPTION_KWH * self._PRICE_ORE / 100
        assert abs(state.baseline_cost_kr - expected) < 0.001

    def test_actual_cost_reduced_by_discharge(self) -> None:
        """Actual cost uses consumption minus battery discharge (lines 253-255)."""
        state = SavingsState()
        record_cost_estimate(
            state, self._CONSUMPTION_KWH, self._PRICE_ORE, self._DISCHARGE_KWH
        )
        expected_actual = (
            (self._CONSUMPTION_KWH - self._DISCHARGE_KWH) * self._PRICE_ORE / 100
        )
        assert abs(state.actual_cost_kr - expected_actual) < 0.001

    def test_actual_cost_not_negative(self) -> None:
        """Discharge > consumption → grid_consumption clamped to 0 (line 254)."""
        state = SavingsState()
        _LARGE_DISCHARGE_KWH: float = 10.0
        record_cost_estimate(state, self._CONSUMPTION_KWH, self._PRICE_ORE, _LARGE_DISCHARGE_KWH)
        assert state.actual_cost_kr >= 0.0


# ---------------------------------------------------------------------------
# Lines 267-269: peak_comparison
# ---------------------------------------------------------------------------


class TestPeakComparison:
    """peak_comparison returns actual and baseline peak lists (lines 267-269)."""

    _TOP_N: int = 3

    def test_returns_actual_and_baseline(self) -> None:
        state = SavingsState()
        state.peak_samples = [4.0, 3.0, 2.0]
        state.baseline_peak_samples = [6.0, 5.0, 4.0]
        result = peak_comparison(state, top_n=self._TOP_N)
        assert result["actual"] == [4.0, 3.0, 2.0]
        assert result["baseline"] == [6.0, 5.0, 4.0]

    def test_empty_state_returns_empty_lists(self) -> None:
        state = SavingsState()
        result = peak_comparison(state)
        assert result["actual"] == []
        assert result["baseline"] == []


# ---------------------------------------------------------------------------
# Lines 300-312: savings_whatif
# ---------------------------------------------------------------------------


class TestSavingsWhatif:
    """savings_whatif computes cost comparison with/without CARMA Box."""

    _COST_PER_KW: float = 81.25

    def test_with_peaks_and_costs(self) -> None:
        """whatif returns expected keys and saved_kr ≥ 0 when peaks reduced."""
        state = SavingsState()
        state.peak_samples = [2.0, 1.5, 1.0]
        state.baseline_peak_samples = [5.0, 4.0, 3.0]
        state.baseline_cost_kr = 200.0
        state.actual_cost_kr = 150.0
        result = savings_whatif(state, cost_per_kw=self._COST_PER_KW)
        assert "without_carma_kr" in result
        assert "with_carma_kr" in result
        assert "saved_kr" in result
        assert result["without_carma_kr"] > result["with_carma_kr"]

    def test_empty_peaks_uses_zero_peak_cost(self) -> None:
        """Empty peak_samples → peak_cost = 0.0 (branches lines 302-306)."""
        state = SavingsState()
        state.baseline_cost_kr = 100.0
        state.actual_cost_kr = 80.0
        result = savings_whatif(state, cost_per_kw=self._COST_PER_KW)
        assert result["without_carma_kr"] == 100.0
        assert result["with_carma_kr"] == 80.0
        assert result["saved_kr"] == 20.0


# ---------------------------------------------------------------------------
# Line 325: daily_trend
# ---------------------------------------------------------------------------


class TestDailyTrend:
    """daily_trend returns list of dicts with expected keys (line 325)."""

    def test_returns_correct_structure(self) -> None:
        state = SavingsState()
        record_daily_snapshot(state, "2026-04-14")
        trend = daily_trend(state)
        assert len(trend) == 1
        entry = trend[0]
        assert entry["date"] == "2026-04-14"
        assert "peak_kr" in entry
        assert "discharge_kr" in entry
        assert "grid_charge_kr" in entry
        assert "total_kr" in entry

    def test_empty_state_returns_empty_list(self) -> None:
        state = SavingsState()
        assert daily_trend(state) == []


# ---------------------------------------------------------------------------
# Lines 405-406: state_from_dict exception path
# ---------------------------------------------------------------------------


class TestStateFromDictExceptionPath:
    """state_from_dict returns fresh state on invalid/corrupt data (lines 405-406)."""

    def test_corrupt_peak_samples_returns_fresh(self) -> None:
        """Non-numeric peak_samples triggers ValueError → fresh SavingsState."""
        corrupt = {
            "month": 4,
            "year": 2026,
            "peak_samples": ["not_a_number"],
        }
        state = state_from_dict(corrupt)
        assert state.month == 0

    def test_missing_nested_key_returns_fresh(self) -> None:
        """daily_savings entry missing 'date' key triggers KeyError → fresh state."""
        corrupt = {
            "month": 4,
            "year": 2026,
            "daily_savings": [{"peak_kr": 10.0}],  # missing 'date'
        }
        state = state_from_dict(corrupt)
        assert state.month == 0
