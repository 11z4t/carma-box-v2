"""Domain models for CARMA Box energy optimization.

All models are immutable dataclasses representing a point-in-time snapshot
of the system state, or a decision produced by the decision engine.
Pure data — no side effects, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, unique
from typing import Optional


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
    ems_mode: str
    ems_power_limit_w: int
    fast_charging: bool
    soh_pct: float
    cap_kwh: float
    ct_placement: str                  # "local_load" or "house_grid"
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
class ConsumerState:
    """Point-in-time state of a dispatchable consumer."""

    consumer_id: str
    name: str
    active: bool
    power_w: float
    priority: int
    priority_shed: int
    load_type: str                     # "on_off", "variable", "climate"


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
        return self.hour >= 22 or self.hour < 6


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
