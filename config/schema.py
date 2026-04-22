"""Pydantic models for CARMA Box site configuration.

Validates all fields from site.yaml with correct types, ranges, and defaults.
Every threshold and entity ID comes from configuration — zero hardcoding.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from core.budget import BudgetConfig
from core.grid_tuner import GridTunerConfig
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
    is_outdoor: bool = Field(default=False)
    cap_kwh: float = Field(..., gt=0.0, le=100.0)
    min_soc_pct: float = Field(default=15.0, ge=5.0, le=50.0)
    min_soc_cold_pct: float = Field(default=20.0, ge=5.0, le=60.0)
    cold_temp_c: float = Field(default=4.0, ge=-20.0, le=20.0)
    cold_lock_temp_c: float = Field(default=0.0, ge=-30.0, le=10.0)
    max_discharge_kw: float = Field(default=5.0, gt=0.0, le=25.0)
    max_charge_kw: float = Field(default=5.0, gt=0.0, le=25.0)
    efficiency: float = Field(default=0.90, ge=0.5, le=1.0)
    default_cell_temp_c: float = Field(default=20.0, ge=-20.0, le=50.0)
    default_soh_pct: float = Field(default=100.0, ge=0.0, le=100.0)
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
            raise ValueError(f"ct_placement must be one of {allowed}, got '{v}'")


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
    fix_off_delay_s: int = Field(default=10, ge=1, le=60)
    fix_override_delay_s: int = Field(default=5, ge=1, le=60)
    fix_on_delay_s: int = Field(default=3, ge=1, le=60)


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
    daily_target_soc_pct: float = Field(default=100.0, ge=10.0, le=100.0)
    weekly_full_charge_days: int = Field(default=7, ge=1, le=30)
    max_soc_jump_pct: float = Field(default=20.0, ge=5.0, le=100.0)
    entities: EVEntities


# ---------------------------------------------------------------------------
# Appliance monitoring
# ---------------------------------------------------------------------------


class ApplianceConfig(BaseModel):
    """Configuration for a single Shelly-monitored appliance."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    entity_id: str = Field(..., min_length=1, description="HA power sensor entity ID")
    start_threshold_w: float = Field(
        default=50.0,
        ge=0.0,
        le=10000.0,
        description="Power above this (W) -> appliance considered active",
    )
    stop_threshold_w: float = Field(
        default=10.0,
        ge=0.0,
        le=1000.0,
        description="Power below this (W) -> appliance considered stopped",
    )


class ApplianceMonitorConfig(BaseModel):
    """Appliance monitoring and EV ramp interaction configuration."""

    enabled: bool = Field(default=True)
    appliances: list[ApplianceConfig] = Field(default_factory=list)
    ramp_pause_on_new_load: bool = Field(
        default=True,
        description="Pause EV ramp when a new appliance starts",
    )


# ---------------------------------------------------------------------------
# Consumers
# ---------------------------------------------------------------------------


class ConsumerConfig(BaseModel):
    """Configuration for a single dispatchable consumer."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    type: str = Field(default="on_off", description="on_off, variable, or climate")
    # PLAT-1700: adapter dictates HOW we talk to the hardware.
    #   "shelly"          — Shelly relay via HA switch.turn_on/off (default, safe for
    #                        simple loads: pumps, heaters, rads, generic appliances).
    #   "goldshell_miner" — Goldshell SC Box II via /mcb/newconfig REST modes 0/1/2.
    #                        NEVER power-cycle via relay (hardware damage).
    #   "read_only"       — No write path; v2 only reads entity_power.
    adapter: str = Field(
        default="shelly",
        description="Adapter kind: 'shelly', 'goldshell_miner', or 'read_only'.",
    )
    dispatchable: bool = Field(
        default=True,
        description=(
            "Whether v2 may turn this consumer on/off. Must be False for "
            "consumers behind adapters that are not yet implemented or that "
            "would be damaged by power cycling (e.g. mining hardware with a "
            "pending CGMinerAdapter)."
        ),
    )
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
    role: str = Field(default="", description="Special role: cold_heater, buffer, etc")

    @field_validator("type")
    @classmethod
    def validate_consumer_type(cls, v: str) -> str:
        """Ensure consumer type is one of the known types."""
        allowed = {"on_off", "variable", "climate"}
        if v not in allowed:
            raise ValueError(f"type must be one of {allowed}, got '{v}'")
        return v

    @field_validator("adapter")
    @classmethod
    def validate_adapter(cls, v: str) -> str:
        """Ensure adapter is one of the supported adapter kinds."""
        allowed = {"shelly", "goldshell_miner", "read_only"}
        if v not in allowed:
            raise ValueError(f"adapter must be one of {allowed}, got '{v}'")
        return v

    @model_validator(mode="after")
    def validate_miner_safety(self) -> "ConsumerConfig":
        """PLAT-1700: Miners MUST NOT be controlled via shelly (power cycling
        damages ASIC hardware). If role=miner or id contains 'miner', require
        adapter != 'shelly' OR dispatchable=False.
        """
        looks_like_miner = self.role == "miner" or "miner" in self.id.lower()
        if looks_like_miner and self.adapter == "shelly" and self.dispatchable:
            raise ValueError(
                f"Consumer '{self.id}' looks like a miner (role='{self.role}', "
                f"id='{self.id}') but is configured with adapter='shelly' and "
                f"dispatchable=True. This would power-cycle the miner via relay "
                f"and damage hardware. Set adapter='goldshell_miner' OR "
                f"dispatchable=False (read-only until proper adapter wired)."
            )
        return self


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
# PLAT-1674: Night EV controller + Bat support controller
# ---------------------------------------------------------------------------


class NightEVControllerConfig(BaseModel):
    """Configuration for ev_night_controller (PLAT-1674).

    Maps directly to NightEVConfig dataclass — used to construct controller.
    """

    enabled: bool = Field(default=True)
    night_start_hour: int = Field(default=22, ge=0, le=23)
    night_end_hour: int = Field(default=6, ge=0, le=23)
    start_amps: int = Field(default=6, ge=6, le=32)
    max_amps: int = Field(default=10, ge=6, le=32)
    min_amps: int = Field(default=6, ge=6, le=32)
    ramp_step_amps: int = Field(default=1, ge=1, le=8)
    ramp_interval_s: int = Field(default=60, ge=10, le=600)
    tak_weighted_kw: float = Field(default=3.0, ge=0.5, le=20.0)
    grid_safety_margin_up: float = Field(default=0.9, ge=0.5, le=1.0)
    grid_safety_margin_down: float = Field(default=0.95, ge=0.5, le=1.0)
    ha_target_entity: str = Field(default="input_number.car_target_soc")


class BatSupportControllerConfig(BaseModel):
    """Configuration for bat_support_controller (PLAT-1674).

    Maps directly to BatSupportConfig dataclass — used to construct controller.
    """

    enabled: bool = Field(default=True)
    tak_weighted_kw: float = Field(default=3.0, ge=0.5, le=20.0)
    night_weight: float = Field(default=0.5, ge=0.1, le=1.0)
    safety_margin: float = Field(default=0.95, ge=0.5, le=1.0)
    min_soc_normal_pct: float = Field(default=15.0, ge=0.0, le=100.0)
    min_soc_cold_pct: float = Field(default=20.0, ge=0.0, le=100.0)
    cold_temp_c: float = Field(default=4.0, ge=-50.0, le=50.0)


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


class BatteryGateConfig(BaseModel):
    """Battery charge/discharge SoC gates.

    `charge_stop_soc_pct` is the single source of truth for the PV_SURPLUS
    handover. It drives:
      - StateMachineConfig.surplus_entry_soc_pct (when S8 triggers)
      - BudgetConfig.bat_charge_stop_soc_pct (when _allocate_bat stops)

    PLAT-1695: if these drift apart a dead zone forms where state machine
    says "surplus" but budget keeps charging → grid export grows at high SoC.
    """

    charge_stop_soc_pct: float = Field(
        default=95.0,
        ge=50.0,
        le=100.0,
        description="Stop bat charging at this SoC (percent). Matches S8 entry.",
    )


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
        default="PV_SURPLUS_DAY",
        description="Scenario to enter at startup before first evaluation cycle.",
    )
    deadband: DeadbandConfig = Field(default_factory=DeadbandConfig)
    export_target: ExportTargetConfig = Field(default_factory=ExportTargetConfig)
    battery_gate: BatteryGateConfig = Field(default_factory=BatteryGateConfig)


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
    timeout_s: int = Field(default=5, ge=1, le=30)


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
    force_replan_entity: str = Field(default="")
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
# Budget Allocator — PLAT-1748
# ---------------------------------------------------------------------------


class BudgetGridTunerSection(BaseModel):
    """Pydantic schema for grid-tuner tunables (mirrors GridTunerConfig).

    All fields default to the current GridTunerConfig defaults so that
    omitting ``grid_tuner:`` from site.yaml preserves existing behaviour.
    """

    enabled: bool = Field(
        default=False,
        description="Enable tiered grid-sensor fine-tuner (PLAT-1737).",
    )
    tiers_w: list[float] = Field(
        default=[50.0, 75.0, 100.0],
        description="Ascending |grid| thresholds (W) where each correction tier starts.",
    )
    corrections_w: list[int] = Field(
        default=[100, 300, 500],
        description="Correction magnitude per tier (W); must be same length as tiers_w.",
    )
    rolling_window_s: int = Field(
        default=300,
        ge=30,
        le=900,
        description="Rolling window duration (s) for anti-flap mode-change guard.",
    )
    mode_change_stability_w: float = Field(
        default=50.0,
        ge=0.0,
        le=500.0,
        description="Block mode-change when |rolling avg| < this (W).",
    )

    @model_validator(mode="after")
    def validate_tiers_and_corrections(self) -> "BudgetGridTunerSection":
        """Tiers must be ascending; corrections length must match tiers length."""
        if any(self.tiers_w[i] >= self.tiers_w[i + 1] for i in range(len(self.tiers_w) - 1)):
            raise ValueError(f"tiers_w must be strictly ascending, got {self.tiers_w}")
        if len(self.corrections_w) != len(self.tiers_w):
            raise ValueError(
                f"corrections_w length ({len(self.corrections_w)}) must match "
                f"tiers_w length ({len(self.tiers_w)})"
            )
        return self

    def to_grid_tuner_config(self) -> GridTunerConfig:
        """Convert to the immutable GridTunerConfig dataclass used by core."""
        return GridTunerConfig(
            enabled=self.enabled,
            tiers_w=tuple(self.tiers_w),
            corrections_w=tuple(self.corrections_w),
            rolling_window_s=self.rolling_window_s,
            mode_change_stability_w=self.mode_change_stability_w,
        )


class BudgetCascadeSection(BaseModel):
    """Consumer cascade tunables — prevents flapping, controls ramp-up pacing."""

    cooldown_s: float = Field(
        default=60.0,
        ge=0.0,
        le=3600.0,
        description="Minimum seconds between two switches of the same consumer.",
    )
    sustained_cycles: int = Field(
        default=2,
        ge=1,
        le=20,
        description="Consecutive export cycles required before starting next consumer.",
    )
    bat_at_max_headroom_w: int = Field(
        default=500,
        ge=0,
        le=5000,
        description=(
            "Battery counts as saturated when allocated charge-limit is within "
            "this many W of its physical max (W)."
        ),
    )


class BudgetSmoothingSection(BaseModel):
    """Grid-sensor median smoothing — rejects spurious single-cycle spikes."""

    grid_smoothing_window: int = Field(
        default=3,
        ge=1,
        le=30,
        description=(
            "Median window size (cycles). Rejects isolated sensor spikes. "
            "1 = no smoothing; 3 = good default."
        ),
    )


class BudgetAggressiveSpreadSection(BaseModel):
    """Battery SoC spread tuning — drives balancing between battery strings."""

    bat_spread_max_pct: float = Field(
        default=1.0,
        ge=0.0,
        le=100.0,
        description=(
            "SoC spread (pp) above which zero_grid uses aggressive P/S split. "
            "1.0 pp matches the user rule 'SoC diff > 1%% = P1'."
        ),
    )
    bat_aggressive_spread_pct: float = Field(
        default=1.0,
        ge=0.0,
        le=100.0,
        description="Backward-compat alias for bat_spread_max_pct; defaults to same value.",
    )
    bat_need_based_enabled: bool = Field(
        default=False,
        description=(
            "PLAT-1766: when True, balanced-path bat distribution weights by "
            "instantaneous energy need ((1-soc)*cap for charging, soc*cap for "
            "discharge) instead of pure capacity (PLAT-1755). Drives SoC "
            "convergence across banks. Default False preserves PLAT-1755 "
            "cap-only behaviour."
        ),
    )


class BudgetEmergencySection(BaseModel):
    """Emergency limits and capacity fallback parameters."""

    bat_discharge_min_soc_pct: float = Field(
        default=15.0,
        ge=5.0,
        le=50.0,
        description="Absolute SoC floor for discharge (%). GoodWe cuts AC output below 15%.",
    )
    bat_default_cap_kwh: float = Field(
        default=10.0,
        gt=0.0,
        le=200.0,
        description="Fallback battery capacity (kWh) when caller omits bat_caps.",
    )
    bat_default_max_charge_w: int = Field(
        default=5000,
        gt=0,
        le=25000,
        description="Fallback max charge rate (W) per battery when not supplied by caller.",
    )
    bat_default_max_discharge_w: int = Field(
        default=5000,
        gt=0,
        le=25000,
        description="Fallback max discharge rate (W) per battery when not supplied by caller.",
    )


class BudgetSection(BaseModel):
    """Full power-budget allocator configuration.

    Maps 1:1 to ``BudgetConfig`` in core/budget.py.  All fields default to
    the current BudgetConfig defaults so that omitting ``budget:`` from
    site.yaml is fully backwards-compatible.

    Sub-sections group related tunables:
      - grid_tuner: tiered grid-sensor fine-tuner (PLAT-1737)
      - cascade: consumer priority cascade pacing
      - smoothing: grid-sensor median smoothing
      - aggressive_spread: battery SoC spread control
      - emergency: discharge floor and capacity fallbacks
    """

    # EV charging amps
    ev_min_amps: int = Field(
        default=6,
        ge=6,
        le=32,
        description="Minimum EV charging current (A).",
    )
    ev_max_amps: int = Field(
        default=16,
        ge=6,
        le=32,
        description="Maximum EV charging current (A).",
    )
    # Battery SoC levels
    bat_soc_full_pct: float = Field(
        default=100.0,
        ge=50.0,
        le=100.0,
        description="Battery is physically full at this SoC (%). Stops bat-discharge-support path.",
    )
    # NOTE: bat_charge_stop_soc_pct is intentionally absent from BudgetSection.
    # It is ALWAYS sourced from control.battery_gate.charge_stop_soc_pct (PLAT-1695
    # invariant: must match S8 surplus_entry_soc_pct in the state machine).
    # main.py applies it as an override after calling to_budget_config().
    # Spread ratios
    bat_lower_ratio: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Fraction of power routed to lower-SoC battery when spread is active.",
    )
    bat_higher_ratio: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="Fraction of power routed to higher-SoC battery when spread is active.",
    )
    # EV ramp hold cycles
    ev_ramp_up_hold_cycles: int = Field(
        default=2,
        ge=1,
        le=20,
        description="Consecutive export cycles required before ramping EV current up.",
    )
    ev_ramp_down_hold_cycles: int = Field(
        default=1,
        ge=1,
        le=20,
        description="Consecutive import cycles required before ramping EV current down.",
    )
    # Battery discharge support
    bat_discharge_support: bool = Field(
        default=True,
        description="Allow battery to discharge to support EV charging when PV is insufficient.",
    )
    # Time-of-day boundary
    evening_cutoff_h: int = Field(
        default=17,
        ge=12,
        le=22,
        description="Hour (local) after which battery priority takes precedence over EV.",
    )
    # Sub-sections
    grid_tuner: BudgetGridTunerSection = Field(
        default_factory=BudgetGridTunerSection,
        description="Tiered grid-sensor fine-tuner settings (PLAT-1737).",
    )
    cascade: BudgetCascadeSection = Field(
        default_factory=BudgetCascadeSection,
        description="Consumer cascade pacing and anti-flap tunables.",
    )
    smoothing: BudgetSmoothingSection = Field(
        default_factory=BudgetSmoothingSection,
        description="Grid-sensor median smoothing window.",
    )
    aggressive_spread: BudgetAggressiveSpreadSection = Field(
        default_factory=BudgetAggressiveSpreadSection,
        description="Battery SoC spread control thresholds.",
    )
    emergency: BudgetEmergencySection = Field(
        default_factory=BudgetEmergencySection,
        description="Discharge floor and capacity fallback parameters.",
    )

    def to_budget_config(self) -> BudgetConfig:
        """Convert this schema section to the immutable BudgetConfig dataclass.

        Maps every field explicitly — no **kwargs magic — so mypy can verify
        that all BudgetConfig fields are accounted for.

        Note: ``bat_charge_stop_soc_pct`` is NOT set here. It is always
        derived from ``control.battery_gate.charge_stop_soc_pct`` in main.py
        to maintain the PLAT-1695 invariant (must match S8 entry SoC).
        """
        return BudgetConfig(
            ev_min_amps=self.ev_min_amps,
            ev_max_amps=self.ev_max_amps,
            bat_soc_full_pct=self.bat_soc_full_pct,
            bat_spread_max_pct=self.aggressive_spread.bat_spread_max_pct,
            bat_aggressive_spread_pct=self.aggressive_spread.bat_aggressive_spread_pct,
            bat_need_based_enabled=self.aggressive_spread.bat_need_based_enabled,
            bat_lower_ratio=self.bat_lower_ratio,
            bat_higher_ratio=self.bat_higher_ratio,
            ev_ramp_up_hold_cycles=self.ev_ramp_up_hold_cycles,
            ev_ramp_down_hold_cycles=self.ev_ramp_down_hold_cycles,
            bat_discharge_support=self.bat_discharge_support,
            evening_cutoff_h=self.evening_cutoff_h,
            bat_discharge_min_soc_pct=self.emergency.bat_discharge_min_soc_pct,
            bat_default_cap_kwh=self.emergency.bat_default_cap_kwh,
            bat_default_max_charge_w=self.emergency.bat_default_max_charge_w,
            bat_default_max_discharge_w=self.emergency.bat_default_max_discharge_w,
            cascade_cooldown_s=self.cascade.cooldown_s,
            cascade_sustained_cycles=self.cascade.sustained_cycles,
            bat_at_max_headroom_w=self.cascade.bat_at_max_headroom_w,
            grid_smoothing_window=self.smoothing.grid_smoothing_window,
            grid_tuner=self.grid_tuner.to_grid_tuner_config(),
        )


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
    appliance_monitor: ApplianceMonitorConfig = Field(default_factory=ApplianceMonitorConfig)
    surplus: SurplusConfig = Field(default_factory=SurplusConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    pv_forecast: PVForecastConfig = Field(default_factory=PVForecastConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    night_plan: NightPlanConfig = Field(default_factory=NightPlanConfig)
    evening_plan: EveningPlanConfig = Field(default_factory=EveningPlanConfig)
    # PLAT-1674: Night EV controller + bat support controller (optional)
    night_ev: NightEVControllerConfig = Field(default_factory=NightEVControllerConfig)
    bat_support: BatSupportControllerConfig = Field(default_factory=BatSupportControllerConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    guards: GuardsConfig = Field(default_factory=GuardsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    manual_override: ManualOverrideConfig = Field(default_factory=ManualOverrideConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    budget: BudgetSection = Field(
        default_factory=BudgetSection,
        description=(
            "Power budget allocator tunables (PLAT-1748). "
            "All fields default to current BudgetConfig defaults — "
            "omitting this section is backwards-compatible."
        ),
    )


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
