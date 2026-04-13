"""PLAT-1385: Edge case tests — energy boundaries and safety fallbacks.

Tests extreme conditions that occur in production:
- SoC at 0% and 100%
- Grid at Ellevio limit
- HA timeout / unavailable
- Battery temperature extremes
- Scenario transitions at boundary hours
- Guard cascade behavior
- EV controller edge cases
- Dual battery asymmetric conditions
"""

from __future__ import annotations

from core.balancer import BatteryBalancer, BatteryInfo
from core.ev_controller import EVAction, EVController, EVControllerConfig
from core.guards import GridGuard, GuardConfig, GuardLevel
from core.models import (
    CTPlacement,
    Scenario,
)
from core.state_machine import StateMachineConfig
from tests.conftest import (
    make_battery_state,
    make_snapshot,
)


def _eval_guard(
    guard: GridGuard,
    soc: float = 50.0,
    weighted_avg_kw: float = 1.0,
    hour: int = 14,
    scenario: Scenario = Scenario.MIDDAY_CHARGE,
    ha_connected: bool = True,
    data_age_s: float = 0.0,
    ems_mode: str = "battery_standby",
) -> object:
    bat = make_battery_state(soc_pct=soc, ems_mode=ems_mode)
    return guard.evaluate(
        batteries=[bat],
        current_scenario=scenario,
        weighted_avg_kw=weighted_avg_kw,
        hour=hour,
        ha_connected=ha_connected,
        data_age_s=data_age_s,
    )


# ===========================================================================
# SoC boundary tests
# ===========================================================================


class TestSoCBoundaries:
    def test_soc_at_zero_triggers_guard(self) -> None:
        guard = GridGuard(GuardConfig(normal_floor_pct=15.0))
        result = _eval_guard(guard, soc=0.0, hour=18,
                             scenario=Scenario.EVENING_DISCHARGE)
        assert result.level in (
            GuardLevel.WARNING, GuardLevel.BREACH, GuardLevel.FREEZE,
        )

    def test_soc_at_floor_no_discharge(self) -> None:
        balancer = BatteryBalancer()
        bat = BatteryInfo(
            battery_id="k", soc_pct=15.0, cap_kwh=15.0,
            cell_temp_c=20.0, soh_pct=100.0,
            max_discharge_w=5000.0, max_charge_w=5000.0,
            ct_placement=CTPlacement.LOCAL_LOAD,
        )
        result = balancer.allocate([bat], 3000.0)
        assert result.allocation_map["k"].watts == 0

    def test_soc_at_100_no_charge(self) -> None:
        balancer = BatteryBalancer()
        bat = BatteryInfo(
            battery_id="k", soc_pct=100.0, cap_kwh=15.0,
            cell_temp_c=20.0, soh_pct=100.0,
            max_discharge_w=5000.0, max_charge_w=5000.0,
            ct_placement=CTPlacement.LOCAL_LOAD,
        )
        result = balancer.allocate([bat], -3000.0, is_charging=True)
        assert result.total_allocated_w == 0.0


# ===========================================================================
# Grid boundary tests
# ===========================================================================


class TestGridBoundaries:
    def test_at_exact_target_no_breach(self) -> None:
        guard = GridGuard(GuardConfig(tak_kw=3.0))
        result = _eval_guard(guard, weighted_avg_kw=3.0)
        assert result.level != GuardLevel.CRITICAL

    def test_above_target_triggers_breach(self) -> None:
        guard = GridGuard(GuardConfig(tak_kw=3.0))
        result = _eval_guard(guard, weighted_avg_kw=3.5)
        assert result.level in (GuardLevel.BREACH, GuardLevel.CRITICAL)

    def test_negative_grid_export_ok(self) -> None:
        guard = GridGuard(GuardConfig(tak_kw=3.0))
        result = _eval_guard(guard, weighted_avg_kw=-2.0,
                             scenario=Scenario.PV_SURPLUS, hour=13)
        assert result.level == GuardLevel.OK


# ===========================================================================
# Temperature extremes
# ===========================================================================


class TestTemperatureExtremes:
    def test_cold_derating_below_4c(self) -> None:
        balancer = BatteryBalancer()
        bat = BatteryInfo(
            battery_id="k", soc_pct=80.0, cap_kwh=15.0,
            cell_temp_c=2.0, soh_pct=100.0,
            max_discharge_w=5000.0, max_charge_w=5000.0,
            ct_placement=CTPlacement.LOCAL_LOAD,
        )
        result = balancer.allocate([bat], 4000.0)
        assert result.allocation_map["k"].cold_derated is True
        assert result.allocation_map["k"].watts < 4000

    def test_frozen_blocked_below_0c(self) -> None:
        balancer = BatteryBalancer()
        bat = BatteryInfo(
            battery_id="k", soc_pct=80.0, cap_kwh=15.0,
            cell_temp_c=-2.0, soh_pct=100.0,
            max_discharge_w=5000.0, max_charge_w=5000.0,
            ct_placement=CTPlacement.LOCAL_LOAD,
        )
        result = balancer.allocate([bat], 4000.0)
        assert result.allocation_map["k"].watts == 0


# ===========================================================================
# HA communication failures
# ===========================================================================


class TestHACommunicationFailure:
    def test_guard_ha_disconnected_triggers_freeze(self) -> None:
        guard = GridGuard(GuardConfig(ha_health_timeout_s=0.0))
        # First call with connected to set baseline
        _eval_guard(guard, ha_connected=True)
        # Force timeout by setting last contact far in past
        guard._last_ha_contact = 0.0
        result = _eval_guard(guard, ha_connected=False)
        assert result.level in (GuardLevel.FREEZE, GuardLevel.ALARM)

    def test_guard_stale_data_triggers_warning(self) -> None:
        guard = GridGuard(GuardConfig(stale_threshold_s=300.0))
        result = _eval_guard(guard, data_age_s=600.0)
        assert result.level != GuardLevel.OK


# ===========================================================================
# Scenario boundary hours
# ===========================================================================


class TestScenarioBoundaryHours:
    def test_morning_window_6_to_9(self) -> None:
        cfg = StateMachineConfig()
        assert cfg.morning_start_h == 6
        assert cfg.morning_end_h == 9

    def test_night_at_22(self) -> None:
        snap = make_snapshot(hour=22, minute=0)
        assert snap.is_night

    def test_not_night_at_6(self) -> None:
        snap = make_snapshot(hour=6, minute=0)
        assert not snap.is_night

    def test_not_night_at_21(self) -> None:
        snap = make_snapshot(hour=21, minute=59)
        assert not snap.is_night


# ===========================================================================
# EV controller edge cases
# ===========================================================================


class TestEVEdgeCases:
    def test_soc_minus_1_fallback(self) -> None:
        ctrl = EVController(EVControllerConfig(
            step_interval_s=0,
            cooldown_after_start_s=0,
            cooldown_after_stop_s=0,
        ))
        result = ctrl.evaluate(
            ev_soc_pct=-1.0, charging=False, ev_connected=True,
            current_amps=0.0, grid_import_w=0.0,
            ellevio_headroom_w=3000.0,
        )
        assert result.action in (EVAction.START, EVAction.NO_CHANGE)

    def test_ev_not_connected_noop(self) -> None:
        ctrl = EVController(EVControllerConfig())
        result = ctrl.evaluate(
            ev_soc_pct=50.0, charging=False, ev_connected=False,
            current_amps=0.0, grid_import_w=0.0,
            ellevio_headroom_w=3000.0,
        )
        assert result.action == EVAction.NO_CHANGE

    def test_at_target_soc_stops(self) -> None:
        ctrl = EVController(EVControllerConfig(
            target_soc_pct=75.0,
            cooldown_after_start_s=0,
            cooldown_after_stop_s=0,
        ))
        result = ctrl.evaluate(
            ev_soc_pct=75.0, charging=True, ev_connected=True,
            current_amps=10.0, grid_import_w=0.0,
            ellevio_headroom_w=3000.0,
        )
        assert result.action == EVAction.STOP


# ===========================================================================
# Dual battery asymmetric
# ===========================================================================


class TestDualBatteryEdgeCases:
    def test_one_full_other_at_floor(self) -> None:
        balancer = BatteryBalancer()
        k = BatteryInfo(
            battery_id="kontor", soc_pct=100.0, cap_kwh=15.0,
            cell_temp_c=20.0, soh_pct=100.0,
            max_discharge_w=5000.0, max_charge_w=5000.0,
            ct_placement=CTPlacement.LOCAL_LOAD,
        )
        f = BatteryInfo(
            battery_id="forrad", soc_pct=15.0, cap_kwh=5.0,
            cell_temp_c=20.0, soh_pct=100.0,
            max_discharge_w=5000.0, max_charge_w=5000.0,
            ct_placement=CTPlacement.HOUSE_GRID,
        )
        result = balancer.allocate([k, f], 3000.0)
        assert result.allocation_map["kontor"].watts > 0
        assert result.allocation_map["forrad"].watts == 0

    def test_both_at_floor(self) -> None:
        balancer = BatteryBalancer()
        k = BatteryInfo(
            battery_id="kontor", soc_pct=15.0, cap_kwh=15.0,
            cell_temp_c=20.0, soh_pct=100.0,
            max_discharge_w=5000.0, max_charge_w=5000.0,
            ct_placement=CTPlacement.LOCAL_LOAD,
        )
        f = BatteryInfo(
            battery_id="forrad", soc_pct=15.0, cap_kwh=5.0,
            cell_temp_c=20.0, soh_pct=100.0,
            max_discharge_w=5000.0, max_charge_w=5000.0,
            ct_placement=CTPlacement.HOUSE_GRID,
        )
        result = balancer.allocate([k, f], 3000.0)
        assert result.allocation_map["kontor"].watts == 0
        assert result.allocation_map["forrad"].watts == 0
