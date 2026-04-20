"""Tests for PLAT-1748: BudgetSection schema + mapping to BudgetConfig.

TDD — tests written BEFORE implementation. All tests in this file
should fail (red) until schema.py and main.py are updated.

Covers:
- BudgetGridTunerSection: defaults, field ranges, validation
- BudgetCascadeSection: defaults, field ranges
- BudgetSmoothingSection: defaults, field ranges
- BudgetAggressiveSpreadSection: defaults, field ranges
- BudgetEmergencySection: defaults, field ranges
- BudgetSection: composite defaults, sub-sections nested
- CarmaConfig: budget field present with correct defaults
- Backwards-compat: missing budget: block → defaults to current BudgetConfig values
- Integration: yaml → CarmaConfig.budget → BudgetConfig round-trip
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from config.schema import (
    BudgetAggressiveSpreadSection,
    BudgetCascadeSection,
    BudgetEmergencySection,
    BudgetGridTunerSection,
    BudgetSection,
    load_config,
)
from core.budget import BudgetConfig
from core.grid_tuner import GridTunerConfig


# ---------------------------------------------------------------------------
# BudgetGridTunerSection
# ---------------------------------------------------------------------------


class TestBudgetGridTunerSection:
    """Schema model mirroring GridTunerConfig — from site.yaml."""

    def test_defaults_match_grid_tuner_config(self) -> None:
        """Schema defaults must exactly match GridTunerConfig defaults."""
        schema = BudgetGridTunerSection()
        ref = GridTunerConfig()
        assert schema.enabled == ref.enabled
        assert schema.tiers_w == list(ref.tiers_w)
        assert schema.corrections_w == list(ref.corrections_w)
        assert schema.rolling_window_s == ref.rolling_window_s
        assert schema.mode_change_stability_w == ref.mode_change_stability_w

    def test_enabled_can_be_true(self) -> None:
        """enabled=true should be accepted."""
        s = BudgetGridTunerSection(enabled=True)
        assert s.enabled is True

    def test_tiers_must_be_ascending(self) -> None:
        """tiers_w must be in ascending order."""
        with pytest.raises(ValidationError, match="ascending"):
            BudgetGridTunerSection(tiers_w=[100.0, 50.0, 75.0])

    def test_corrections_length_must_match_tiers(self) -> None:
        """corrections_w length must match tiers_w length."""
        with pytest.raises(ValidationError):
            BudgetGridTunerSection(
                tiers_w=[50.0, 75.0],
                corrections_w=[100, 200, 300],
            )

    def test_rolling_window_min(self) -> None:
        """rolling_window_s must be >= 30."""
        with pytest.raises(ValidationError):
            BudgetGridTunerSection(rolling_window_s=10)

    def test_rolling_window_max(self) -> None:
        """rolling_window_s must be <= 900."""
        with pytest.raises(ValidationError):
            BudgetGridTunerSection(rolling_window_s=1000)

    def test_stability_w_non_negative(self) -> None:
        """mode_change_stability_w must be >= 0."""
        with pytest.raises(ValidationError):
            BudgetGridTunerSection(mode_change_stability_w=-1.0)

    def test_to_grid_tuner_config(self) -> None:
        """to_grid_tuner_config() returns correct GridTunerConfig."""
        s = BudgetGridTunerSection(
            enabled=True,
            tiers_w=[40.0, 60.0, 80.0],
            corrections_w=[50, 150, 300],
            rolling_window_s=120,
            mode_change_stability_w=30.0,
        )
        cfg = s.to_grid_tuner_config()
        assert isinstance(cfg, GridTunerConfig)
        assert cfg.enabled is True
        assert cfg.tiers_w == (40.0, 60.0, 80.0)
        assert cfg.corrections_w == (50, 150, 300)
        assert cfg.rolling_window_s == 120
        assert cfg.mode_change_stability_w == 30.0


# ---------------------------------------------------------------------------
# BudgetCascadeSection
# ---------------------------------------------------------------------------


class TestBudgetCascadeSection:
    """Consumer cascade tunables sub-section."""

    def test_defaults_match_budget_config(self) -> None:
        """Schema defaults must match BudgetConfig defaults."""
        s = BudgetCascadeSection()
        ref = BudgetConfig()
        assert s.cooldown_s == ref.cascade_cooldown_s
        assert s.sustained_cycles == ref.cascade_sustained_cycles
        assert s.bat_at_max_headroom_w == ref.bat_at_max_headroom_w

    def test_cooldown_non_negative(self) -> None:
        """cooldown_s must be >= 0."""
        with pytest.raises(ValidationError):
            BudgetCascadeSection(cooldown_s=-1.0)

    def test_cooldown_max(self) -> None:
        """cooldown_s must be <= 3600."""
        with pytest.raises(ValidationError):
            BudgetCascadeSection(cooldown_s=3601.0)

    def test_sustained_cycles_min(self) -> None:
        """sustained_cycles must be >= 1."""
        with pytest.raises(ValidationError):
            BudgetCascadeSection(sustained_cycles=0)

    def test_sustained_cycles_max(self) -> None:
        """sustained_cycles must be <= 20."""
        with pytest.raises(ValidationError):
            BudgetCascadeSection(sustained_cycles=21)

    def test_headroom_non_negative(self) -> None:
        """bat_at_max_headroom_w must be >= 0."""
        with pytest.raises(ValidationError):
            BudgetCascadeSection(bat_at_max_headroom_w=-1)

    def test_headroom_max(self) -> None:
        """bat_at_max_headroom_w must be <= 5000."""
        with pytest.raises(ValidationError):
            BudgetCascadeSection(bat_at_max_headroom_w=5001)


# ---------------------------------------------------------------------------
# BudgetSmoothingSection
# ---------------------------------------------------------------------------


class TestBudgetSmoothingSection:
    """Grid smoothing sub-section."""

    def test_defaults_match_budget_config(self) -> None:
        """Schema default must match BudgetConfig.grid_smoothing_window."""
        from config.schema import BudgetSmoothingSection

        s = BudgetSmoothingSection()
        ref = BudgetConfig()
        assert s.grid_smoothing_window == ref.grid_smoothing_window

    def test_window_min(self) -> None:
        """grid_smoothing_window must be >= 1."""
        from config.schema import BudgetSmoothingSection

        with pytest.raises(ValidationError):
            BudgetSmoothingSection(grid_smoothing_window=0)

    def test_window_max(self) -> None:
        """grid_smoothing_window must be <= 30."""
        from config.schema import BudgetSmoothingSection

        with pytest.raises(ValidationError):
            BudgetSmoothingSection(grid_smoothing_window=31)


# ---------------------------------------------------------------------------
# BudgetAggressiveSpreadSection
# ---------------------------------------------------------------------------


class TestBudgetAggressiveSpreadSection:
    """Battery SoC spread tuning sub-section."""

    def test_defaults_match_budget_config(self) -> None:
        """Schema defaults must match BudgetConfig defaults."""
        s = BudgetAggressiveSpreadSection()
        ref = BudgetConfig()
        assert s.bat_spread_max_pct == ref.bat_spread_max_pct
        assert s.bat_aggressive_spread_pct == ref.bat_aggressive_spread_pct

    def test_spread_non_negative(self) -> None:
        """bat_spread_max_pct must be >= 0."""
        with pytest.raises(ValidationError):
            BudgetAggressiveSpreadSection(bat_spread_max_pct=-0.1)

    def test_spread_max(self) -> None:
        """bat_spread_max_pct must be <= 100."""
        with pytest.raises(ValidationError):
            BudgetAggressiveSpreadSection(bat_spread_max_pct=100.1)


# ---------------------------------------------------------------------------
# BudgetEmergencySection
# ---------------------------------------------------------------------------


class TestBudgetEmergencySection:
    """Battery emergency / capacity fallback sub-section."""

    def test_defaults_match_budget_config(self) -> None:
        """Schema defaults must match BudgetConfig defaults."""
        s = BudgetEmergencySection()
        ref = BudgetConfig()
        assert s.bat_discharge_min_soc_pct == ref.bat_discharge_min_soc_pct
        assert s.bat_default_cap_kwh == ref.bat_default_cap_kwh
        assert s.bat_default_max_charge_w == ref.bat_default_max_charge_w
        assert s.bat_default_max_discharge_w == ref.bat_default_max_discharge_w

    def test_min_soc_floor(self) -> None:
        """bat_discharge_min_soc_pct must be >= 5."""
        with pytest.raises(ValidationError):
            BudgetEmergencySection(bat_discharge_min_soc_pct=4.9)

    def test_min_soc_ceiling(self) -> None:
        """bat_discharge_min_soc_pct must be <= 50."""
        with pytest.raises(ValidationError):
            BudgetEmergencySection(bat_discharge_min_soc_pct=51.0)

    def test_cap_kwh_positive(self) -> None:
        """bat_default_cap_kwh must be > 0."""
        with pytest.raises(ValidationError):
            BudgetEmergencySection(bat_default_cap_kwh=0.0)

    def test_cap_kwh_max(self) -> None:
        """bat_default_cap_kwh must be <= 200."""
        with pytest.raises(ValidationError):
            BudgetEmergencySection(bat_default_cap_kwh=201.0)

    def test_max_charge_w_positive(self) -> None:
        """bat_default_max_charge_w must be > 0."""
        with pytest.raises(ValidationError):
            BudgetEmergencySection(bat_default_max_charge_w=0)

    def test_max_discharge_w_positive(self) -> None:
        """bat_default_max_discharge_w must be > 0."""
        with pytest.raises(ValidationError):
            BudgetEmergencySection(bat_default_max_discharge_w=0)


# ---------------------------------------------------------------------------
# BudgetSection (composite)
# ---------------------------------------------------------------------------


class TestBudgetSection:
    """Composite BudgetSection schema."""

    def test_defaults_match_budget_config(self) -> None:
        """All BudgetSection defaults must match BudgetConfig defaults.

        bat_charge_stop_soc_pct is intentionally absent from BudgetSection
        (sourced from control.battery_gate.charge_stop_soc_pct per PLAT-1695).
        """
        s = BudgetSection()
        ref = BudgetConfig()
        assert s.ev_min_amps == ref.ev_min_amps
        assert s.ev_max_amps == ref.ev_max_amps
        assert s.bat_soc_full_pct == ref.bat_soc_full_pct
        assert s.bat_lower_ratio == ref.bat_lower_ratio
        assert s.bat_higher_ratio == ref.bat_higher_ratio
        assert s.ev_ramp_up_hold_cycles == ref.ev_ramp_up_hold_cycles
        assert s.ev_ramp_down_hold_cycles == ref.ev_ramp_down_hold_cycles
        assert s.bat_discharge_support == ref.bat_discharge_support
        assert s.evening_cutoff_h == ref.evening_cutoff_h

    def test_sub_sections_present(self) -> None:
        """BudgetSection must expose sub-section objects."""
        s = BudgetSection()
        assert hasattr(s, "grid_tuner")
        assert hasattr(s, "cascade")
        assert hasattr(s, "smoothing")
        assert hasattr(s, "aggressive_spread")
        assert hasattr(s, "emergency")

    def test_sub_section_types(self) -> None:
        """Sub-sections must be correct types."""
        from config.schema import BudgetSmoothingSection

        s = BudgetSection()
        assert isinstance(s.grid_tuner, BudgetGridTunerSection)
        assert isinstance(s.cascade, BudgetCascadeSection)
        assert isinstance(s.smoothing, BudgetSmoothingSection)
        assert isinstance(s.aggressive_spread, BudgetAggressiveSpreadSection)
        assert isinstance(s.emergency, BudgetEmergencySection)

    def test_ev_min_amps_floor(self) -> None:
        """ev_min_amps must be >= 6."""
        with pytest.raises(ValidationError):
            BudgetSection(ev_min_amps=5)

    def test_ev_min_amps_ceiling(self) -> None:
        """ev_min_amps must be <= 32."""
        with pytest.raises(ValidationError):
            BudgetSection(ev_min_amps=33)

    def test_ev_max_amps_floor(self) -> None:
        """ev_max_amps must be >= 6."""
        with pytest.raises(ValidationError):
            BudgetSection(ev_max_amps=5)

    def test_bat_soc_full_range(self) -> None:
        """bat_soc_full_pct must be 50..100."""
        with pytest.raises(ValidationError):
            BudgetSection(bat_soc_full_pct=49.0)
        with pytest.raises(ValidationError):
            BudgetSection(bat_soc_full_pct=101.0)

    def test_bat_charge_stop_soc_not_in_section(self) -> None:
        """bat_charge_stop_soc_pct must NOT be a field on BudgetSection.

        It is sourced from control.battery_gate.charge_stop_soc_pct
        (PLAT-1695 invariant) and applied in main.py.
        """
        s = BudgetSection()
        assert not hasattr(s, "bat_charge_stop_soc_pct")

    def test_bat_lower_ratio_range(self) -> None:
        """bat_lower_ratio must be 0..1."""
        with pytest.raises(ValidationError):
            BudgetSection(bat_lower_ratio=-0.1)
        with pytest.raises(ValidationError):
            BudgetSection(bat_lower_ratio=1.1)

    def test_bat_higher_ratio_range(self) -> None:
        """bat_higher_ratio must be 0..1."""
        with pytest.raises(ValidationError):
            BudgetSection(bat_higher_ratio=-0.1)
        with pytest.raises(ValidationError):
            BudgetSection(bat_higher_ratio=1.1)

    def test_evening_cutoff_h_range(self) -> None:
        """evening_cutoff_h must be 12..22."""
        with pytest.raises(ValidationError):
            BudgetSection(evening_cutoff_h=11)
        with pytest.raises(ValidationError):
            BudgetSection(evening_cutoff_h=23)

    def test_to_budget_config_returns_all_fields(self) -> None:
        """to_budget_config() must populate all BudgetConfig fields from BudgetSection.

        bat_charge_stop_soc_pct is NOT set here — it retains BudgetConfig default.
        main.py overrides it from control.battery_gate.charge_stop_soc_pct.
        """
        s = BudgetSection(
            ev_min_amps=8,
            ev_max_amps=12,
            grid_tuner=BudgetGridTunerSection(enabled=True),
        )
        cfg = s.to_budget_config()
        assert isinstance(cfg, BudgetConfig)
        assert cfg.ev_min_amps == 8
        assert cfg.ev_max_amps == 12
        assert cfg.grid_tuner.enabled is True

    def test_to_budget_config_cascade(self) -> None:
        """to_budget_config() maps cascade sub-section correctly."""
        s = BudgetSection(cascade=BudgetCascadeSection(cooldown_s=120.0, sustained_cycles=3))
        cfg = s.to_budget_config()
        assert cfg.cascade_cooldown_s == 120.0
        assert cfg.cascade_sustained_cycles == 3

    def test_to_budget_config_smoothing(self) -> None:
        """to_budget_config() maps smoothing sub-section correctly."""
        from config.schema import BudgetSmoothingSection

        s = BudgetSection(smoothing=BudgetSmoothingSection(grid_smoothing_window=5))
        cfg = s.to_budget_config()
        assert cfg.grid_smoothing_window == 5

    def test_to_budget_config_aggressive_spread(self) -> None:
        """to_budget_config() maps aggressive_spread sub-section correctly."""
        s = BudgetSection(
            aggressive_spread=BudgetAggressiveSpreadSection(
                bat_spread_max_pct=3.0,
                bat_aggressive_spread_pct=5.0,
            )
        )
        cfg = s.to_budget_config()
        assert cfg.bat_spread_max_pct == 3.0
        assert cfg.bat_aggressive_spread_pct == 5.0

    def test_to_budget_config_emergency(self) -> None:
        """to_budget_config() maps emergency sub-section correctly."""
        s = BudgetSection(emergency=BudgetEmergencySection(bat_discharge_min_soc_pct=20.0))
        cfg = s.to_budget_config()
        assert cfg.bat_discharge_min_soc_pct == 20.0


# ---------------------------------------------------------------------------
# CarmaConfig.budget field
# ---------------------------------------------------------------------------


class TestCarmaConfigBudgetField:
    """CarmaConfig must expose a budget: field."""

    def test_budget_field_exists(self, site_yaml_path: Path) -> None:
        """Loaded config must have a budget attribute."""
        config = load_config(str(site_yaml_path))
        assert hasattr(config, "budget")

    def test_budget_defaults_without_yaml_section(
        self, minimal_config_dict: dict[str, Any], tmp_path: Path
    ) -> None:
        """Missing budget: section in yaml must default to BudgetConfig defaults."""
        assert "budget" not in minimal_config_dict
        config_file = tmp_path / "no_budget.yaml"
        config_file.write_text(yaml.dump(minimal_config_dict))
        config = load_config(str(config_file))
        ref = BudgetConfig()
        assert config.budget.ev_min_amps == ref.ev_min_amps
        assert config.budget.grid_tuner.enabled == ref.grid_tuner.enabled

    def test_budget_section_parses_from_yaml(
        self, minimal_config_dict: dict[str, Any], tmp_path: Path
    ) -> None:
        """budget: section in yaml must override defaults."""
        minimal_config_dict["budget"] = {
            "ev_min_amps": 8,
            "grid_tuner": {"enabled": True},
            "cascade": {"cooldown_s": 120.0},
            "smoothing": {"grid_smoothing_window": 5},
            "aggressive_spread": {"bat_aggressive_spread_pct": 5.0},
            "emergency": {"bat_discharge_min_soc_pct": 20.0},
        }
        config_file = tmp_path / "with_budget.yaml"
        config_file.write_text(yaml.dump(minimal_config_dict))
        config = load_config(str(config_file))
        assert config.budget.ev_min_amps == 8
        assert config.budget.grid_tuner.enabled is True
        assert config.budget.cascade.cooldown_s == 120.0
        assert config.budget.smoothing.grid_smoothing_window == 5
        assert config.budget.aggressive_spread.bat_aggressive_spread_pct == 5.0
        assert config.budget.emergency.bat_discharge_min_soc_pct == 20.0


# ---------------------------------------------------------------------------
# Integration: yaml → BudgetConfig round-trip via main.py mapping
# ---------------------------------------------------------------------------


class TestBudgetRoundTrip:
    """Integration: full yaml → CarmaConfig → BudgetConfig path."""

    def test_production_yaml_round_trip(self, site_yaml_path: Path) -> None:
        """Production site.yaml round-trips to valid BudgetConfig."""
        config = load_config(str(site_yaml_path))
        budget_cfg = config.budget.to_budget_config()
        assert isinstance(budget_cfg, BudgetConfig)
        # bat_charge_stop_soc_pct is sourced from control.battery_gate (PLAT-1695);
        # round-trip through to_budget_config retains the BudgetConfig default.
        assert 50.0 <= budget_cfg.bat_charge_stop_soc_pct <= 100.0

    def test_round_trip_preserves_grid_tuner(
        self, minimal_config_dict: dict[str, Any], tmp_path: Path
    ) -> None:
        """Enabling grid_tuner via yaml is preserved through round-trip."""
        minimal_config_dict["budget"] = {
            "grid_tuner": {
                "enabled": True,
                "tiers_w": [40.0, 70.0, 100.0],
                "corrections_w": [80, 200, 400],
            }
        }
        config_file = tmp_path / "round_trip.yaml"
        config_file.write_text(yaml.dump(minimal_config_dict))
        config = load_config(str(config_file))
        cfg = config.budget.to_budget_config()
        assert cfg.grid_tuner.enabled is True
        assert cfg.grid_tuner.tiers_w == (40.0, 70.0, 100.0)
        assert cfg.grid_tuner.corrections_w == (80, 200, 400)

    def test_round_trip_all_sub_sections(
        self, minimal_config_dict: dict[str, Any], tmp_path: Path
    ) -> None:
        """All sub-sections propagate correctly to BudgetConfig."""
        minimal_config_dict["budget"] = {
            "ev_min_amps": 7,
            "ev_max_amps": 14,
            "bat_soc_full_pct": 98.0,
            "bat_lower_ratio": 0.7,
            "bat_higher_ratio": 0.3,
            "ev_ramp_up_hold_cycles": 3,
            "ev_ramp_down_hold_cycles": 2,
            "bat_discharge_support": False,
            "evening_cutoff_h": 18,
            "cascade": {
                "cooldown_s": 90.0,
                "sustained_cycles": 4,
                "bat_at_max_headroom_w": 300,
            },
            "smoothing": {"grid_smoothing_window": 7},
            "aggressive_spread": {
                "bat_spread_max_pct": 2.0,
                "bat_aggressive_spread_pct": 3.0,
            },
            "emergency": {
                "bat_discharge_min_soc_pct": 18.0,
                "bat_default_cap_kwh": 12.0,
                "bat_default_max_charge_w": 6000,
                "bat_default_max_discharge_w": 7000,
            },
            "grid_tuner": {
                "enabled": False,
                "rolling_window_s": 180,
                "mode_change_stability_w": 40.0,
            },
        }
        config_file = tmp_path / "all_sections.yaml"
        config_file.write_text(yaml.dump(minimal_config_dict))
        config = load_config(str(config_file))
        cfg = config.budget.to_budget_config()

        assert cfg.ev_min_amps == 7
        assert cfg.ev_max_amps == 14
        assert cfg.bat_soc_full_pct == 98.0
        assert cfg.bat_lower_ratio == 0.7
        assert cfg.bat_higher_ratio == 0.3
        assert cfg.ev_ramp_up_hold_cycles == 3
        assert cfg.ev_ramp_down_hold_cycles == 2
        assert cfg.bat_discharge_support is False
        assert cfg.evening_cutoff_h == 18
        assert cfg.cascade_cooldown_s == 90.0
        assert cfg.cascade_sustained_cycles == 4
        assert cfg.bat_at_max_headroom_w == 300
        assert cfg.grid_smoothing_window == 7
        assert cfg.bat_spread_max_pct == 2.0
        assert cfg.bat_aggressive_spread_pct == 3.0
        assert cfg.bat_discharge_min_soc_pct == 18.0
        assert cfg.bat_default_cap_kwh == 12.0
        assert cfg.bat_default_max_charge_w == 6000
        assert cfg.bat_default_max_discharge_w == 7000
        assert cfg.grid_tuner.rolling_window_s == 180
        assert cfg.grid_tuner.mode_change_stability_w == 40.0
