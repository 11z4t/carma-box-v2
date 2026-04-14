"""Pydantic models for CARMA Box site configuration.

Validates all fields from site.yaml with correct types, ranges, and defaults.
Every threshold and entity ID comes from configuration — zero hardcoding.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from core.models import CTPlacement


# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------

class SiteConfig(BaseModel):
    """Top-level site identification."""

    id: str = Field(..., min_length=1, description="Unique site identifier")
    name: str = Field(..., min_length=1, description="Human-readable site name")
    timezone: str = Field(default="Europe/Stockholm")
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    altitude_m: int = Field(default=0, ge=0, le=5000)


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------

class HAConfig(BaseModel):
    """Home Assistant REST API connection settings."""

    url: str = Field(..., min_length=1, description="HA base URL")
    token_env: str = Field(
        default="CARMA_HA_TOKEN",
        description="Environment variable holding the long-lived access token",
    )
    verify_ssl: bool = Field(default=False)
    timeout_s: int = Field(default=10, ge=1, le=120)
    retry_count: int = Field(default=3, ge=0, le=10)
    retry_delay_s: int = Field(default=2, ge=1, le=30)
    batch_cache_ttl_s: float = Field(default=25.0, ge=1.0, le=120.0)
    input_text_max_len: int = Field(default=255, ge=1, le=1000)


# ---------------------------------------------------------------------------
# Grid / Ellevio
# ---------------------------------------------------------------------------

class EllevioConfig(BaseModel):
    """Ellevio peak shaving tariff configuration."""

    cost_per_kw_month: float = Field(default=81.25, ge=0.0)
    top_n: int = Field(default=3, ge=1, le=10)
    tak_kw: float = Field(default=3.0, ge=0.1, le=50.0)
    night_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    day_weight: float = Field(default=1.0, ge=0.0, le=2.0)
    night_start_hour: int = Field(default=22, ge=0, le=23)
    night_end_hour: int = Field(default=6, ge=0, le=23)
    margin: float = Field(default=0.85, ge=0.5, le=1.0)
    emergency_factor: float = Field(default=1.10, ge=1.0, le=2.0)
    entity_weighted_avg: str = Field(default="")
    entity_current_peak: str = Field(default="")
    entity_dynamic_tak: str = Field(default="")

    @model_validator(mode="after")
    def validate_night_window(self) -> "EllevioConfig":
        """Ensure night_start > night_end (night window wraps midnight).

        A night window like 22→6 is valid (crosses midnight).
        A window like 6→22 would be the daytime — that's invalid here.
        """
        if self.night_start_hour <= self.night_end_hour:
            raise ValueError(
                f"night_start_hour ({self.night_start_hour}) must be greater than "
                f"night_end_hour ({self.night_end_hour}) — night window wraps midnight. "
                f"Example: night_start=22, night_end=6 means 22:00→06:00."
            )
        return self


class GridConfig(BaseModel):
    """Grid connection parameters."""

    main_fuse_a: int = Field(default=25, ge=10, le=63)
    main_fuse_phases: int = Field(default=3, ge=1, le=3)
    voltage_v: int = Field(default=230, ge=200, le=253)
    max_import_kw: float = Field(default=17.25, ge=1.0, le=50.0)
    ellevio: EllevioConfig = Field(default_factory=EllevioConfig)


# ---------------------------------------------------------------------------
# Battery / GoodWe
# ---------------------------------------------------------------------------

class GoodWeConfig(BaseModel):
    """GoodWe inverter-specific Modbus settings."""

    host: str = Field(default="", description="Inverter IP address")
    port: int = Field(default=8899, ge=1, le=65535)
    comm_addr: int = Field(default=0xF7, ge=0, le=255)
    operation_mode: str = Field(default="peak_shaving")


class BatteryEntities(BaseModel):
    """Home Assistant entity IDs for a single battery/inverter."""

    soc: str
    power: str
    cell_temp: str = Field(default="")
    pv_power: str = Field(default="")
    grid_power: str = Field(default="")
    load_power: str = Field(default="")
    ems_mode: str
    ems_power_limit: str
    fast_charging: str
    fast_charging_power: str = Field(default="")
    export_limit: str = Field(default="")
    peak_shaving_power_limit: str = Field(default="")
    soh: str = Field(default="")


class BatteryConfig(BaseModel):
    """Configuration for a single battery inverter."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    type: str = Field(default="goodwe_et")
    cap_kwh: float = Field(..., gt=0.0, le=100.0)
    min_soc_pct: float = Field(default=15.0, ge=5.0, le=50.0)
    min_soc_cold_pct: float = Field(default=20.0, ge=5.0, le=60.0)
    cold_temp_c: float = Field(default=4.0, ge=-20.0, le=20.0)
    cold_lock_temp_c: float = Field(default=0.0, ge=-30.0, le=10.0)
    max_discharge_kw: float = Field(default=5.0, gt=0.0, le=25.0)
    max_charge_kw: float = Field(default=5.0, gt=0.0, le=25.0)
    efficiency: float = Field(default=0.90, ge=0.5, le=1.0)
    ct_placement: CTPlacement = Field(
        ...,
        description="CT clamp placement: 'local_load' or 'house_grid'",
    )
    goodwe_device_id: str = Field(default="")
    goodwe: GoodWeConfig = Field(default_factory=GoodWeConfig)
    entities: BatteryEntities

    @field_validator("ct_placement", mode="before")
    @classmethod
    def validate_ct_placement(cls, v: str | CTPlacement) -> CTPlacement:
        """Convert string to CTPlacement enum."""
        if isinstance(v, CTPlacement):
            return v
        try:
            return CTPlacement(v)
        except ValueError:
            allowed = {e.value for e in CTPlacement}
            raise ValueError(
                f"ct_placement must be one of {allowed}, got '{v}'"
            )


# ---------------------------------------------------------------------------
# EV Charger
# ---------------------------------------------------------------------------

class EVChargerRampConfig(BaseModel):
    """EV charger ramp-up/ramp-down settings."""

    start_amps: int = Field(default=6, ge=6, le=16)
    step_amps: int = Field(default=1, ge=1, le=5)
    step_interval_s: int = Field(default=300, ge=30, le=900)
    steps: tuple[int, ...] = Field(default=(6, 8, 10))
    cooldown_after_start_s: int = Field(default=120, ge=0, le=600)
    cooldown_after_stop_s: int = Field(default=180, ge=0, le=600)
    emergency_cut_amps: int = Field(default=6, ge=6, le=16)


class EaseeSpecificConfig(BaseModel):
    """Easee-specific charger settings."""

    smart_charging: bool = Field(default=False)
    waiting_in_fully_fix_delay_s: int = Field(default=180, ge=0, le=600)
    max_charger_current_floor: int = Field(default=10, ge=6, le=32)


class EVChargerEntities(BaseModel):
    """Home Assistant entity IDs for the EV charger."""

    status: str
    power: str
    current: str
    enabled: str
    dynamic_charger_limit: str = Field(default="")
    max_charger_limit: str = Field(default="")
    smart_charging: str = Field(default="")
    cable_locked: str = Field(default="")
    reason_for_no_current: str = Field(default="")
    override_schedule: str = Field(default="")


class EVChargerConfig(BaseModel):
    """EV charger configuration."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    type: str = Field(default="easee")
    charger_id: str = Field(..., min_length=1, description="Charger ID (NOT device_id)")
    max_amps: int = Field(default=10, ge=6, le=32)
    min_amps: int = Field(default=6, ge=6, le=32)
    phases: int = Field(default=3, ge=1, le=3)
    voltage_v: int = Field(default=230, ge=200, le=253)
    ramp: EVChargerRampConfig = Field(default_factory=EVChargerRampConfig)
    easee: EaseeSpecificConfig = Field(default_factory=EaseeSpecificConfig)
    entities: EVChargerEntities

    @field_validator("max_amps")
    @classmethod
    def validate_max_amps(cls, v: int) -> int:
        """Hard cap at 32A; typical residential = 10-16A."""
        if v < 6:  # pragma: no cover
            raise ValueError("max_amps must be >= 6")
        return v


# ---------------------------------------------------------------------------
# EV
# ---------------------------------------------------------------------------

class EVEntities(BaseModel):
    """Home Assistant entity IDs for the electric vehicle."""

    soc: str


class EVConfig(BaseModel):
    """Electric vehicle configuration."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    battery_kwh: float = Field(..., gt=0.0, le=200.0)
    efficiency: float = Field(default=0.92, ge=0.5, le=1.0)
    daily_target_soc_pct: float = Field(default=75.0, ge=10.0, le=100.0)
    weekly_full_charge_days: int = Field(default=7, ge=1, le=30)
    max_soc_jump_pct: float = Field(default=20.0, ge=5.0, le=100.0)
    entities: EVEntities


# ---------------------------------------------------------------------------
# Consumers
# ---------------------------------------------------------------------------

class ConsumerConfig(BaseModel):
    """Configuration for a single dispatchable consumer."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    type: str = Field(default="on_off", description="on_off, variable, or climate")
    priority: int = Field(..., ge=1, le=100)
    priority_shed: int = Field(..., ge=1, le=100)
    power_w: int = Field(..., ge=0, le=50000)
    min_w: int = Field(default=0, ge=0, le=50000)
    max_w: int = Field(default=0, ge=0, le=50000)
    entity_switch: str = Field(default="")
    entity_power: str = Field(default="")
    start_export_w: int = Field(default=200, ge=0, le=10000)
    stop_import_w: int = Field(default=500, ge=0, le=10000)
    requires_active: str = Field(default="", description="ID of prerequisite consumer")
    phase_count: int = Field(default=1, ge=1, le=3)

    @field_validator("type")
    @classmethod
    def validate_consumer_type(cls, v: str) -> str:
        """Ensure consumer type is one of the known types."""
        allowed = {"on_off", "variable", "climate"}
        if v not in allowed:
            raise ValueError(f"type must be one of {allowed}, got '{v}'")
        return v


# ---------------------------------------------------------------------------
# Surplus
# ---------------------------------------------------------------------------

class ClimateBoostConfig(BaseModel):
    """Climate boost surplus configuration."""

    max_degrees: float = Field(default=2.0, ge=0.0, le=5.0)
    min_surplus_w: int = Field(default=500, ge=0, le=10000)


class SurplusConfig(BaseModel):
    """Surplus chain dispatch configuration."""

    start_delay_s: int = Field(default=60, ge=0, le=600)
    stop_delay_s: int = Field(default=180, ge=0, le=600)
    bump_delay_s: int = Field(default=60, ge=0, le=600)
    min_surplus_w: int = Field(default=50, ge=0, le=1000)
    max_switches_per_window: int = Field(default=2, ge=1, le=10)
    switch_window_min: int = Field(default=30, ge=5, le=120)
    start_threshold_kw: float = Field(default=1.0, ge=0.0, le=20.0)
    stop_threshold_kw: float = Field(default=0.5, ge=0.0, le=20.0)
    start_delay_min: int = Field(default=5, ge=0, le=30)
    stop_delay_min: int = Field(default=3, ge=0, le=30)
    climate_boost: ClimateBoostConfig = Field(default_factory=ClimateBoostConfig)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

class PricingConfig(BaseModel):
    """Electricity price data source configuration."""

    source: str = Field(default="nordpool")
    entity: str = Field(default="")
    cheap_ore: int = Field(default=30, ge=0, le=500)
    expensive_ore: int = Field(default=80, ge=0, le=1000)
    fallback_ore: int = Field(default=100, ge=0, le=1000)


# ---------------------------------------------------------------------------
# PV Forecast
# ---------------------------------------------------------------------------

class P10SafetyConfig(BaseModel):
    """P10 safety floor for conservative discharge in low-confidence forecasts."""

    threshold_kwh: float = Field(default=5.0, ge=0.0, le=50.0)
    confidence_pct: float = Field(default=20.0, ge=0.0, le=100.0)
    conservative_kw: float = Field(default=0.5, ge=0.0, le=10.0)
    moderate_kw: float = Field(default=1.0, ge=0.0, le=10.0)
    normal_kw: float = Field(default=2.0, ge=0.0, le=10.0)


class PVForecastConfig(BaseModel):
    """PV forecast data source configuration."""

    source: str = Field(default="solcast")
    entity_today: str = Field(default="")
    entity_tomorrow: str = Field(default="")
    entity_pv_high_today: str = Field(default="")
    entity_pv_high_tomorrow: str = Field(default="")
    api_calls_per_day: int = Field(default=10, ge=1, le=50)
    p10_safety: P10SafetyConfig = Field(default_factory=P10SafetyConfig)


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

class WeatherConfig(BaseModel):
    """Weather data source configuration."""

    source: str = Field(default="tempest")
    entity_pressure: str = Field(default="")
    entity_temp: str = Field(default="")
    entity_uv: str = Field(default="")
    entity_solar_radiation: str = Field(default="")


# ---------------------------------------------------------------------------
# Night / Evening planning
# ---------------------------------------------------------------------------

class NightPlanConfig(BaseModel):
    """Night planning window configuration."""

    window_start_hour: int = Field(default=22, ge=0, le=23)
    window_end_hour: int = Field(default=6, ge=0, le=23)
    house_baseload_kw: float = Field(default=2.5, ge=0.0, le=10.0)
    night_hours: int = Field(default=8, ge=1, le=12)
    appliance_margin_kwh: float = Field(default=3.0, ge=0.0, le=20.0)
    grid_charge_max_kw: float = Field(default=3.0, ge=0.0, le=10.0)
    grid_charge_price_threshold_ore: int = Field(default=60, ge=0, le=500)
    grid_charge_max_soc_pct: float = Field(default=90.0, ge=50.0, le=100.0)


class EveningPlanConfig(BaseModel):
    """Evening planning configuration."""

    start_hour: int = Field(default=17, ge=0, le=23)
    end_hour: int = Field(default=22, ge=0, le=23)
    evening_allocation_pct: int = Field(default=50, ge=0, le=100)
    morning_allocation_pct: int = Field(default=50, ge=0, le=100)


# ---------------------------------------------------------------------------
# Control loop
# ---------------------------------------------------------------------------

class DeadbandConfig(BaseModel):
    """Deadband configuration to prevent oscillation."""

    normal_w: int = Field(default=100, ge=0, le=1000)
    doubled_w: int = Field(default=200, ge=0, le=2000)
    doubled_duration_s: int = Field(default=180, ge=0, le=600)


class ExportTargetConfig(BaseModel):
    """Export target configuration."""

    rolling_avg_minutes: int = Field(default=15, ge=1, le=60)
    max_export_w: int = Field(default=0, ge=0, le=5000)
    momentary_tolerance_w: int = Field(default=200, ge=0, le=2000)


class ControlConfig(BaseModel):
    """Control loop timing and behaviour."""

    cycle_interval_s: int = Field(default=30, ge=5, le=300)
    plan_interval_s: int = Field(default=300, ge=30, le=3600)
    mode_change_cooldown_s: int = Field(default=300, ge=30, le=900)
    standby_intermediate_s: int = Field(default=300, ge=30, le=900)
    scenario_transition_s: int = Field(default=300, ge=30, le=900)
    measurement_stale_s: int = Field(default=300, ge=30, le=900)
    mode_change_clear_wait_s: int = Field(default=60, ge=5, le=300)
    mode_change_set_wait_s: int = Field(default=60, ge=5, le=300)
    mode_change_verify_wait_s: int = Field(default=30, ge=5, le=300)
    start_scenario: str = Field(
        default="MIDDAY_CHARGE",
        description="Scenario to enter at startup before first evaluation cycle.",
    )
    deadband: DeadbandConfig = Field(default_factory=DeadbandConfig)
    export_target: ExportTargetConfig = Field(default_factory=ExportTargetConfig)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

class G0Config(BaseModel):
    """Guard G0: Grid charging detection."""

    enabled: bool = Field(default=True)
    check_interval_s: int = Field(default=30, ge=5, le=300)
    action: str = Field(default="zero_ems_power_limit")


class G1Config(BaseModel):
    """Guard G1: SoC floor."""

    enabled: bool = Field(default=True)
    floor_pct: float = Field(default=15.0, ge=5.0, le=50.0)
    cold_floor_pct: float = Field(default=20.0, ge=5.0, le=60.0)
    freeze_floor_pct: float = Field(default=25.0, ge=5.0, le=70.0)
    action: str = Field(default="battery_standby")


class G2Config(BaseModel):
    """Guard G2: fast_charging + discharge_pv conflict (INV-3)."""

    enabled: bool = Field(default=True)
    action: str = Field(default="fast_charging_off")


class G3Config(BaseModel):
    """Guard G3: Ellevio breach detection."""

    enabled: bool = Field(default=True)
    breach_factor: float = Field(default=1.05, ge=1.0, le=2.0)
    action: str = Field(default="kill_loads_max_discharge")
    recovery_hold_s: int = Field(default=60, ge=0, le=600)


class G4Config(BaseModel):
    """Guard G4: Temperature-based SoC floor adjustment."""

    enabled: bool = Field(default=True)
    cold_temp_c: float = Field(default=4.0, ge=-20.0, le=20.0)
    freeze_temp_c: float = Field(default=0.0, ge=-30.0, le=10.0)
    soc_floor_raise_pct: float = Field(default=5.0, ge=0.0, le=20.0)


class G5Config(BaseModel):
    """Guard G5: Oscillation detection."""

    enabled: bool = Field(default=True)
    max_changes_per_window: int = Field(default=3, ge=1, le=20)
    window_s: int = Field(default=300, ge=60, le=900)
    doubled_deadband_s: int = Field(default=180, ge=0, le=600)


class G6Config(BaseModel):
    """Guard G6: Stale data detection."""

    enabled: bool = Field(default=True)
    threshold_s: int = Field(default=300, ge=30, le=900)
    action: str = Field(default="alarm_freeze")


class G7Config(BaseModel):
    """Guard G7: Communication loss."""

    enabled: bool = Field(default=True)
    ha_health_timeout_s: int = Field(default=30, ge=5, le=120)
    action: str = Field(default="alarm_freeze")


class GuardsConfig(BaseModel):
    """All safety guard configurations."""

    g0_grid_charging: G0Config = Field(default_factory=G0Config)
    g1_soc_floor: G1Config = Field(default_factory=G1Config)
    g2_fast_charging_conflict: G2Config = Field(default_factory=G2Config)
    g3_ellevio_breach: G3Config = Field(default_factory=G3Config)
    g4_temperature: G4Config = Field(default_factory=G4Config)
    g5_oscillation: G5Config = Field(default_factory=G5Config)
    g6_stale_data: G6Config = Field(default_factory=G6Config)
    g7_communication_lost: G7Config = Field(default_factory=G7Config)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class SQLiteConfig(BaseModel):
    """SQLite local storage configuration."""

    path: str = Field(default="/var/lib/carma-box/carma.db")
    retention_days: int = Field(default=7, ge=1, le=365)
    vacuum_interval_hours: int = Field(default=24, ge=1, le=168)


class PostgreSQLConfig(BaseModel):
    """PostgreSQL hub sync configuration."""

    host: str = Field(default="")
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(default="energy")
    user_env: str = Field(default="CARMA_PG_USER")
    password_env: str = Field(default="CARMA_PG_PASS")
    sync_interval_s: int = Field(default=300, ge=30, le=3600)
    batch_size: int = Field(default=1000, ge=1, le=10000)


class StorageConfig(BaseModel):
    """Storage layer configuration."""

    sqlite: SQLiteConfig = Field(default_factory=SQLiteConfig)
    postgresql: PostgreSQLConfig = Field(default_factory=PostgreSQLConfig)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class SlackConfig(BaseModel):
    """Slack notification configuration."""

    webhook_env: str = Field(default="CARMA_SLACK_WEBHOOK")
    channel: str = Field(default="#energy")
    notify_on: list[str] = Field(default_factory=list)


class HANotifyConfig(BaseModel):
    """Home Assistant notification configuration."""

    entity: str = Field(default="")
    notify_on: list[str] = Field(default_factory=list)


class NotificationsConfig(BaseModel):
    """All notification channels."""

    slack: SlackConfig = Field(default_factory=SlackConfig)
    ha_notify: HANotifyConfig = Field(default_factory=HANotifyConfig)


# ---------------------------------------------------------------------------
# Manual Override
# ---------------------------------------------------------------------------

class ManualOverrideConfig(BaseModel):
    """Manual override entity configuration."""

    enabled_entity: str = Field(default="")
    scenario_entity: str = Field(default="")
    strategy_entity: str = Field(default="")
    discharge_target_entity: str = Field(default="")
    scenarios: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = Field(default="INFO")
    file: str = Field(default="/var/log/carma-box/carma.log")
    max_bytes: int = Field(default=10485760, ge=1024, le=104857600)
    backup_count: int = Field(default=5, ge=0, le=20)
    cycle_log_to_db: bool = Field(default=True)
    audit_log_to_db: bool = Field(default=True)
    event_log_to_db: bool = Field(default=True)

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: str) -> str:
        """Ensure log level is valid."""
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"level must be one of {allowed}, got '{v}'")
        return upper


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

class HealthConfig(BaseModel):
    """Health-check endpoint configuration."""

    port: int = Field(default=8412, ge=1024, le=65535)
    path: str = Field(default="/health")


# ---------------------------------------------------------------------------
# Dashboard write-back
# ---------------------------------------------------------------------------


class DashboardConfig(BaseModel):
    """Entity IDs for dashboard write-back sensors."""

    entity_scenario: str = Field(default="sensor.carma_box_scenario")
    entity_rules: str = Field(default="sensor.carma_box_rules")
    entity_decision_reason: str = Field(default="sensor.carma_box_decision_reason")
    entity_plan_today: str = Field(default="input_text.v6_battery_plan_today")
    entity_plan_tomorrow: str = Field(default="input_text.v6_battery_plan_tomorrow")
    entity_plan_day3: str = Field(default="input_text.v6_battery_plan_day3")


# ---------------------------------------------------------------------------
# Root Config
# ---------------------------------------------------------------------------

class CarmaConfig(BaseModel):
    """Root configuration model — represents the entire site.yaml."""

    site: SiteConfig
    homeassistant: HAConfig
    grid: GridConfig = Field(default_factory=GridConfig)
    batteries: list[BatteryConfig]
    ev_charger: EVChargerConfig
    ev: EVConfig
    consumers: list[ConsumerConfig] = Field(default_factory=list)
    surplus: SurplusConfig = Field(default_factory=SurplusConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    pv_forecast: PVForecastConfig = Field(default_factory=PVForecastConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    night_plan: NightPlanConfig = Field(default_factory=NightPlanConfig)
    evening_plan: EveningPlanConfig = Field(default_factory=EveningPlanConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    guards: GuardsConfig = Field(default_factory=GuardsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    manual_override: ManualOverrideConfig = Field(default_factory=ManualOverrideConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)


def load_config(path: str) -> CarmaConfig:
    """Load and validate site configuration from a YAML file.

    Args:
        path: Path to the site.yaml configuration file.

    Returns:
        Validated CarmaConfig instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        yaml.YAMLError: If the YAML is malformed.
        pydantic.ValidationError: If any field fails validation.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(f"Expected a YAML mapping at top level, got {type(raw).__name__}")

    return CarmaConfig(**raw)
