"""Domain models for CARMA Box energy optimization.

All models are immutable dataclasses representing a point-in-time snapshot
of the system state, or a decision produced by the decision engine.
Pure data — no side effects, no I/O.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum, unique
from typing import Any, Optional, Protocol


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

@unique
class EMSMode(Enum):
    """GoodWe EMS mode identifiers.

    FORBIDDEN: 'auto' is never used by CARMA Box (B10).
    """

    CHARGE_PV = "charge_pv"
    DISCHARGE_PV = "discharge_pv"
    BATTERY_STANDBY = "battery_standby"
    IMPORT_AC = "import_ac"
    EXPORT_AC = "export_ac"
    CONSERVE = "conserve"
    AUTO = "auto"  # FORBIDDEN — guard G0 will correct


@unique
class CTPlacement(Enum):
    """CT clamp placement on GoodWe inverter.

    LOCAL_LOAD:  CT measures the local load at the inverter output.
                 Used for Kontor — control strategy targets local consumption.
    HOUSE_GRID:  CT measures the house grid feed-in/import point.
                 Used for Förråd — control strategy targets net grid power.
    """

    LOCAL_LOAD = "local_load"
    HOUSE_GRID = "house_grid"


# Maximum battery state-of-charge (physical ceiling — cannot exceed 100 %)
MAX_SOC_PCT: float = 100.0

# Appliance detection thresholds (Shelly sensors)
DEFAULT_APPLIANCE_START_W: float = 50.0   # power above this → appliance active
DEFAULT_APPLIANCE_STOP_W: float = 10.0    # power below this → appliance stopped


@unique
class Scenario(Enum):
    """State machine scenarios, ordered by priority (lower = higher)."""

    MORNING_DISCHARGE = "MORNING_DISCHARGE"      # S1
    FORENOON_PV_EV = "FORENOON_PV_EV"            # S2
    MIDDAY_CHARGE = "MIDDAY_CHARGE"              # S3
    EVENING_DISCHARGE = "EVENING_DISCHARGE"      # S4
    NIGHT_HIGH_PV = "NIGHT_HIGH_PV"              # S5
    NIGHT_LOW_PV = "NIGHT_LOW_PV"                # S6
    NIGHT_GRID_CHARGE = "NIGHT_GRID_CHARGE"      # S7
    PV_SURPLUS = "PV_SURPLUS"                    # S8


@unique
class GuardStatus(Enum):
    """Guard evaluation result status."""

    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    BREACH = "breach"
    FREEZE = "freeze"


@unique
class CommandType(Enum):
    """Types of commands the decision engine can emit."""

    SET_EMS_MODE = "set_ems_mode"
    SET_EMS_POWER_LIMIT = "set_ems_power_limit"
    SET_FAST_CHARGING = "set_fast_charging"
    SET_EV_CURRENT = "set_ev_current"
    START_EV_CHARGING = "start_ev_charging"
    STOP_EV_CHARGING = "stop_ev_charging"
    TURN_ON_CONSUMER = "turn_on_consumer"
    TURN_OFF_CONSUMER = "turn_off_consumer"
    CLIMATE_SET_TEMP = "climate_set_temp"
    CLIMATE_SET_MODE = "climate_set_mode"
    NO_OP = "no_op"


# ---------------------------------------------------------------------------
# State snapshots (immutable, point-in-time)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BatteryState:
    """Point-in-time state of a single battery/inverter."""

    battery_id: str
    soc_pct: float
    power_w: float                     # positive = discharge, negative = charge
    cell_temp_c: float
    pv_power_w: float
    grid_power_w: float                # positive = import, negative = export
    load_power_w: float
    ems_mode: EMSMode
    ems_power_limit_w: int
    fast_charging: bool
    soh_pct: float
    cap_kwh: float
    ct_placement: CTPlacement
    available_kwh: float = 0.0         # computed: (soc - floor) * cap * efficiency


@dataclass(frozen=True)
class EVState:
    """Point-in-time state of the EV and charger."""

    soc_pct: float
    connected: bool
    charging: bool
    power_w: float
    current_a: float
    charger_status: str
    reason_for_no_current: str = ""
    target_soc_pct: float = 75.0


@dataclass(frozen=True)
class GridState:
    """Point-in-time state of the grid connection."""

    grid_power_w: float                # positive = import, negative = export
    weighted_avg_kw: float             # Ellevio rolling weighted hourly average
    current_peak_kw: float             # Current month peak
    dynamic_tak_kw: float              # Dynamic Ellevio target
    pv_total_w: float                  # Total PV production
    price_ore: float                   # Current electricity price
    pv_forecast_today_kwh: float       # Remaining PV forecast today
    pv_forecast_tomorrow_kwh: float    # PV forecast tomorrow


@dataclass(frozen=True)
class ApplianceState:
    """Point-in-time state of a monitored Shelly appliance."""

    entity_id: str
    name: str
    active: bool
    power_w: float


@dataclass(frozen=True)
class ConsumerState:
    """Point-in-time state of a dispatchable consumer."""

    consumer_id: str
    name: str
    active: bool
    power_w: float
    priority: int
    priority_shed: int
    load_type: str                     # "on_off", "variable", "climate"
    requires_active: str = ""          # ID of prerequisite consumer (empty = no dependency)


# ---------------------------------------------------------------------------
# Shared pure functions (H3: single source of truth for common logic)
# ---------------------------------------------------------------------------


class MinSocConfig(Protocol):
    """Protocol for config objects that carry SoC floor thresholds.

    Both GuardConfig and BalancerConfig satisfy this protocol.
    Frozen dataclasses expose read-only attributes, so all members here
    are declared read-only via @property syntax (Protocol supports this).
    """

    @property
    def normal_floor_pct(self) -> float: ...
    @property
    def cold_floor_pct(self) -> float: ...
    @property
    def freeze_floor_pct(self) -> float: ...
    @property
    def cold_temp_c(self) -> float: ...
    @property
    def freeze_temp_c(self) -> float: ...
    @property
    def soh_warn_pct(self) -> float: ...
    @property
    def soh_crit_pct(self) -> float: ...
    @property
    def soh_warn_raise_pct(self) -> float: ...
    @property
    def soh_crit_raise_pct(self) -> float: ...


def effective_min_soc(
    cell_temp_c: float,
    soh_pct: float,
    cfg: MinSocConfig,
) -> float:
    """Calculate the effective minimum SoC for a battery cell.

    Takes temperature and SoH into account:
    - Below freeze_temp_c: use freeze_floor_pct
    - Below cold_temp_c:   use cold_floor_pct
    - Otherwise:           use normal_floor_pct

    SoH degradation adds to the floor:
    - soh_pct < soh_crit_pct: add soh_crit_raise_pct
    - soh_pct < soh_warn_pct: add soh_warn_raise_pct

    This is a pure function — no side effects, no I/O.
    All thresholds come from cfg — zero hardcoding.

    H3: Single canonical implementation shared by GridGuard and BatteryBalancer.
    """
    if cell_temp_c < cfg.freeze_temp_c:
        floor = cfg.freeze_floor_pct
    elif cell_temp_c < cfg.cold_temp_c:
        floor = cfg.cold_floor_pct
    else:
        floor = cfg.normal_floor_pct

    if soh_pct < cfg.soh_crit_pct:
        floor += cfg.soh_crit_raise_pct
    elif soh_pct < cfg.soh_warn_pct:
        floor += cfg.soh_warn_raise_pct

    return floor


@dataclass
class ScenarioState:
    """Mutable state of the scenario state machine.

    Tracks current scenario, entry time, transition state, and dwell time.
    NOT frozen — updated by the state machine each cycle.
    """

    current: Scenario
    entry_time: datetime
    previous: Optional[Scenario] = None
    in_transition: bool = False
    transition_start: Optional[datetime] = None
    transition_target: Optional[Scenario] = None
    # H7: monotonic entry time for dwell tracking (no timezone / naive-datetime issues)
    _entry_monotonic: float = field(default_factory=time.monotonic, init=False, repr=False)

    @property
    def dwell_s(self) -> float:
        """Seconds in current scenario since entry.

        H7: Uses time.monotonic() to avoid timezone / naive-datetime fragility.
        """
        return time.monotonic() - self._entry_monotonic


@dataclass(frozen=True)
class SystemSnapshot:
    """Complete system state for one control cycle.

    This is the SOLE input to the decision engine — everything it needs
    to make a decision is captured here. No external I/O inside decide().
    """

    timestamp: datetime
    batteries: list[BatteryState]
    ev: EVState
    grid: GridState
    consumers: list[ConsumerState]
    current_scenario: Scenario
    hour: int
    minute: int
    night_start_h: int = 22
    night_end_h: int = 6
    appliances: list[ApplianceState] = field(default_factory=list)

    @property
    def total_battery_soc_pct(self) -> float:
        """Weighted average SoC across all batteries."""
        total_cap = sum(b.cap_kwh for b in self.batteries)
        if total_cap <= 0:
            return 0.0
        return sum(b.soc_pct * b.cap_kwh for b in self.batteries) / total_cap

    @property
    def total_available_kwh(self) -> float:
        """Total available energy across all batteries."""
        return sum(b.available_kwh for b in self.batteries)

    @property
    def is_night(self) -> bool:
        """Whether current time is in the night window (22:00-06:00)."""
        return self.hour >= self.night_start_h or self.hour < self.night_end_h

    @property
    def total_appliance_kw(self) -> float:
        """Total active appliance load in kW."""
        return sum(a.power_w for a in self.appliances if a.active) / 1000.0

    @property
    def available_surplus_w(self) -> float:
        """Available surplus for dispatch (W).

        Calculated as: grid export (negative grid_power) + sum of active consumer power.
        Positive = surplus available for new consumers.
        """
        export_w = max(0.0, -self.grid.grid_power_w)
        active_consumer_w = sum(
            c.power_w for c in self.consumers if c.active
        )
        return export_w + active_consumer_w


# ---------------------------------------------------------------------------
# Decision output (immutable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    """A single command to be executed by the command executor."""

    command_type: CommandType
    target_id: str                     # battery_id, charger_id, or consumer_id
    value: int | float | str | bool | None = None  # mode string, watts int, amps int, bool
    rule_id: str = ""                  # which rule produced this command
    reason: str = ""                   # human-readable reason for audit trail


@dataclass(frozen=True)
class GuardResult:
    """Result of the grid guard evaluation for one cycle."""

    status: GuardStatus
    commands: list[Command] = field(default_factory=list)
    headroom_kw: float = 0.0
    invariant_violations: list[str] = field(default_factory=list)
    replan_needed: bool = False


@dataclass(frozen=True)
class CycleDecision:
    """Complete decision for one 30-second control cycle.

    Produced by decide(snapshot, config, plan) — a pure function.
    """

    timestamp: datetime
    scenario: Scenario
    commands: list[Command] = field(default_factory=list)
    guard_result: Optional[GuardResult] = None
    rule_id: str = ""
    reason: str = ""
    headroom_kw: float = 0.0

    @property
    def has_commands(self) -> bool:
        """Whether this decision contains any actionable commands."""
        return len(self.commands) > 0 and any(
            c.command_type != CommandType.NO_OP for c in self.commands
        )


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


class ModelEncoder(json.JSONEncoder):
    """JSON encoder for CARMA Box dataclasses.

    Handles datetime, Enum, and dataclass serialization for audit trail.
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, Enum):
            return o.value
        if isinstance(o, (set, frozenset)):
            return sorted(o, key=str)
        if hasattr(o, "__dataclass_fields__"):
            return asdict(o)
        return super().default(o)


def to_json(obj: Any) -> str:
    """Serialize any CARMA Box model to JSON string."""
    return json.dumps(obj, cls=ModelEncoder, ensure_ascii=False)
