"""Tests for Night and Evening Planner.

Covers:
- EV charge need calculation
- Battery grid charge need
- Night plan: high PV, low PV, weekend skip
- Evening plan: 50/50 split, deficit mode
- Cheapest hour sorting
- Edge cases: EV not connected, battery full
"""

from __future__ import annotations

import pytest

from core.planner import Planner, PlannerConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def planner() -> Planner:
    return Planner()


def _prices() -> dict[int, float]:
    """Night prices: 22-05 cheap, rest expensive."""
    return {
        22: 20, 23: 15, 0: 10, 1: 12, 2: 14, 3: 16, 4: 18, 5: 25,
    }


# ===========================================================================
# Night plan: high PV tomorrow
# ===========================================================================


class TestNightHighPV:
    """High PV tomorrow: skip battery charging, charge EV."""

    def test_bat_skip_high_pv(self, planner: Planner) -> None:
        plan = planner.generate_night_plan(
            bat_soc_pct=40.0, bat_cap_kwh=20.0,
            ev_connected=True, ev_soc_pct=50.0,
            pv_tomorrow_kwh=25.0, prices_by_hour=_prices(),
        )
        assert plan.bat_skip
        assert "high PV" in plan.bat_skip_reason

    def test_ev_charges_high_pv(self, planner: Planner) -> None:
        plan = planner.generate_night_plan(
            bat_soc_pct=40.0, bat_cap_kwh=20.0,
            ev_connected=True, ev_soc_pct=50.0,
            pv_tomorrow_kwh=25.0, prices_by_hour=_prices(),
        )
        assert not plan.ev_skip
        assert plan.ev_charge_need_kwh > 0


# ===========================================================================
# Night plan: low PV tomorrow
# ===========================================================================


class TestNightLowPV:
    """Low PV tomorrow: charge battery from grid, limit EV SoC jump."""

    def test_bat_charges_low_pv(self, planner: Planner) -> None:
        plan = planner.generate_night_plan(
            bat_soc_pct=30.0, bat_cap_kwh=20.0,
            ev_connected=True, ev_soc_pct=50.0,
            pv_tomorrow_kwh=5.0, prices_by_hour=_prices(),
        )
        assert not plan.bat_skip
        assert plan.bat_charge_need_kwh > 0

    def test_ev_limited_soc_jump_low_pv(self, planner: Planner) -> None:
        """Low PV: EV charge limited to max_soc_jump (20%)."""
        plan = planner.generate_night_plan(
            bat_soc_pct=30.0, bat_cap_kwh=20.0,
            ev_connected=True, ev_soc_pct=50.0,
            pv_tomorrow_kwh=5.0, prices_by_hour=_prices(),
        )
        # max_soc_jump = 20% of 92kWh = 18.4 kWh / 0.92 efficiency ≈ 20 kWh
        # Full need = (75-50)/100 * 92 / 0.92 = 25 kWh
        # Should be capped
        assert plan.ev_charge_need_kwh < 25.0


# ===========================================================================
# Night plan: weekend skip
# ===========================================================================


class TestWeekendSkip:
    """Weekend + high PV + EV > 80%: skip night EV."""

    def test_weekend_high_pv_high_ev_skips(self, planner: Planner) -> None:
        """Weekend + high PV + EV > 80% → skip. Need custom config with higher target."""
        custom_planner = Planner(PlannerConfig(ev_target_soc_pct=95.0))
        plan = custom_planner.generate_night_plan(
            bat_soc_pct=40.0, bat_cap_kwh=20.0,
            ev_connected=True, ev_soc_pct=85.0,
            pv_tomorrow_kwh=25.0, prices_by_hour=_prices(),
            is_weekend=True,
        )
        assert plan.ev_skip
        assert "weekend" in plan.ev_skip_reason

    def test_weekday_high_pv_high_ev_charges(self, planner: Planner) -> None:
        """Same conditions but weekday → should NOT skip."""
        custom_planner = Planner(PlannerConfig(ev_target_soc_pct=95.0))
        plan = custom_planner.generate_night_plan(
            bat_soc_pct=40.0, bat_cap_kwh=20.0,
            ev_connected=True, ev_soc_pct=85.0,
            pv_tomorrow_kwh=25.0, prices_by_hour=_prices(),
            is_weekend=False,
        )
        assert not plan.ev_skip


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:
    """Edge cases in night planning."""

    def test_ev_not_connected(self, planner: Planner) -> None:
        plan = planner.generate_night_plan(
            bat_soc_pct=40.0, bat_cap_kwh=20.0,
            ev_connected=False, ev_soc_pct=50.0,
            pv_tomorrow_kwh=20.0, prices_by_hour=_prices(),
        )
        assert plan.ev_skip
        assert plan.ev_charge_need_kwh == 0.0

    def test_ev_at_target(self, planner: Planner) -> None:
        plan = planner.generate_night_plan(
            bat_soc_pct=40.0, bat_cap_kwh=20.0,
            ev_connected=True, ev_soc_pct=80.0,
            pv_tomorrow_kwh=20.0, prices_by_hour=_prices(),
        )
        assert plan.ev_charge_need_kwh == 0.0

    def test_bat_already_full(self, planner: Planner) -> None:
        plan = planner.generate_night_plan(
            bat_soc_pct=95.0, bat_cap_kwh=20.0,
            ev_connected=False, ev_soc_pct=80.0,
            pv_tomorrow_kwh=5.0, prices_by_hour=_prices(),
        )
        assert plan.bat_charge_need_kwh == 0.0


# ===========================================================================
# Cheapest hours
# ===========================================================================


class TestCheapestHours:
    """Cheapest hour sorting."""

    def test_sorted_by_price(self, planner: Planner) -> None:
        plan = planner.generate_night_plan(
            bat_soc_pct=40.0, bat_cap_kwh=20.0,
            ev_connected=True, ev_soc_pct=50.0,
            pv_tomorrow_kwh=5.0, prices_by_hour=_prices(),
        )
        # Hour 0 (10 öre) should be first
        assert plan.cheapest_hours[0] == 0


# ===========================================================================
# Evening plan
# ===========================================================================


class TestEveningPlan:
    """Evening planner: 50/50 split, deficit mode."""

    def test_50_50_split(self, planner: Planner) -> None:
        plan = planner.generate_evening_plan(
            bat_soc_pct=80.0, bat_cap_kwh=20.0,
            ev_connected=False, ev_soc_pct=80.0,
        )
        if plan.bat_surplus_kwh > 0:
            assert plan.evening_allocation_kwh == pytest.approx(
                plan.morning_allocation_kwh, rel=0.1,
            )

    def test_deficit_no_discharge(self, planner: Planner) -> None:
        """Low SoC → deficit → no evening discharge."""
        plan = planner.generate_evening_plan(
            bat_soc_pct=20.0, bat_cap_kwh=20.0,
            ev_connected=True, ev_soc_pct=30.0,
        )
        assert plan.evening_allocation_kwh == 0.0
        assert plan.hourly_rate_w == 0.0

    def test_evening_floor_above_min_soc(self, planner: Planner) -> None:
        """Evening floor should be above min_soc when night need exists."""
        plan = planner.generate_evening_plan(
            bat_soc_pct=80.0, bat_cap_kwh=20.0,
            ev_connected=True, ev_soc_pct=50.0,
        )
        assert plan.evening_floor_soc_pct >= 15.0  # Always above min_soc
