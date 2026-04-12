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
# S3: MIDDAY_CHARGE
# ===========================================================================


class TestS3MiddayCharge:
    """S3 entry: 12-17."""

    def test_entry_at_noon(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.FORENOON_PV_EV
        snap = _snap(hour=12)
        result = sm.evaluate(snap)
        assert result == Scenario.MIDDAY_CHARGE

    def test_exit_at_17(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.MIDDAY_CHARGE
        snap = _snap(hour=17)
        result = sm.evaluate(snap)
        assert result is not None  # Should exit


# ===========================================================================
# S4: EVENING_DISCHARGE
# ===========================================================================


class TestS4EveningDischarge:
    """S4 entry: 17-22, SoC > floor + 10%."""

    def test_entry_conditions_met(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.MIDDAY_CHARGE
        snap = _snap(hour=17, bat_soc=60.0)
        result = sm.evaluate(snap)
        assert result == Scenario.EVENING_DISCHARGE

    def test_low_soc_blocks_entry(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.MIDDAY_CHARGE
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

    def test_night_at_midnight_stays_if_ev_charging(self, sm: StateMachine) -> None:
        """00:00 with EV charging should stay in S5 (S7 blocked by EV not done)."""
        sm.state.current = Scenario.NIGHT_HIGH_PV
        snap = _snap(hour=0, pv_tomorrow=20.0, ev_connected=True, ev_soc=40.0)
        result = sm.evaluate(snap)
        assert result is None  # Stay in S5 — EV still charging

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
        sm.state.current = Scenario.MIDDAY_CHARGE
        snap = _snap(
            hour=13, bat_soc=96.0,
            pv_total_w=3000.0, grid_power_w=-500.0,
        )
        result = sm.evaluate(snap)
        assert result == Scenario.PV_SURPLUS

    def test_bat_not_full_blocks(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.MIDDAY_CHARGE
        snap = _snap(
            hour=13, bat_soc=80.0,
            pv_total_w=3000.0, grid_power_w=-500.0,
        )
        result = sm.evaluate(snap)
        assert result != Scenario.PV_SURPLUS

    def test_no_export_blocks(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.MIDDAY_CHARGE
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
        sm.state.current = Scenario.MIDDAY_CHARGE
        sm.state.entry_time = datetime.now(tz=timezone.utc)  # Just entered

        snap = _snap(hour=17)  # Would trigger S4 exit
        result = sm.evaluate(snap)
        assert result is None  # Blocked by dwell

    def test_after_dwell_allows_transition(self) -> None:
        """After dwell time → transition allowed."""
        sm = StateMachine(StateMachineConfig(min_dwell_s=300.0))
        sm.state.current = Scenario.MIDDAY_CHARGE
        sm.state.entry_time = datetime.now(tz=timezone.utc) - timedelta(seconds=600)

        snap = _snap(hour=17, bat_soc=60.0)
        result = sm.evaluate(snap)
        assert result is not None


# ===========================================================================
# Manual override
# ===========================================================================


class TestManualOverride:
    """Manual override forces scenario."""

    def test_override_forces_transition(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.MIDDAY_CHARGE
        sm.set_manual_override(Scenario.EVENING_DISCHARGE)
        snap = _snap(hour=12)  # Would normally stay in S3
        result = sm.evaluate(snap)
        assert result == Scenario.EVENING_DISCHARGE

    def test_clear_override_returns_to_auto(self, sm: StateMachine) -> None:
        sm.set_manual_override(Scenario.EVENING_DISCHARGE)
        sm.set_manual_override(None)  # Clear
        sm.state.current = Scenario.MIDDAY_CHARGE
        snap = _snap(hour=12)
        result = sm.evaluate(snap)
        assert result is None  # Stay in S3 (auto)

    def test_override_same_scenario_no_transition(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.MIDDAY_CHARGE
        sm.set_manual_override(Scenario.MIDDAY_CHARGE)
        snap = _snap(hour=12)
        result = sm.evaluate(snap)
        assert result is None  # Already in target


# ===========================================================================
# Transition matrix
# ===========================================================================


class TestTransitionMatrix:
    """Only allowed transitions should happen."""

    def test_s3_cannot_go_to_s1(self, sm: StateMachine) -> None:
        """S3 MIDDAY_CHARGE cannot transition directly to S1 MORNING_DISCHARGE."""
        sm.state.current = Scenario.MIDDAY_CHARGE
        snap = _snap(hour=7, pv_today=20.0, bat_soc=60.0)
        result = sm.evaluate(snap)
        # S1 entry conditions match but transition not allowed from S3
        assert result != Scenario.MORNING_DISCHARGE

    def test_s4_cannot_go_to_s3(self, sm: StateMachine) -> None:
        """S4 EVENING_DISCHARGE cannot go back to S3 MIDDAY_CHARGE."""
        sm.state.current = Scenario.EVENING_DISCHARGE
        snap = _snap(hour=13)  # S3 conditions match
        result = sm.evaluate(snap)
        assert result != Scenario.MIDDAY_CHARGE


# ===========================================================================
# transition_to
# ===========================================================================


class TestTransitionTo:
    """Test the transition_to method."""

    def test_updates_state(self, sm: StateMachine) -> None:
        sm.state.current = Scenario.MIDDAY_CHARGE
        sm.transition_to(Scenario.EVENING_DISCHARGE)
        assert sm.state.current == Scenario.EVENING_DISCHARGE
        assert sm.state.previous == Scenario.MIDDAY_CHARGE


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

        At hour=3 with bat full, exit conditions are met but no valid target
        is found (S1 requires 6-9am) — state machine logs a warning and stays.
        """
        sm.state.current = Scenario.NIGHT_GRID_CHARGE
        # Battery at or above grid_charge_max_soc (90%) → triggers exit path
        snap = _snap(hour=3, bat_soc=95.0, pv_today=15.0)
        result = sm.evaluate(snap)
        # Exit conditions met but no allowed target passes entry → None (stays)
        assert result is None

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
