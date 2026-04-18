"""Tests for configuration loading and validation.

Covers:
- Valid site.yaml loads without error
- Missing required fields raise ValidationError
- Out-of-range values raise ValidationError
- Default values applied correctly
- Edge cases at min/max boundaries
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from config.schema import (
    BatteryConfig,
    CarmaConfig,
    ConsumerConfig,
    EVChargerConfig,
    GridConfig,
    HAConfig,
    LoggingConfig,
    SiteConfig,
    load_config,
)
from core.models import CTPlacement


class TestLoadConfig:
    """Test the load_config() function."""

    def test_load_production_site_yaml(self, site_yaml_path: Path) -> None:
        """Production site.yaml should load and validate without error."""
        config = load_config(str(site_yaml_path))
        assert config.site.name == "Sanduddsvagen 60"
        assert config.site.id == "sanduddsvagen-60"

    def test_file_not_found_raises(self) -> None:
        """Missing config file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_config("/nonexistent/path/site.yaml")

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        """Malformed YAML should raise an error."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("  bad:\n  - [invalid yaml\n")
        with pytest.raises(Exception):
            load_config(str(bad_yaml))

    def test_non_mapping_yaml_raises(self, tmp_path: Path) -> None:
        """YAML that is not a mapping at top level should raise ValueError."""
        scalar_yaml = tmp_path / "scalar.yaml"
        scalar_yaml.write_text("just a string")
        with pytest.raises(ValueError, match="Expected a YAML mapping"):
            load_config(str(scalar_yaml))

    def test_minimal_config_loads(
        self, minimal_config_dict: dict[str, Any], tmp_path: Path
    ) -> None:
        """Minimal valid config with only required fields should load."""
        config_file = tmp_path / "minimal.yaml"
        config_file.write_text(yaml.dump(minimal_config_dict))
        config = load_config(str(config_file))
        assert config.site.id == "test-site"
        assert len(config.batteries) == 1
        assert config.batteries[0].id == "test_bat"

    def test_missing_site_raises(
        self, minimal_config_dict: dict[str, Any], tmp_path: Path
    ) -> None:
        """Missing required 'site' section should raise ValidationError."""
        del minimal_config_dict["site"]
        config_file = tmp_path / "no_site.yaml"
        config_file.write_text(yaml.dump(minimal_config_dict))
        with pytest.raises(ValidationError):
            load_config(str(config_file))

    def test_missing_batteries_raises(
        self, minimal_config_dict: dict[str, Any], tmp_path: Path
    ) -> None:
        """Missing required 'batteries' section should raise ValidationError."""
        del minimal_config_dict["batteries"]
        config_file = tmp_path / "no_batteries.yaml"
        config_file.write_text(yaml.dump(minimal_config_dict))
        with pytest.raises(ValidationError):
            load_config(str(config_file))


class TestSiteConfig:
    """Test SiteConfig validation."""

    def test_valid_site(self) -> None:
        """Valid site config should pass."""
        site = SiteConfig(id="test", name="Test", latitude=59.0, longitude=18.0)
        assert site.id == "test"

    def test_empty_id_raises(self) -> None:
        """Empty site ID should raise ValidationError."""
        with pytest.raises(ValidationError):
            SiteConfig(id="", name="Test", latitude=59.0, longitude=18.0)

    def test_latitude_out_of_range(self) -> None:
        """Latitude > 90 should raise ValidationError."""
        with pytest.raises(ValidationError):
            SiteConfig(id="t", name="T", latitude=91.0, longitude=18.0)

    def test_longitude_out_of_range(self) -> None:
        """Longitude < -180 should raise ValidationError."""
        with pytest.raises(ValidationError):
            SiteConfig(id="t", name="T", latitude=59.0, longitude=-181.0)

    def test_default_timezone(self) -> None:
        """Default timezone should be Europe/Stockholm."""
        site = SiteConfig(id="t", name="T", latitude=59.0, longitude=18.0)
        assert site.timezone == "Europe/Stockholm"


class TestHAConfig:
    """Test HAConfig validation."""

    def test_valid_ha(self) -> None:
        """Valid HA config should pass."""
        ha = HAConfig(url="http://localhost:8123")
        assert ha.timeout_s == 10
        assert ha.retry_count == 3

    def test_timeout_too_high(self) -> None:
        """Timeout > 120s should raise ValidationError."""
        with pytest.raises(ValidationError):
            HAConfig(url="http://localhost:8123", timeout_s=121)

    def test_retry_count_negative(self) -> None:
        """Negative retry count should raise ValidationError."""
        with pytest.raises(ValidationError):
            HAConfig(url="http://localhost:8123", retry_count=-1)


class TestBatteryConfig:
    """Test BatteryConfig validation."""

    def _make_battery(self, **overrides: Any) -> BatteryConfig:
        """Create a BatteryConfig with defaults."""
        defaults: dict[str, Any] = {
            "id": "test",
            "name": "Test",
            "cap_kwh": 15.0,
            "ct_placement": "house_grid",
            "entities": {
                "soc": "sensor.soc",
                "power": "sensor.power",
                "ems_mode": "select.ems_mode",
                "ems_power_limit": "number.ems_limit",
                "fast_charging": "switch.fast_charging",
            },
        }
        defaults.update(overrides)
        return BatteryConfig(**defaults)

    def test_valid_battery(self) -> None:
        """Valid battery config should pass."""
        bat = self._make_battery()
        assert bat.cap_kwh == 15.0
        assert bat.min_soc_pct == 15.0  # default

    def test_zero_capacity_raises(self) -> None:
        """Zero capacity should raise ValidationError."""
        with pytest.raises(ValidationError):
            self._make_battery(cap_kwh=0.0)

    def test_invalid_ct_placement_raises(self) -> None:
        """Invalid CT placement should raise ValidationError."""
        with pytest.raises(ValidationError, match="ct_placement"):
            self._make_battery(ct_placement="invalid")

    def test_local_load_ct_placement(self) -> None:
        """'local_load' CT placement should be accepted."""
        bat = self._make_battery(ct_placement="local_load")
        assert bat.ct_placement == CTPlacement.LOCAL_LOAD

    def test_min_soc_at_boundary(self) -> None:
        """SoC floor at minimum boundary (5%) should be accepted."""
        bat = self._make_battery(min_soc_pct=5.0)
        assert bat.min_soc_pct == 5.0

    def test_min_soc_below_boundary_raises(self) -> None:
        """SoC floor below minimum (< 5%) should raise ValidationError."""
        with pytest.raises(ValidationError):
            self._make_battery(min_soc_pct=4.9)

    def test_efficiency_range(self) -> None:
        """Efficiency must be 0.5-1.0."""
        bat = self._make_battery(efficiency=0.5)
        assert bat.efficiency == 0.5
        with pytest.raises(ValidationError):
            self._make_battery(efficiency=0.49)
        with pytest.raises(ValidationError):
            self._make_battery(efficiency=1.01)


class TestEVChargerConfig:
    """Test EVChargerConfig validation."""

    def _make_charger(self, **overrides: Any) -> EVChargerConfig:
        """Create an EVChargerConfig with defaults."""
        defaults: dict[str, Any] = {
            "id": "test",
            "name": "Test Charger",
            "charger_id": "TEST001",
            "entities": {
                "status": "sensor.status",
                "power": "sensor.power",
                "current": "sensor.current",
                "enabled": "switch.enabled",
            },
        }
        defaults.update(overrides)
        return EVChargerConfig(**defaults)

    def test_valid_charger(self) -> None:
        """Valid charger config should pass."""
        charger = self._make_charger()
        assert charger.max_amps == 10  # default
        assert charger.min_amps == 6  # default

    def test_max_amps_too_low_raises(self) -> None:
        """max_amps below 6 should raise ValidationError."""
        with pytest.raises(ValidationError):
            self._make_charger(max_amps=5)

    def test_empty_charger_id_raises(self) -> None:
        """Empty charger_id should raise ValidationError."""
        with pytest.raises(ValidationError):
            self._make_charger(charger_id="")

    def test_phases_valid_values(self) -> None:
        """Phases must be 1-3."""
        for p in (1, 2, 3):
            charger = self._make_charger(phases=p)
            assert charger.phases == p

    def test_phases_out_of_range_raises(self) -> None:
        """Phases > 3 should raise ValidationError."""
        with pytest.raises(ValidationError):
            self._make_charger(phases=4)


class TestGridConfig:
    """Test GridConfig validation."""

    def test_defaults(self) -> None:
        """Default grid config should have correct values."""
        grid = GridConfig()
        assert grid.main_fuse_a == 25
        assert grid.ellevio.tak_kw == 3.0
        assert grid.ellevio.night_weight == 0.5

    def test_ellevio_margin_range(self) -> None:
        """Ellevio margin must be 0.5-1.0."""
        grid = GridConfig(ellevio={"margin": 0.5})  # type: ignore[arg-type]
        assert grid.ellevio.margin == 0.5
        with pytest.raises(ValidationError):
            GridConfig(ellevio={"margin": 0.49})  # type: ignore[arg-type]


class TestConsumerConfig:
    """Test ConsumerConfig validation."""

    def _make_consumer(self, **overrides: Any) -> ConsumerConfig:
        defaults: dict[str, Any] = {
            "id": "test_consumer",
            "name": "Test Consumer",
            "priority": 1,
            "priority_shed": 1,
            "power_w": 400,
        }
        defaults.update(overrides)
        return ConsumerConfig(**defaults)

    def test_valid_consumer(self) -> None:
        consumer = self._make_consumer()
        assert consumer.type == "on_off"  # default

    def test_valid_types(self) -> None:
        """All valid types should be accepted."""
        for t in ("on_off", "variable", "climate"):
            c = self._make_consumer(type=t)
            assert c.type == t

    def test_invalid_type_raises(self) -> None:
        """Invalid consumer type should raise ValidationError."""
        with pytest.raises(ValidationError, match="type"):
            self._make_consumer(type="invalid_type")


class TestLoggingConfig:
    """Test LoggingConfig validation."""

    def test_valid_levels(self) -> None:
        """All standard log levels should be accepted (case insensitive)."""
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "info", "debug"):
            cfg = LoggingConfig(level=level)
            assert cfg.level == level.upper()

    def test_invalid_level_raises(self) -> None:
        """Invalid log level should raise ValidationError."""
        with pytest.raises(ValidationError, match="level"):
            LoggingConfig(level="TRACE")


class TestProductionConfig:
    """Test the production site.yaml thoroughly."""

    def test_two_batteries(self, config: CarmaConfig) -> None:
        """Production config should have exactly 2 batteries."""
        assert len(config.batteries) == 2

    def test_battery_ids(self, config: CarmaConfig) -> None:
        """Battery IDs should be 'kontor' and 'forrad'."""
        ids = {b.id for b in config.batteries}
        assert ids == {"kontor", "forrad"}

    def test_kontor_ct_local_load(self, config: CarmaConfig) -> None:
        """Kontor battery CT placement should be 'local_load'."""
        kontor = next(b for b in config.batteries if b.id == "kontor")
        assert kontor.ct_placement == CTPlacement.LOCAL_LOAD

    def test_forrad_ct_house_grid(self, config: CarmaConfig) -> None:
        """Forrad battery CT placement should be 'house_grid'."""
        forrad = next(b for b in config.batteries if b.id == "forrad")
        assert forrad.ct_placement == CTPlacement.HOUSE_GRID

    def test_kontor_capacity(self, config: CarmaConfig) -> None:
        """Kontor battery should be 15 kWh."""
        kontor = next(b for b in config.batteries if b.id == "kontor")
        assert kontor.cap_kwh == 15.0

    def test_forrad_capacity(self, config: CarmaConfig) -> None:
        """Forrad battery should be 5 kWh."""
        forrad = next(b for b in config.batteries if b.id == "forrad")
        assert forrad.cap_kwh == 5.0

    def test_ev_charger_id(self, config: CarmaConfig) -> None:
        """EV charger ID should be 'EH128405'."""
        assert config.ev_charger.charger_id == "EH128405"

    def test_ev_charger_max_amps(self, config: CarmaConfig) -> None:
        """EV charger max amps should be 10 (safety cap for 25A fuse)."""
        assert config.ev_charger.max_amps == 10

    def test_ev_battery_kwh(self, config: CarmaConfig) -> None:
        """XPENG G9 should have 92 kWh usable battery."""
        assert config.ev.battery_kwh == 92.0

    def test_ellevio_tak(self, config: CarmaConfig) -> None:
        """Ellevio weighted target should be 3.0 kW."""
        assert config.grid.ellevio.tak_kw == 3.0

    def test_cycle_interval(self, config: CarmaConfig) -> None:
        """Control loop cycle should be 30 seconds."""
        assert config.control.cycle_interval_s == 30

    def test_consumers_exist(self, config: CarmaConfig) -> None:
        """Production config should have consumers."""
        assert len(config.consumers) >= 2

    def test_miner_is_last_priority(self, config: CarmaConfig) -> None:
        """PLAT-1715: Miner (cold_heater) must be LAST in surplus cascade.

        User rule: "miner kan inte köras förrän alla före kör på max".
        Lower number = higher priority; miner must have the highest number
        among consumers so it is only started when everything else is full.
        """
        miner = next(c for c in config.consumers if c.id == "miner")
        other_priorities = [
            c.priority for c in config.consumers if c.id != "miner"
        ]
        assert miner.priority > max(other_priorities), (
            f"Miner priority={miner.priority} must be greater than all others "
            f"({other_priorities}) so miner is last in the surplus cascade."
        )

    def test_goodwe_device_ids(self, config: CarmaConfig) -> None:
        """GoodWe device IDs should match the physical hardware."""
        kontor = next(b for b in config.batteries if b.id == "kontor")
        forrad = next(b for b in config.batteries if b.id == "forrad")
        assert kontor.goodwe_device_id == "696f2a85fed59b45f2ced7fc2663984a"
        assert forrad.goodwe_device_id == "e087f4789d3713e9b18f1ff27d4e7cb9"

    def test_all_guards_enabled(self, config: CarmaConfig) -> None:
        """All safety guards should be enabled in production."""
        guards = config.guards
        assert guards.g0_grid_charging.enabled
        assert guards.g1_soc_floor.enabled
        assert guards.g2_fast_charging_conflict.enabled
        assert guards.g3_ellevio_breach.enabled
        assert guards.g4_temperature.enabled
        assert guards.g5_oscillation.enabled
        assert guards.g6_stale_data.enabled
        assert guards.g7_communication_lost.enabled

    def test_health_port(self, config: CarmaConfig) -> None:
        """Health check port should be 8412."""
        assert config.health.port == 8412

    def test_ha_entity_kontor_soc(self, config: CarmaConfig) -> None:
        """Kontor SoC entity should use real HA entity ID."""
        kontor = next(b for b in config.batteries if b.id == "kontor")
        assert kontor.entities.soc == "sensor.goodwe_battery_state_of_charge_kontor"

    def test_easee_entities(self, config: CarmaConfig) -> None:
        """Easee charger entities should use real HA entity IDs."""
        entities = config.ev_charger.entities
        assert entities.status == "sensor.easee_home_12840_status"
        assert entities.enabled == "switch.easee_home_12840_is_enabled"
        assert entities.power == "sensor.easee_home_12840_power"

    def test_ev_soc_entity(self, config: CarmaConfig) -> None:
        """EV SoC entity should use the real HA entity ID."""
        assert config.ev.entities.soc == "sensor.bil_batteri_soc"


# ---------------------------------------------------------------------------
# PLAT-1700: validate_miner_safety Pydantic validator
# ---------------------------------------------------------------------------


class TestMinerSafetyValidator:
    """PLAT-1700: ConsumerConfig.validate_miner_safety rejects unsafe configs."""

    def _base(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "id": "miner",
            "name": "Goldshell SC Box II",
            "type": "on_off",
            "role": "cold_heater",
            "adapter": "shelly",
            "dispatchable": True,
            "priority": 5,
            "priority_shed": 1,
            "power_w": 400,
            "min_w": 400,
            "max_w": 400,
            "entity_switch": "switch.miner_relay",
            "entity_power": "sensor.miner_power",
        }
        base.update(overrides)
        return base

    def test_miner_id_with_shelly_adapter_is_rejected(self) -> None:
        """id='miner' + adapter='shelly' + dispatchable=True → ValidationError."""
        from pydantic import ValidationError  # noqa: PLC0415
        from config.schema import ConsumerConfig  # noqa: PLC0415

        with pytest.raises(ValidationError) as exc_info:
            ConsumerConfig.model_validate(self._base())
        assert "miner" in str(exc_info.value).lower()
        assert "adapter" in str(exc_info.value).lower()

    def test_miner_role_with_shelly_adapter_is_rejected(self) -> None:
        """role='miner' + adapter='shelly' + dispatchable=True → ValidationError."""
        from pydantic import ValidationError  # noqa: PLC0415
        from config.schema import ConsumerConfig  # noqa: PLC0415

        with pytest.raises(ValidationError):
            ConsumerConfig.model_validate(self._base(
                id="buffer_load", role="miner",
            ))

    def test_miner_with_goldshell_adapter_passes(self) -> None:
        """adapter='goldshell_miner' → OK, no exception."""
        from config.schema import ConsumerConfig  # noqa: PLC0415

        c = ConsumerConfig.model_validate(self._base(
            adapter="goldshell_miner", entity_switch="",
        ))
        assert c.adapter == "goldshell_miner"

    def test_miner_with_shelly_but_not_dispatchable_passes(self) -> None:
        """dispatchable=False lets shelly adapter through (read-only)."""
        from config.schema import ConsumerConfig  # noqa: PLC0415

        c = ConsumerConfig.model_validate(self._base(dispatchable=False))
        assert c.dispatchable is False

    def test_non_miner_with_shelly_adapter_passes(self) -> None:
        """Ordinary consumers (pumps, heaters) keep the default shelly path."""
        from config.schema import ConsumerConfig  # noqa: PLC0415

        c = ConsumerConfig.model_validate(self._base(
            id="vp_kontor", name="Heat pump", role="",
        ))
        assert c.adapter == "shelly"

    def test_unknown_adapter_is_rejected(self) -> None:
        """Unknown adapter kinds raise ValidationError (enum guard)."""
        from pydantic import ValidationError  # noqa: PLC0415
        from config.schema import ConsumerConfig  # noqa: PLC0415

        with pytest.raises(ValidationError):
            ConsumerConfig.model_validate(self._base(adapter="tuya"))
