"""E2E 24h Simulation Tests.

Simulates full summer and winter days using synthetic profiles.
Verifies core objectives and scenario transitions.
"""

from __future__ import annotations

from datetime import datetime, timezone


from core.guards import GridGuard, GuardConfig
from core.models import Scenario
from core.state_machine import StateMachine, StateMachineConfig
from tests.conftest import make_battery_state, make_ev_state, make_grid_state, make_snapshot
from tests.e2e.profiles import (
    house_consumption_kw,
    nordpool_price_ore,
    summer_pv_kw,
    winter_pv_kw,
)


class TestSummerDay:
    """Simulate a full summer day with high PV."""

    def test_scenario_transitions_occur(self) -> None:
        """Summer day should have scenario transitions (not stuck in one)."""
        sm = StateMachine(StateMachineConfig(min_dwell_s=0))
        sm.state.current = Scenario.MIDDAY_CHARGE
        sm.state.entry_time = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)  # Far past

        transitions: list[Scenario] = [Scenario.MIDDAY_CHARGE]

        # Simulate daytime hours where transitions are most likely
        for hour in range(12, 24):
            pv = summer_pv_kw(hour)
            snap = make_snapshot(
                hour=hour,
                batteries=[make_battery_state(soc_pct=60.0, pv_power_w=pv * 1000)],
                ev=make_ev_state(connected=False),
                grid=make_grid_state(
                    pv_forecast_today_kwh=50.0,
                    pv_forecast_tomorrow_kwh=50.0,
                    pv_total_w=pv * 1000,
                    grid_power_w=house_consumption_kw(hour) * 1000 - pv * 1000,
                    price_ore=nordpool_price_ore(hour),
                ),
            )
            new = sm.evaluate(snap)
            if new is not None:
                sm.transition_to(new)
                transitions.append(new)

        # Should transition from S3→S4 at 17:00 and S4→S5/S6 at 22:00
        assert len(transitions) >= 2, f"Expected ≥2 transitions, got {transitions}"

    def test_no_grid_charging_in_summer(self) -> None:
        """G0 should never fire during a summer day (no unintentional grid charging)."""
        guard = GridGuard(GuardConfig())

        for hour in range(24):
            pv = summer_pv_kw(hour)
            bat = make_battery_state(
                soc_pct=60.0, ems_mode="charge_pv", ems_power_limit_w=0,
                pv_power_w=pv * 1000,
            )
            result = guard.evaluate(
                batteries=[bat], current_scenario=Scenario.MIDDAY_CHARGE,
                weighted_avg_kw=1.0, hour=hour, ha_connected=True,
            )
            g0 = [v for v in result.violations if "G0" in v]
            assert len(g0) == 0, f"G0 fired at hour {hour}"

    def test_soc_floor_always_respected(self) -> None:
        """G1 floor must be enforced at all times."""
        guard = GridGuard(GuardConfig())
        bat = make_battery_state(soc_pct=14.0)
        result = guard.evaluate(
            batteries=[bat], current_scenario=Scenario.EVENING_DISCHARGE,
            weighted_avg_kw=1.0, hour=19, ha_connected=True,
        )
        g1 = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1) >= 1, "G1 must fire when SoC < floor"


class TestWinterDay:
    """Simulate a full winter day with low PV."""

    def test_conservative_winter_day(self) -> None:
        """Winter day: S3→S4 transition at 17:00."""
        sm = StateMachine(StateMachineConfig(min_dwell_s=0))
        sm.state.current = Scenario.MIDDAY_CHARGE
        sm.state.entry_time = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)  # Far past

        transitions: list[Scenario] = [Scenario.MIDDAY_CHARGE]

        for hour in range(12, 23):
            snap = make_snapshot(
                hour=hour,
                batteries=[make_battery_state(soc_pct=40.0)],
                ev=make_ev_state(connected=False),
                grid=make_grid_state(
                    pv_forecast_today_kwh=5.0,
                    pv_forecast_tomorrow_kwh=5.0,
                    grid_power_w=house_consumption_kw(hour) * 1000,
                    price_ore=nordpool_price_ore(hour),
                ),
            )
            new = sm.evaluate(snap)
            if new is not None:
                sm.transition_to(new)
                transitions.append(new)

        # Should at least transition S3→S4 at 17:00
        assert len(transitions) >= 2

    def test_cold_derating_applied(self) -> None:
        """Cold winter day should raise SoC floor."""
        guard = GridGuard(GuardConfig())
        bat = make_battery_state(soc_pct=18.0, cell_temp_c=2.0)
        result = guard.evaluate(
            batteries=[bat], current_scenario=Scenario.EVENING_DISCHARGE,
            weighted_avg_kw=1.0, hour=19, ha_connected=True,
        )
        # 18% < cold floor 20% → G1 should fire
        g1 = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1) >= 1, "Cold derating not applied"


class TestProfiles:
    """Verify synthetic profile sanity."""

    def test_summer_pv_peaks_at_noon(self) -> None:
        peak_hour = max(range(24), key=summer_pv_kw)
        assert 11 <= peak_hour <= 14

    def test_winter_pv_much_lower(self) -> None:
        summer_total = sum(summer_pv_kw(h) for h in range(24))
        winter_total = sum(winter_pv_kw(h) for h in range(24))
        assert winter_total < summer_total * 0.3

    def test_consumption_reasonable(self) -> None:
        daily = sum(house_consumption_kw(h) for h in range(24))
        assert 40 < daily < 80  # 40-80 kWh/day

    def test_price_cheap_at_night(self) -> None:
        assert nordpool_price_ore(2) < nordpool_price_ore(18)
