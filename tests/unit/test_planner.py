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
        """Use large battery with low baseload to ensure surplus > 0."""
        cfg = PlannerConfig(house_baseload_kw=0.5, night_hours=4)
        p = Planner(cfg)
        plan = p.generate_evening_plan(
            bat_soc_pct=80.0, bat_cap_kwh=20.0,
            ev_connected=False, ev_soc_pct=80.0,
        )
        assert plan.bat_surplus_kwh > 0
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


# ===========================================================================
# Coverage: planner helper branches
# ===========================================================================


class TestPlannerCoverage:
    """Tests targeting specific uncovered branches."""

    def test_calculate_ev_charge_need_at_target_returns_zero(self) -> None:
        """_calculate_ev_charge_need returns 0.0 when SoC >= target (line 288)."""
        planner = Planner()
        result = planner._calculate_ev_charge_need(80.0)  # target=75% by default
        assert result == 0.0

    def test_calculate_bat_charge_need_at_max_returns_zero(self) -> None:
        """_calculate_bat_charge_need returns 0.0 when SoC >= grid_charge_max (line 299)."""
        planner = Planner()
        # grid_charge_max_soc_pct=90 by default, bat_soc=90 → soc_gap=0
        result = planner._calculate_bat_charge_need(90.0, 20.0, 5.0)
        assert result == 0.0

    def test_evening_plan_50_50_allocation_computed(self) -> None:
        """Evening plan should compute 50/50 allocation when surplus exists (lines 260-269)."""
        cfg = PlannerConfig(house_baseload_kw=0.2, night_hours=2)
        planner = Planner(cfg)
        plan = planner.generate_evening_plan(
            bat_soc_pct=90.0, bat_cap_kwh=20.0,
            ev_connected=False, ev_soc_pct=80.0,
        )
        assert plan.bat_surplus_kwh > 0
        assert plan.evening_allocation_kwh > 0
        assert plan.hourly_rate_w > 0


# ===========================================================================
# PLAT-1358: Named config coefficients replace magic numbers
# ===========================================================================


class TestPlat1358PlannerConfigCoefficients:
    """PLAT-1358: All planner magic numbers must be named in PlannerConfig."""

    def test_pv_bat_contribution_factor_exists(self) -> None:
        """0.5 PV contribution must be named pv_bat_contribution_factor."""
        cfg = PlannerConfig()
        assert hasattr(cfg, "pv_bat_contribution_factor")
        assert cfg.pv_bat_contribution_factor == 0.5

    def test_ev_bat_contribution_pct_exists(self) -> None:
        """0.3 EV/battery contribution must be named ev_bat_contribution_pct."""
        cfg = PlannerConfig()
        assert hasattr(cfg, "ev_bat_contribution_pct")
        assert cfg.ev_bat_contribution_pct == 0.3

    def test_evening_discharge_hours_exists(self) -> None:
        """5.0 evening hours must be named evening_discharge_hours."""
        cfg = PlannerConfig()
        assert hasattr(cfg, "evening_discharge_hours")
        assert cfg.evening_discharge_hours == 5.0

    def test_grid_voltage_v_exists(self) -> None:
        """230V grid voltage must be named grid_voltage_v."""
        cfg = PlannerConfig()
        assert hasattr(cfg, "grid_voltage_v")
        assert cfg.grid_voltage_v == 230.0

    def test_ev_phases_exists(self) -> None:
        """3 EV phases must be named ev_phases."""
        cfg = PlannerConfig()
        assert hasattr(cfg, "ev_phases")
        assert cfg.ev_phases == 3

    def test_ev_amps_uses_config_voltage_and_phases(self) -> None:
        """ev_amps in NightPlan is calculated from config grid_voltage_v and ev_phases."""
        # Use 1-phase config to verify the formula uses config, not hardcoded 230*3
        cfg = PlannerConfig(
            ev_charge_kw=2.3,   # 2300W
            grid_voltage_v=230.0,
            ev_phases=1,        # single phase: 2300 / 230 = 10A
        )
        planner = Planner(cfg)
        prices = {h: 50.0 for h in range(24)}
        plan = planner.generate_night_plan(
            bat_soc_pct=50.0, bat_cap_kwh=20.0,
            ev_connected=True, ev_soc_pct=50.0,
            pv_tomorrow_kwh=0.0, prices_by_hour=prices,
        )
        # 2300W / (230V * 1 phase) = 10A
        assert plan.ev_amps == 10

    def test_bat_charge_need_uses_pv_contribution_factor(self) -> None:
        """pv_bat_contribution_factor controls PV offset in battery charge need."""
        cfg_default = PlannerConfig()  # factor=0.5
        cfg_no_pv = PlannerConfig(pv_bat_contribution_factor=0.0)

        planner_default = Planner(cfg_default)
        planner_no_pv = Planner(cfg_no_pv)

        # With high PV forecast, default factor reduces charge need
        need_default = planner_default._calculate_bat_charge_need(
            bat_soc_pct=50.0, bat_cap_kwh=20.0, pv_tomorrow_kwh=20.0
        )
        need_no_pv = planner_no_pv._calculate_bat_charge_need(
            bat_soc_pct=50.0, bat_cap_kwh=20.0, pv_tomorrow_kwh=20.0
        )
        # With 0 PV contribution, need should be higher
        assert need_no_pv > need_default

    def test_evening_hourly_rate_uses_discharge_hours(self) -> None:
        """hourly_rate_w divides by evening_discharge_hours from config."""
        cfg_5h = PlannerConfig(house_baseload_kw=0.1, night_hours=1, evening_discharge_hours=5.0)
        cfg_2h = PlannerConfig(house_baseload_kw=0.1, night_hours=1, evening_discharge_hours=2.0)

        plan_5h = Planner(cfg_5h).generate_evening_plan(
            bat_soc_pct=90.0, bat_cap_kwh=20.0,
            ev_connected=False, ev_soc_pct=80.0,
        )
        plan_2h = Planner(cfg_2h).generate_evening_plan(
            bat_soc_pct=90.0, bat_cap_kwh=20.0,
            ev_connected=False, ev_soc_pct=80.0,
        )
        # Fewer hours → higher hourly rate for same surplus
        if plan_5h.bat_surplus_kwh > 0:
            assert plan_2h.hourly_rate_w > plan_5h.hourly_rate_w


class TestPlanHoursConfig:
    """PLAT-1551: plan_hours must be injectable from config."""

    def test_plan_hours_injected_from_config(self) -> None:
        custom = PlannerConfig(plan_hours=(13,))
        planner = Planner(custom)
        assert set(planner._config.plan_hours) == {13}
        assert 22 not in planner._config.plan_hours
