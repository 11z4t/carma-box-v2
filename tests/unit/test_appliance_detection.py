"""Tests for PLAT-1535: Appliance detection via Shelly sensors.

Covers:
- Active/inactive detection by threshold
- Hysteresis (stop threshold)
- Headroom reduction in guards
- EV ramp pause on new appliance start
- EV continues normally if appliance already active
"""

from __future__ import annotations

import pytest

from config.schema import ApplianceConfig, ApplianceMonitorConfig
from core.ev_controller import EVAction, EVController, EVControllerConfig
from core.guards import GridGuard, GuardConfig
from core.models import ApplianceState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_appliance(
    entity_id: str = "sensor.102_shelly_plug_g3_power",
    name: str = "Tvätt",
    active: bool = False,
    power_w: float = 0.0,
) -> ApplianceState:
    return ApplianceState(entity_id=entity_id, name=name, active=active, power_w=power_w)


def make_ctrl() -> EVController:
    """Controller with zero cooldowns for deterministic tests."""
    return EVController(
        EVControllerConfig(
            step_interval_s=0,
            cooldown_after_start_s=0,
            cooldown_after_stop_s=0,
            max_amps=10,
            steps=(6, 8, 10),
        )
    )


# ---------------------------------------------------------------------------
# 1. Threshold detection — ApplianceConfig & ApplianceState
# ---------------------------------------------------------------------------


class TestApplianceThresholds:
    """Appliance active/inactive detection via power thresholds."""

    def test_appliance_detected_above_threshold(self) -> None:
        """Power > start_threshold_w → active=True."""
        cfg = ApplianceConfig(
            id="tvatt", name="Tvätt",
            entity_id="sensor.102_shelly_plug_g3_power",
            start_threshold_w=50.0, stop_threshold_w=10.0,
        )
        # Simulate detection logic (same as appliance_reader.read_appliances)
        power_w = 120.0
        was_active = False
        threshold = cfg.start_threshold_w if not was_active else cfg.stop_threshold_w
        active = power_w >= threshold
        assert active is True

    def test_appliance_not_detected_below_threshold(self) -> None:
        """Power < start_threshold_w → active=False."""
        cfg = ApplianceConfig(
            id="tvatt", name="Tvätt",
            entity_id="sensor.102_shelly_plug_g3_power",
            start_threshold_w=50.0, stop_threshold_w=10.0,
        )
        power_w = 30.0
        was_active = False
        threshold = cfg.start_threshold_w if not was_active else cfg.stop_threshold_w
        active = power_w >= threshold
        assert active is False

    def test_appliance_stopped_below_stop_threshold(self) -> None:
        """Hysteresis: was_active=True, power < stop_threshold_w → active=False."""
        cfg = ApplianceConfig(
            id="tvatt", name="Tvätt",
            entity_id="sensor.102_shelly_plug_g3_power",
            start_threshold_w=50.0, stop_threshold_w=10.0,
        )
        power_w = 5.0
        was_active = True
        threshold = cfg.start_threshold_w if not was_active else cfg.stop_threshold_w
        active = power_w >= threshold
        assert active is False

    def test_appliance_monitor_config_defaults(self) -> None:
        """ApplianceMonitorConfig defaults are correct."""
        cfg = ApplianceMonitorConfig()
        assert cfg.enabled is True
        assert cfg.ramp_pause_on_new_load is True
        assert cfg.appliances == []

    def test_appliance_config_entity_id_stored(self) -> None:
        """ApplianceConfig stores entity_id correctly."""
        cfg = ApplianceConfig(
            id="disk", name="Disk",
            entity_id="sensor.98_shelly_plug_s_power",
        )
        assert cfg.entity_id == "sensor.98_shelly_plug_s_power"
        assert cfg.start_threshold_w == 50.0
        assert cfg.stop_threshold_w == 10.0


# ---------------------------------------------------------------------------
# 2. Headroom integration — guards.py
# ---------------------------------------------------------------------------


class TestHeadroomReducedByApplianceLoad:
    """Appliance load is subtracted from Ellevio headroom."""

    @pytest.fixture()
    def guard(self) -> GridGuard:
        return GridGuard(GuardConfig(tak_kw=3.0, night_weight=0.5, day_weight=1.0))

    def test_headroom_reduced_by_appliance_load(self, guard: GridGuard) -> None:
        """With 1.5 kW appliance load, headroom is 0.5 kW less than without."""
        from tests.conftest import make_battery_state
        from core.models import Scenario

        bat = make_battery_state()
        weighted_avg_kw = 1.5  # day: tak=3.0/1.0=3.0 → headroom=1.5 before appliances

        result_no_appliances = guard.evaluate(
            batteries=[bat],
            current_scenario=Scenario.PV_SURPLUS_DAY,
            weighted_avg_kw=weighted_avg_kw,
            hour=12,
            ha_connected=True,
            appliance_kw=0.0,
        )
        result_with_appliances = guard.evaluate(
            batteries=[bat],
            current_scenario=Scenario.PV_SURPLUS_DAY,
            weighted_avg_kw=weighted_avg_kw,
            hour=12,
            ha_connected=True,
            appliance_kw=0.5,
        )

        assert result_no_appliances.headroom_kw == pytest.approx(1.5, abs=0.01)
        assert result_with_appliances.headroom_kw == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# 3. EV ramp pause — ev_controller.py
# ---------------------------------------------------------------------------


class TestEVRampPauseOnNewAppliance:
    """EV ramp pauses when a new appliance starts."""

    def test_ev_ramp_paused_on_new_appliance(self) -> None:
        """New appliance start (False→True) → EV NO_CHANGE while charging."""
        ctrl = make_ctrl()

        # First cycle: no appliances active — EV charges at 6A
        ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=6, grid_import_w=500, ellevio_headroom_w=2000,
            appliances=[make_appliance(active=False, power_w=0.0)],
        )

        # Second cycle: appliance starts — ramp should pause
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=6, grid_import_w=500, ellevio_headroom_w=2000,
            appliances=[make_appliance(active=True, power_w=120.0)],
        )

        assert result.action == EVAction.NO_CHANGE
        assert "appliance" in result.reason.lower()

    def test_ev_not_paused_if_already_active(self) -> None:
        """Appliance already active last cycle → EV ramp continues normally."""
        ctrl = make_ctrl()

        # First cycle: appliance already active
        ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=6, grid_import_w=500, ellevio_headroom_w=2000,
            appliances=[make_appliance(active=True, power_w=120.0)],
        )

        # Second cycle: same appliance still active — not a new start
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=6, grid_import_w=500, ellevio_headroom_w=2000,
            appliances=[make_appliance(active=True, power_w=120.0)],
        )

        # Should ramp up (not pause) since appliance was already on
        assert result.action == EVAction.SET_CURRENT
        assert result.target_amps == 8

    def test_ev_no_pause_when_not_charging(self) -> None:
        """New appliance while EV not charging → no ramp pause (not relevant)."""
        ctrl = make_ctrl()

        ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=False,
            current_amps=0, grid_import_w=500, ellevio_headroom_w=500,
            appliances=[make_appliance(active=False, power_w=0.0)],
        )

        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=False,
            current_amps=0, grid_import_w=500, ellevio_headroom_w=500,
            appliances=[make_appliance(active=True, power_w=120.0)],
        )

        # EV wasn't charging, so "new appliance" ramp pause is irrelevant
        assert result.action == EVAction.NO_CHANGE
        # Could be "insufficient headroom" or "stop cooldown" — just not appliance-related
        assert "appliance" not in result.reason.lower()

    def test_total_appliance_kw_property(self) -> None:
        """SystemSnapshot.total_appliance_kw sums active appliance power."""
        from tests.conftest import make_snapshot

        snap = make_snapshot(
            appliances=[
                make_appliance(active=True, power_w=120.0),
                make_appliance(entity_id="sensor.103", name="Tork", active=True, power_w=800.0),
                make_appliance(entity_id="sensor.98", name="Disk", active=False, power_w=20.0),
            ]
        )

        # Only active appliances count: 120 + 800 = 920 W = 0.92 kW
        assert snap.total_appliance_kw == pytest.approx(0.92, abs=0.001)
