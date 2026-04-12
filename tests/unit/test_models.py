"""Tests for core domain models.

Covers:
- BatteryState, EVState, GridState creation and immutability
- SystemSnapshot computed properties
- CycleDecision has_commands logic
- Enum values
- Edge cases
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.models import (
    Command,
    CommandType,
    CycleDecision,
    EMSMode,
    GuardResult,
    GuardStatus,
    Scenario,
)
from tests.conftest import (
    make_battery_state,
    make_ev_state,
    make_grid_state,
    make_snapshot,
)


class TestEMSMode:
    """Test EMSMode enum."""

    def test_all_modes_exist(self) -> None:
        """All expected EMS modes should be defined."""
        expected = {
            "charge_pv",
            "discharge_pv",
            "battery_standby",
            "import_ac",
            "export_ac",
            "conserve",
            "auto",
        }
        actual = {m.value for m in EMSMode}
        assert actual == expected

    def test_auto_is_forbidden(self) -> None:
        """Auto mode should exist in the enum (for guard detection) but is FORBIDDEN."""
        assert EMSMode.AUTO.value == "auto"


class TestScenario:
    """Test Scenario enum."""

    def test_all_scenarios(self) -> None:
        """All 8 scenarios should be defined."""
        assert len(Scenario) == 8

    def test_scenario_values(self) -> None:
        """Scenario values should match the state machine definition."""
        assert Scenario.MORNING_DISCHARGE.value == "MORNING_DISCHARGE"
        assert Scenario.NIGHT_GRID_CHARGE.value == "NIGHT_GRID_CHARGE"


class TestBatteryState:
    """Test BatteryState dataclass."""

    def test_create_battery_state(self) -> None:
        """BatteryState should be creatable with all fields."""
        bat = make_battery_state()
        assert bat.battery_id == "kontor"
        assert bat.soc_pct == 60.0

    def test_immutable(self) -> None:
        """BatteryState should be frozen (immutable)."""
        bat = make_battery_state()
        with pytest.raises(AttributeError):
            bat.soc_pct = 80.0  # type: ignore[misc]

    def test_ct_placement_values(self) -> None:
        """CT placement should accept both valid values."""
        bat_local = make_battery_state(ct_placement="local_load")
        bat_grid = make_battery_state(ct_placement="house_grid")
        assert bat_local.ct_placement == "local_load"
        assert bat_grid.ct_placement == "house_grid"

    def test_positive_power_is_discharge(self) -> None:
        """Positive power_w means discharge."""
        bat = make_battery_state(power_w=2000.0)
        assert bat.power_w > 0  # discharge

    def test_negative_power_is_charge(self) -> None:
        """Negative power_w means charge."""
        bat = make_battery_state(power_w=-3000.0)
        assert bat.power_w < 0  # charge


class TestEVState:
    """Test EVState dataclass."""

    def test_create_ev_state(self) -> None:
        """EVState should be creatable with defaults."""
        ev = make_ev_state()
        assert ev.soc_pct == 50.0
        assert not ev.connected

    def test_immutable(self) -> None:
        """EVState should be frozen."""
        ev = make_ev_state()
        with pytest.raises(AttributeError):
            ev.soc_pct = 90.0  # type: ignore[misc]

    def test_connected_and_charging(self) -> None:
        """EV connected and charging should both be settable."""
        ev = make_ev_state(connected=True, charging=True, power_w=4140.0)
        assert ev.connected
        assert ev.charging
        assert ev.power_w == 4140.0


class TestGridState:
    """Test GridState dataclass."""

    def test_create_grid_state(self) -> None:
        """GridState should be creatable with defaults."""
        grid = make_grid_state()
        assert grid.grid_power_w == 500.0

    def test_positive_is_import(self) -> None:
        """Positive grid_power_w means import."""
        grid = make_grid_state(grid_power_w=1500.0)
        assert grid.grid_power_w > 0

    def test_negative_is_export(self) -> None:
        """Negative grid_power_w means export."""
        grid = make_grid_state(grid_power_w=-300.0)
        assert grid.grid_power_w < 0


class TestSystemSnapshot:
    """Test SystemSnapshot and its computed properties."""

    def test_create_snapshot(self) -> None:
        """SystemSnapshot should be creatable."""
        snap = make_snapshot()
        assert snap.hour == 12
        assert snap.current_scenario == Scenario.MIDDAY_CHARGE

    def test_total_battery_soc_single(self) -> None:
        """With one battery, total SoC equals that battery's SoC."""
        snap = make_snapshot(
            batteries=[make_battery_state(soc_pct=70.0, cap_kwh=10.0)]
        )
        assert snap.total_battery_soc_pct == pytest.approx(70.0)

    def test_total_battery_soc_weighted(self) -> None:
        """With two batteries of different capacity, SoC is weighted by capacity."""
        kontor = make_battery_state(
            battery_id="kontor", soc_pct=80.0, cap_kwh=15.0
        )
        forrad = make_battery_state(
            battery_id="forrad", soc_pct=40.0, cap_kwh=5.0
        )
        snap = make_snapshot(batteries=[kontor, forrad])
        # (80*15 + 40*5) / (15+5) = (1200+200)/20 = 70.0
        assert snap.total_battery_soc_pct == pytest.approx(70.0)

    def test_total_battery_soc_empty(self) -> None:
        """With no batteries, total SoC should be 0."""
        snap = make_snapshot(batteries=[])
        assert snap.total_battery_soc_pct == 0.0

    def test_total_available_kwh(self) -> None:
        """Total available kWh should sum across batteries."""
        b1 = make_battery_state(battery_id="a", available_kwh=5.0)
        b2 = make_battery_state(battery_id="b", available_kwh=2.0)
        snap = make_snapshot(batteries=[b1, b2])
        assert snap.total_available_kwh == pytest.approx(7.0)

    def test_is_night_at_22(self) -> None:
        """22:00 should be night."""
        snap = make_snapshot(hour=22)
        assert snap.is_night

    def test_is_night_at_03(self) -> None:
        """03:00 should be night."""
        snap = make_snapshot(hour=3)
        assert snap.is_night

    def test_is_not_night_at_06(self) -> None:
        """06:00 should not be night."""
        snap = make_snapshot(hour=6)
        assert not snap.is_night

    def test_is_not_night_at_12(self) -> None:
        """12:00 should not be night."""
        snap = make_snapshot(hour=12)
        assert not snap.is_night

    def test_is_not_night_at_21(self) -> None:
        """21:00 should not be night."""
        snap = make_snapshot(hour=21)
        assert not snap.is_night

    def test_is_night_at_00(self) -> None:
        """00:00 should be night."""
        snap = make_snapshot(hour=0)
        assert snap.is_night


class TestCycleDecision:
    """Test CycleDecision dataclass."""

    def test_no_commands(self) -> None:
        """Decision with no commands should report has_commands=False."""
        decision = CycleDecision(
            timestamp=datetime.now(tz=timezone.utc),
            scenario=Scenario.MIDDAY_CHARGE,
        )
        assert not decision.has_commands

    def test_noop_only(self) -> None:
        """Decision with only NO_OP commands should report has_commands=False."""
        decision = CycleDecision(
            timestamp=datetime.now(tz=timezone.utc),
            scenario=Scenario.MIDDAY_CHARGE,
            commands=[
                Command(
                    command_type=CommandType.NO_OP,
                    target_id="",
                    reason="nothing to do",
                )
            ],
        )
        assert not decision.has_commands

    def test_has_real_commands(self) -> None:
        """Decision with actionable commands should report has_commands=True."""
        decision = CycleDecision(
            timestamp=datetime.now(tz=timezone.utc),
            scenario=Scenario.EVENING_DISCHARGE,
            commands=[
                Command(
                    command_type=CommandType.SET_EMS_MODE,
                    target_id="kontor",
                    value="discharge_pv",
                    rule_id="S4-entry",
                    reason="Evening discharge start",
                )
            ],
        )
        assert decision.has_commands

    def test_immutable(self) -> None:
        """CycleDecision should be frozen."""
        decision = CycleDecision(
            timestamp=datetime.now(tz=timezone.utc),
            scenario=Scenario.MIDDAY_CHARGE,
        )
        with pytest.raises(AttributeError):
            decision.scenario = Scenario.PV_SURPLUS  # type: ignore[misc]


class TestGuardResult:
    """Test GuardResult dataclass."""

    def test_ok_status(self) -> None:
        """OK status with no violations."""
        result = GuardResult(status=GuardStatus.OK, headroom_kw=1.5)
        assert result.status == GuardStatus.OK
        assert len(result.invariant_violations) == 0

    def test_breach_with_commands(self) -> None:
        """BREACH status should carry commands and violation descriptions."""
        result = GuardResult(
            status=GuardStatus.BREACH,
            commands=[
                Command(
                    command_type=CommandType.SET_EMS_MODE,
                    target_id="kontor",
                    value="discharge_pv",
                    rule_id="G3",
                    reason="Ellevio breach",
                )
            ],
            headroom_kw=-0.5,
            invariant_violations=["G3: weighted_avg 2.1 kW > tak 2.0 kW"],
            replan_needed=True,
        )
        assert result.status == GuardStatus.BREACH
        assert result.replan_needed
        assert len(result.commands) == 1
        assert result.headroom_kw < 0


class TestCommandType:
    """Test CommandType enum."""

    def test_all_command_types(self) -> None:
        """All expected command types should exist."""
        expected = {
            "set_ems_mode",
            "set_ems_power_limit",
            "set_fast_charging",
            "set_ev_current",
            "start_ev_charging",
            "stop_ev_charging",
            "turn_on_consumer",
            "turn_off_consumer",
            "no_op",
        }
        actual = {ct.value for ct in CommandType}
        assert actual == expected
