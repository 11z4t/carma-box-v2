"""Tests for State Machine (8 Scenarios).

Covers:
- Each scenario entry condition (true/false inputs)
- Each scenario exit condition
- Priority: S1 wins over S2 when both match
- Dwell time blocks early transition
- Manual override forces scenario
- Transition matrix enforcement
- Night boundary (22:00, 06:00) correct for S5/S6/S7
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.models import Scenario, SystemSnapshot
from core.state_machine import StateMachine, StateMachineConfig
from tests.conftest import make_ev_state, make_grid_state, make_snapshot

# Named test constants
_MIDNIGHT_HOUR: int = 0
_S3_MIDDAY_SANITY_HOUR: int = 14


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sm() -> StateMachine:
    return StateMachine(StateMachineConfig(min_dwell_s=0))  # No dwell in tests


def _snap(
    hour: int = 12,
    pv_today: float = 20.0,
    pv_tomorrow: float = 20.0,
    bat_soc: float = 60.0,
    ev_connected: bool = False,
    ev_soc: float = 50.0,
    grid_power_w: float = 500.0,
    pv_total_w: float = 0.0,
    price_ore: float = 50.0,
) -> SystemSnapshot:
    """Create a snapshot with convenient defaults."""
    from tests.conftest import make_battery_state

    return make_snapshot(
        hour=hour,
        batteries=[
            make_battery_state(soc_pct=bat_soc, cap_kwh=15.0),
        ],
        ev=make_ev_state(connected=ev_connected, soc_pct=ev_soc),
        grid=make_grid_state(
            pv_forecast_today_kwh=pv_today,
            pv_forecast_tomorrow_kwh=pv_tomorrow,
            grid_power_w=grid_power_w,
            pv_total_w=pv_total_w,
            price_ore=price_ore,
        ),
    )


# ===========================================================================
# S1: MORNING_DISCHARGE
# ===========================================================================


class TestS1MorningDischarge:
    """S1 entry: 06-09, PV forecast > 10 kWh, SoC > 30%."""

    def test_entry_conditions_met(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.NIGHT_HIGH_PV
        snap = _snap(hour=7, pv_today=15.0, bat_soc=60.0)
        result = sm.evaluate(snap)
        assert result == Scenario.MORNING_DISCHARGE

    def test_too_early(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.NIGHT_HIGH_PV
        snap = _snap(hour=5, pv_today=15.0, bat_soc=60.0)
        result = sm.evaluate(snap)
        assert result != Scenario.MORNING_DISCHARGE

    def test_exits_at_9_with_ev(self, sm: StateMachine) -> None:
        """At hour 9, S1 exits. With EV connected → S2."""
        sm.state.current = Scenario.MORNING_DISCHARGE
        snap = _snap(hour=9, pv_today=20.0, bat_soc=60.0, ev_connected=True, ev_soc=40.0)
        result = sm.evaluate(snap)
        assert result == Scenario.FORENOON_PV_EV

    def test_exits_at_9_no_target(self, sm: StateMachine) -> None:
        """At hour 9 without EV, no valid transition from S1 at this hour."""
        sm.state.current = Scenario.MORNING_DISCHARGE
        snap = _snap(hour=9, pv_today=15.0, bat_soc=60.0)
        result = sm.evaluate(snap)
        # S1 exits but S2 needs EV, S3 needs hour>=12 — no valid target
        assert result is None

    def test_low_pv_forecast(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.NIGHT_HIGH_PV
        snap = _snap(hour=7, pv_today=5.0, bat_soc=60.0)
        result = sm.evaluate(snap)
        assert result != Scenario.MORNING_DISCHARGE

    def test_low_soc(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.NIGHT_HIGH_PV
        snap = _snap(hour=7, pv_today=15.0, bat_soc=20.0)
        result = sm.evaluate(snap)
        assert result != Scenario.MORNING_DISCHARGE


# ===========================================================================
# S2: FORENOON_PV_EV
# ===========================================================================


class TestS2ForenoonPvEv:
    """S2 entry: 06-12, high PV, EV connected + below target."""

    def test_entry_conditions_met(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.MORNING_DISCHARGE
        snap = _snap(hour=9, pv_today=20.0, ev_connected=True, ev_soc=40.0)
        result = sm.evaluate(snap)
        assert result == Scenario.FORENOON_PV_EV

    def test_ev_not_connected(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.MORNING_DISCHARGE
        snap = _snap(hour=9, pv_today=20.0, ev_connected=False)
        result = sm.evaluate(snap)
        assert result != Scenario.FORENOON_PV_EV

    def test_ev_at_target(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.MORNING_DISCHARGE
        snap = _snap(hour=9, pv_today=20.0, ev_connected=True, ev_soc=80.0)
        result = sm.evaluate(snap)
        assert result != Scenario.FORENOON_PV_EV


# ===========================================================================
# S3: PV_SURPLUS_DAY
# ===========================================================================


class TestS3MiddayCharge:
    """S3 entry: 12-17."""

    def test_entry_at_noon(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.FORENOON_PV_EV
        snap = _snap(hour=12)
        result = sm.evaluate(snap)
        assert result == Scenario.PV_SURPLUS_DAY

    def test_exit_at_17(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.PV_SURPLUS_DAY
        snap = _snap(hour=17)
        result = sm.evaluate(snap)
        assert result is not None  # Should exit


# ===========================================================================
# S4: EVENING_DISCHARGE
# ===========================================================================


class TestS4EveningDischarge:
    """S4 entry: 17-22, SoC > floor + 10%."""

    def test_entry_conditions_met(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.PV_SURPLUS_DAY
        snap = _snap(hour=17, bat_soc=60.0)
        result = sm.evaluate(snap)
        assert result == Scenario.EVENING_DISCHARGE

    def test_low_soc_blocks_entry(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.PV_SURPLUS_DAY
        snap = _snap(hour=17, bat_soc=20.0)  # Below floor+10%
        result = sm.evaluate(snap)
        assert result != Scenario.EVENING_DISCHARGE

    def test_exit_at_22(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.EVENING_DISCHARGE
        snap = _snap(hour=22)
        result = sm.evaluate(snap)
        assert result is not None


# ===========================================================================
# S5/S6: NIGHT_HIGH_PV / NIGHT_LOW_PV
# ===========================================================================


class TestNightScenarios:
    """S5/S6 depend on tomorrow's PV forecast."""

    def test_s5_high_pv_tomorrow(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.EVENING_DISCHARGE
        snap = _snap(hour=22, pv_tomorrow=20.0)
        result = sm.evaluate(snap)
        assert result == Scenario.NIGHT_HIGH_PV

    def test_s6_low_pv_tomorrow(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.EVENING_DISCHARGE
        snap = _snap(hour=22, pv_tomorrow=5.0)
        result = sm.evaluate(snap)
        assert result == Scenario.NIGHT_LOW_PV

    def test_night_at_midnight_with_ev_charging_transitions_to_NIGHT_EV(
        self, sm: StateMachine,
    ) -> None:
        """PLAT-1674: 00:00 + EV plugged + below target → NIGHT_EV takes over.

        Previously expected: stay in NIGHT_HIGH_PV.
        Now expected: opportunistic transition to NIGHT_EV (S9 highest priority).
        """
        sm.state.current = Scenario.NIGHT_HIGH_PV
        snap = _snap(hour=_MIDNIGHT_HOUR, pv_tomorrow=20.0, ev_connected=True, ev_soc=40.0)
        result = sm.evaluate(snap)
        assert result == Scenario.NIGHT_EV

    def test_night_exits_at_06(self, sm: StateMachine) -> None:
        """06:00 exits night scenarios."""
        sm.state.current = Scenario.NIGHT_HIGH_PV
        snap = _snap(hour=6, pv_today=15.0, bat_soc=60.0)
        result = sm.evaluate(snap)
        assert result is not None  # Should exit to S1


# ===========================================================================
# S7: NIGHT_GRID_CHARGE
# ===========================================================================


class TestS7NightGridCharge:
    """S7: EV done, bat needs charge, price OK."""

    def test_entry_conditions_met(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.NIGHT_HIGH_PV
        snap = _snap(
            hour=3, ev_connected=True, ev_soc=80.0,
            bat_soc=40.0, price_ore=30.0,
        )
        result = sm.evaluate(snap)
        assert result == Scenario.NIGHT_GRID_CHARGE

    def test_high_price_blocks(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.NIGHT_HIGH_PV
        snap = _snap(
            hour=3, ev_connected=True, ev_soc=80.0,
            bat_soc=40.0, price_ore=100.0,
        )
        result = sm.evaluate(snap)
        assert result != Scenario.NIGHT_GRID_CHARGE

    def test_bat_already_full_blocks(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.NIGHT_HIGH_PV
        snap = _snap(
            hour=3, ev_connected=True, ev_soc=80.0,
            bat_soc=95.0, price_ore=30.0,
        )
        result = sm.evaluate(snap)
        assert result != Scenario.NIGHT_GRID_CHARGE


# ===========================================================================
# S8: PV_SURPLUS
# ===========================================================================


class TestS8PvSurplus:
    """S8: bat full, PV producing, exporting."""

    def test_entry_conditions_met(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.PV_SURPLUS_DAY
        snap = _snap(
            hour=13, bat_soc=96.0,
            pv_total_w=3000.0, grid_power_w=-500.0,
        )
        result = sm.evaluate(snap)
        assert result == Scenario.PV_SURPLUS

    def test_bat_not_full_blocks(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.PV_SURPLUS_DAY
        snap = _snap(
            hour=13, bat_soc=80.0,
            pv_total_w=3000.0, grid_power_w=-500.0,
        )
        result = sm.evaluate(snap)
        assert result != Scenario.PV_SURPLUS

    def test_no_export_blocks(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.PV_SURPLUS_DAY
        snap = _snap(
            hour=13, bat_soc=96.0,
            pv_total_w=3000.0, grid_power_w=100.0,  # Importing
        )
        result = sm.evaluate(snap)
        assert result != Scenario.PV_SURPLUS


# ===========================================================================
# Priority
# ===========================================================================


class TestPriority:
    """S1 wins over S2 when both match."""

    def test_s1_wins_over_s2(self, sm: StateMachine) -> None:
        """Both S1 and S2 match at 07:00 with EV connected — S1 has priority."""
        sm.state.current = Scenario.NIGHT_HIGH_PV
        snap = _snap(
            hour=7, pv_today=20.0, bat_soc=60.0,
            ev_connected=True, ev_soc=40.0,
        )
        result = sm.evaluate(snap)
        assert result == Scenario.MORNING_DISCHARGE  # S1 wins


# ===========================================================================
# Dwell time
# ===========================================================================


class TestDwellTime:
    """Minimum dwell time blocks early transition."""

    def test_dwell_blocks_transition(self) -> None:
        """Within dwell time → no transition allowed."""
        sm = StateMachine(StateMachineConfig(min_dwell_s=300.0))
        sm.state.current = Scenario.PV_SURPLUS_DAY
        sm.state.entry_time = datetime.now(tz=timezone.utc)  # Just entered

        snap = _snap(hour=17)  # Would trigger S4 exit
        result = sm.evaluate(snap)
        assert result is None  # Blocked by dwell

    def test_after_dwell_allows_transition(self) -> None:
        """After dwell time → transition allowed (H7: uses monotonic clock)."""
        import time

        sm = StateMachine(StateMachineConfig(min_dwell_s=300.0))
        sm.state.current = Scenario.PV_SURPLUS_DAY
        sm.state.entry_time = datetime.now(tz=timezone.utc) - timedelta(seconds=600)
        # Back-date monotonic stamp to simulate 600 s of dwell (H7)
        sm.state._entry_monotonic = time.monotonic() - 600.0

        snap = _snap(hour=17, bat_soc=60.0)
        result = sm.evaluate(snap)
        assert result is not None


# ===========================================================================
# Manual override
# ===========================================================================


class TestManualOverride:
    """Manual override forces scenario."""

    def test_override_forces_transition(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.PV_SURPLUS_DAY
        sm.set_manual_override(Scenario.EVENING_DISCHARGE)
        snap = _snap(hour=12)  # Would normally stay in S3
        result = sm.evaluate(snap)
        assert result == Scenario.EVENING_DISCHARGE

    def test_clear_override_returns_to_auto(self, sm: StateMachine) -> None:
        sm.set_manual_override(Scenario.EVENING_DISCHARGE)
        sm.set_manual_override(None)  # Clear
        sm.state.current = Scenario.PV_SURPLUS_DAY
        snap = _snap(hour=12)
        result = sm.evaluate(snap)
        assert result is None  # Stay in S3 (auto)

    def test_override_same_scenario_no_transition(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.PV_SURPLUS_DAY
        sm.set_manual_override(Scenario.PV_SURPLUS_DAY)
        snap = _snap(hour=12)
        result = sm.evaluate(snap)
        assert result is None  # Already in target


# ===========================================================================
# Transition matrix
# ===========================================================================


class TestTransitionMatrix:
    """Only allowed transitions should happen."""

    def test_s3_at_morning_recovers_to_s1(self, sm: StateMachine) -> None:
        """S3 PV_SURPLUS_DAY at hour=7 → catchall recovery to S1."""
        sm.state.current = Scenario.PV_SURPLUS_DAY
        snap = _snap(hour=7, pv_today=20.0, bat_soc=60.0)
        result = sm.evaluate(snap)
        # S3 at hour 7 is outside midday window → exit → catchall → S1
        assert result == Scenario.MORNING_DISCHARGE

    def test_s4_at_midday_recovers_to_s3(self, sm: StateMachine) -> None:
        """S4 EVENING_DISCHARGE at hour=13 → catchall recovery to S3."""
        sm.state.current = Scenario.EVENING_DISCHARGE
        snap = _snap(hour=13)
        result = sm.evaluate(snap)
        # S4 at hour 13 is outside evening window → exit → catchall → S3
        assert result == Scenario.PV_SURPLUS_DAY


# ===========================================================================
# REGRESSION: Midnight wrap exit — S3/S4 must exit at midnight
# ===========================================================================


class TestMidnightWrapExit:
    """Scenario exit conditions must handle midnight correctly."""

    def test_s3_exits_at_midnight(self, sm: StateMachine) -> None:
        """S3 PV_SURPLUS_DAY at hour=0 must exit (not stay stuck)."""
        sm.state.current = Scenario.PV_SURPLUS_DAY
        snap = _snap(hour=_MIDNIGHT_HOUR, pv_tomorrow=20.0)
        result = sm.evaluate(snap)
        assert result is not None, "S3 stuck at midnight — exit not triggered"

    def test_s4_exits_at_midnight(self, sm: StateMachine) -> None:
        """S4 EVENING_DISCHARGE at hour=0 must exit."""
        sm.state.current = Scenario.EVENING_DISCHARGE
        snap = _snap(hour=_MIDNIGHT_HOUR, pv_tomorrow=20.0, bat_soc=60.0)
        result = sm.evaluate(snap)
        assert result is not None, "S4 stuck at midnight — exit not triggered"

    def test_s3_stays_during_midday(self, sm: StateMachine) -> None:
        """S3 at hour=14 (within window) should NOT exit."""
        sm.state.current = Scenario.PV_SURPLUS_DAY
        snap = _snap(hour=_S3_MIDDAY_SANITY_HOUR)
        result = sm.evaluate(snap)
        assert result is None  # Stay in S3


# ===========================================================================
# transition_to
# ===========================================================================


class TestTransitionTo:
    """Test the transition_to method."""

    def test_updates_state(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.PV_SURPLUS_DAY
        sm.transition_to(Scenario.EVENING_DISCHARGE)
        assert sm.state.current == Scenario.EVENING_DISCHARGE
        assert sm.state.previous == Scenario.PV_SURPLUS_DAY


# ===========================================================================
# Coverage: exit conditions for S6, S7, S8
# ===========================================================================


class TestExitConditions:
    """Tests for exit condition branches in _exit_s6, _exit_s7, _exit_s8."""

    def test_exit_s6_at_6am(self, sm: StateMachine) -> None:
        """S6 exits when hour >= 6 (line 341 covered by _exit_s6)."""
        sm.state.current = Scenario.NIGHT_LOW_PV
        snap = _snap(hour=6, pv_today=15.0, bat_soc=60.0)
        result = sm.evaluate(snap)
        # Should exit S6 and transition to S1 (6am, high PV, SoC ok)
        assert result == Scenario.MORNING_DISCHARGE

    def test_exit_s7_at_6am(self, sm: StateMachine) -> None:
        """S7 exits when hour >= 6 (lines 345-346 covered by _exit_s7)."""
        sm.state.current = Scenario.NIGHT_GRID_CHARGE
        snap = _snap(hour=6, pv_today=15.0, bat_soc=60.0)
        result = sm.evaluate(snap)
        assert result == Scenario.MORNING_DISCHARGE

    def test_exit_s7_bat_full(self, sm: StateMachine) -> None:
        """S7 also exits when battery is full (line 348 covered).

        M1 fix: when normal matrix exit finds no valid target, a catchall
        recovery fires and selects the best scenario for current conditions.
        At hour=3, pv_today=15 → NIGHT_HIGH_PV entry conditions match.
        """
        sm.state.current = Scenario.NIGHT_GRID_CHARGE
        # Battery at or above grid_charge_max_soc (90%) → triggers exit path
        snap = _snap(hour=3, bat_soc=95.0, pv_today=15.0)
        result = sm.evaluate(snap)
        # M1 catchall: NIGHT_HIGH_PV is valid at hour=3 with high PV forecast
        assert result == Scenario.NIGHT_HIGH_PV

    def test_exit_s8_bat_drops(self, sm: StateMachine) -> None:
        """S8 exits when bat SoC drops below surplus_exit threshold (lines 353-354)."""
        sm.state.current = Scenario.PV_SURPLUS
        # SoC below exit threshold (90%)
        snap = _snap(hour=13, bat_soc=85.0, pv_total_w=3000.0, grid_power_w=-300.0)
        result = sm.evaluate(snap)
        # Should exit S8 (SoC < 90 exit threshold)
        assert result is not None

    def test_exit_s8_pv_too_low(self, sm: StateMachine) -> None:
        """S8 exits when PV production drops too low."""
        sm.state.current = Scenario.PV_SURPLUS
        snap = _snap(hour=13, bat_soc=96.0, pv_total_w=100.0, grid_power_w=-300.0)
        result = sm.evaluate(snap)
        # Should exit S8 (PV < 200W)
        assert result is not None
