"""Integration tests for PLAT-1536: Appliance schema in site.yaml + pipeline.

Tests the full chain from YAML config loading through ApplianceState
to headroom reduction and EV ramp pause — no mocks of schema or models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from config.schema import ApplianceMonitorConfig, load_config
from core.ev_controller import EVAction, EVController, EVControllerConfig
from core.guards import GridGuard, GuardConfig
from core.models import (
    DEFAULT_APPLIANCE_START_W,
    DEFAULT_APPLIANCE_STOP_W,
    ApplianceState,
    Scenario,
    SystemSnapshot,
)
from tests.conftest import make_battery_state, make_ev_state, make_grid_state

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SITE_WITH_APPLIANCES = FIXTURES_DIR / "site_with_appliances.yaml"


# ---------------------------------------------------------------------------
# 1. YAML loading — real CarmaConfig, no mocks
# ---------------------------------------------------------------------------


class TestSiteYamlLoadsApplianceConfig:
    """CarmaConfig correctly loads appliance_monitor from site.yaml."""

    def test_site_yaml_loads_three_appliances(self) -> None:
        """site_with_appliances.yaml → CarmaConfig with 3 appliances."""
        cfg = load_config(str(SITE_WITH_APPLIANCES))

        assert cfg.appliance_monitor.enabled is True
        assert cfg.appliance_monitor.ramp_pause_on_new_load is True
        assert len(cfg.appliance_monitor.appliances) == 3

    def test_appliance_names_and_entity_ids(self) -> None:
        """Each appliance has correct name and entity_id from fixture."""
        cfg = load_config(str(SITE_WITH_APPLIANCES))
        apps = {a.id: a for a in cfg.appliance_monitor.appliances}

        assert apps["tvatt"].entity_id == "sensor.102_shelly_plug_g3_power"
        assert apps["tumlare"].entity_id == "sensor.103_shelly_plug_g3_power"
        assert apps["disk"].entity_id == "sensor.98_shelly_plug_s_power"

    def test_appliance_thresholds_from_yaml(self) -> None:
        """Tumlare has custom start_threshold_w=100 from fixture."""
        cfg = load_config(str(SITE_WITH_APPLIANCES))
        tumlare = next(a for a in cfg.appliance_monitor.appliances if a.id == "tumlare")

        assert tumlare.start_threshold_w == 100.0
        assert tumlare.stop_threshold_w == 20.0

    def test_appliance_monitor_defaults_when_not_in_yaml(self) -> None:
        """CarmaConfig without appliance_monitor → ApplianceMonitorConfig defaults."""
        # Use the production site.yaml which has no appliance_monitor section
        prod_yaml = Path(__file__).resolve().parent.parent.parent / "config" / "site.yaml"
        cfg = load_config(str(prod_yaml))

        # Defaults: enabled=True, empty list, ramp_pause=True
        assert isinstance(cfg.appliance_monitor, ApplianceMonitorConfig)
        assert cfg.appliance_monitor.appliances == []


# ---------------------------------------------------------------------------
# 2. Schema validation
# ---------------------------------------------------------------------------


class TestApplianceSchemaValidation:
    """ApplianceConfig Pydantic validators reject invalid values."""

    def test_negative_start_threshold_raises(self) -> None:
        """start_threshold_w < 0 → ValidationError."""
        from pydantic import ValidationError
        from config.schema import ApplianceConfig

        with pytest.raises(ValidationError):
            ApplianceConfig(
                id="bad",
                name="Bad",
                entity_id="sensor.bad",
                start_threshold_w=-1.0,
            )

    def test_default_thresholds_match_constants(self) -> None:
        """ApplianceConfig defaults == DEFAULT_APPLIANCE_START/STOP_W constants."""
        from config.schema import ApplianceConfig

        cfg = ApplianceConfig(id="x", name="X", entity_id="sensor.x")
        assert cfg.start_threshold_w == DEFAULT_APPLIANCE_START_W
        assert cfg.stop_threshold_w == DEFAULT_APPLIANCE_STOP_W


# ---------------------------------------------------------------------------
# 3. Snapshot pipeline: total_appliance_kw
# ---------------------------------------------------------------------------


class TestTotalApplianceKwPipeline:
    """SystemSnapshot.total_appliance_kw reflects only active appliances."""

    def _make_snapshot(self, appliances: list[ApplianceState]) -> SystemSnapshot:
        return SystemSnapshot(
            timestamp=datetime.now(tz=timezone.utc),
            batteries=[make_battery_state()],
            ev=make_ev_state(),
            grid=make_grid_state(),
            consumers=[],
            current_scenario=Scenario.PV_SURPLUS_DAY,
            hour=12,
            minute=0,
            appliances=appliances,
        )

    def test_total_appliance_kw_sums_active_only(self) -> None:
        """Only active=True appliances count toward total_appliance_kw."""
        snap = self._make_snapshot([
            ApplianceState("sensor.a", "Tvätt", active=True, power_w=120.0),
            ApplianceState("sensor.b", "Tumlare", active=True, power_w=800.0),
            ApplianceState("sensor.c", "Disk", active=False, power_w=30.0),
        ])
        # 120 + 800 = 920 W = 0.92 kW; disk is off → excluded
        assert snap.total_appliance_kw == pytest.approx(0.92, abs=0.001)

    def test_total_appliance_kw_zero_when_all_off(self) -> None:
        """All appliances inactive → total_appliance_kw == 0.0."""
        snap = self._make_snapshot([
            ApplianceState("sensor.a", "Tvätt", active=False, power_w=5.0),
        ])
        assert snap.total_appliance_kw == 0.0


# ---------------------------------------------------------------------------
# 4. Headroom pipeline: guards.py integration
# ---------------------------------------------------------------------------


class TestHeadroomPipeline:
    """Appliance load reduces Ellevio headroom in GridGuard.evaluate()."""

    @pytest.fixture()
    def guard(self) -> GridGuard:
        return GridGuard(GuardConfig(tak_kw=3.0, day_weight=1.0, night_weight=0.5))

    def test_headroom_pipeline_no_appliances(self, guard: GridGuard) -> None:
        """Baseline: headroom = effective_tak - weighted_avg (day: 3.0 - 1.5 = 1.5)."""
        result = guard.evaluate(
            batteries=[make_battery_state()],
            current_scenario=Scenario.PV_SURPLUS_DAY,
            weighted_avg_kw=1.5,
            hour=12,
            ha_connected=True,
            appliance_kw=0.0,
        )
        assert result.headroom_kw == pytest.approx(1.5, abs=0.01)

    def test_headroom_pipeline_with_appliances(self, guard: GridGuard) -> None:
        """Appliance load subtracts from headroom: 1.5 - 0.92 = 0.58 kW."""
        result = guard.evaluate(
            batteries=[make_battery_state()],
            current_scenario=Scenario.PV_SURPLUS_DAY,
            weighted_avg_kw=1.5,
            hour=12,
            ha_connected=True,
            appliance_kw=0.92,
        )
        assert result.headroom_kw == pytest.approx(0.58, abs=0.01)


# ---------------------------------------------------------------------------
# 5. EV ramp pause pipeline: ev_controller.py integration
# ---------------------------------------------------------------------------


class TestRampPausePipeline:
    """New appliance start → EV ramp pauses; existing appliance → ramp continues."""

    @pytest.fixture()
    def ctrl(self) -> EVController:
        return EVController(EVControllerConfig(
            step_interval_s=0,
            cooldown_after_start_s=0,
            cooldown_after_stop_s=0,
            max_amps=10,
            steps=(6, 8, 10),
        ))

    def test_ramp_pause_pipeline_new_appliance(self, ctrl: EVController) -> None:
        """Cycle 1: appliance off. Cycle 2: appliance on → EV NO_CHANGE."""
        # Cycle 1 — register baseline (appliance off)
        ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=6, grid_import_w=500, ellevio_headroom_w=2000,
            appliances=[ApplianceState("sensor.a", "Tvätt", active=False, power_w=0.0)],
        )
        # Cycle 2 — appliance starts
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=6, grid_import_w=500, ellevio_headroom_w=2000,
            appliances=[ApplianceState("sensor.a", "Tvätt", active=True, power_w=120.0)],
        )
        assert result.action == EVAction.NO_CHANGE
        assert "appliance" in result.reason.lower()

    def test_ramp_continues_if_appliance_already_active(self, ctrl: EVController) -> None:
        """Cycle 1: appliance on. Cycle 2: same appliance on → EV ramps up."""
        # Cycle 1 — appliance already active
        ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=6, grid_import_w=500, ellevio_headroom_w=2000,
            appliances=[ApplianceState("sensor.a", "Tvätt", active=True, power_w=120.0)],
        )
        # Cycle 2 — still on, not a new start
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=6, grid_import_w=500, ellevio_headroom_w=2000,
            appliances=[ApplianceState("sensor.a", "Tvätt", active=True, power_w=120.0)],
        )
        assert result.action == EVAction.SET_CURRENT
        assert result.target_amps == 8
