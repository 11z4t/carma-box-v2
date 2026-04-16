"""State Machine with 8 Scenarios for CARMA Box.

Manages scenario transitions based on time, PV forecast, battery SoC,
EV state, and grid conditions. Each scenario has entry/exit conditions
and per-cycle actions.

Priority: S1 > S2 > S3 > S4 > S5 > S6 > S7 > S8
All transitions go through 5-min standby intermediate.
Minimum dwell time: 5 min before transition allowed.

Transition matrix enforced — only allowed transitions from spec Section 4.3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.models import Scenario, ScenarioState, SystemSnapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateMachineConfig:
    """State machine thresholds — all from site.yaml."""

    start_scenario: Scenario = Scenario.PV_SURPLUS_DAY  # Initial scenario at startup
    min_dwell_s: float = 300.0          # 5 min minimum in any scenario
    pv_high_threshold_kwh: float = 15.0  # High PV forecast threshold
    pv_medium_threshold_kwh: float = 10.0  # Medium PV forecast threshold
    morning_min_soc_pct: float = 30.0    # Min SoC for morning discharge
    evening_min_soc_above_floor_pct: float = 10.0  # Min SoC above floor for evening
    normal_floor_pct: float = 15.0       # Absolute SoC floor (GoodWe limit)
    surplus_entry_soc_pct: float = 95.0  # Bat SoC for surplus mode entry
    surplus_exit_soc_pct: float = 90.0   # Bat SoC for surplus mode exit
    surplus_pv_min_w: float = 500.0      # Min PV for surplus entry
    surplus_export_min_w: float = 200.0  # Min export for surplus entry
    surplus_exit_pv_min_w: float = 200.0  # Min PV to stay in surplus
    grid_charge_max_soc_pct: float = 90.0
    grid_charge_price_threshold_ore: float = 60.0
    ev_target_soc_pct: float = 75.0
    # Time windows for scenario transitions (QC reject: no hardcoded hours)
    morning_start_h: int = 6
    morning_end_h: int = 9
    forenoon_end_h: int = 12
    midday_end_h: int = 17
    evening_end_h: int = 22


# ---------------------------------------------------------------------------
# Transition matrix
# ---------------------------------------------------------------------------

# Allowed transitions: FROM -> set of TO scenarios
_TRANSITION_MATRIX: dict[Scenario, set[Scenario]] = {
    Scenario.MORNING_DISCHARGE: {
        Scenario.FORENOON_PV_EV, Scenario.PV_SURPLUS_DAY, Scenario.PV_SURPLUS,
    },
    Scenario.FORENOON_PV_EV: {
        Scenario.PV_SURPLUS_DAY, Scenario.PV_SURPLUS,
    },
    Scenario.PV_SURPLUS_DAY: {
        Scenario.EVENING_DISCHARGE, Scenario.PV_SURPLUS,
    },
    Scenario.EVENING_DISCHARGE: {
        Scenario.NIGHT_HIGH_PV, Scenario.NIGHT_LOW_PV,
    },
    Scenario.NIGHT_HIGH_PV: {
        Scenario.MORNING_DISCHARGE, Scenario.NIGHT_GRID_CHARGE,
    },
    Scenario.NIGHT_LOW_PV: {
        Scenario.MORNING_DISCHARGE, Scenario.NIGHT_GRID_CHARGE,
    },
    Scenario.NIGHT_GRID_CHARGE: {
        Scenario.MORNING_DISCHARGE,
    },
    Scenario.PV_SURPLUS: {
        Scenario.FORENOON_PV_EV, Scenario.PV_SURPLUS_DAY, Scenario.EVENING_DISCHARGE,
    },
}

# Scenarios checked in priority order (lowest number = highest priority)
_SCENARIO_PRIORITY = [
    Scenario.MORNING_DISCHARGE,    # S1
    Scenario.FORENOON_PV_EV,       # S2
    Scenario.PV_SURPLUS_DAY,        # S3
    Scenario.EVENING_DISCHARGE,    # S4
    Scenario.NIGHT_HIGH_PV,        # S5
    Scenario.NIGHT_LOW_PV,         # S6
    Scenario.NIGHT_GRID_CHARGE,    # S7
    Scenario.PV_SURPLUS,           # S8
]


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------


class StateMachine:
    """Evaluates scenario transitions based on system state.

    Pure evaluation — does not execute commands. Returns the target
    scenario (if a transition is warranted) or None (stay in current).
    """

    def __init__(self, config: StateMachineConfig | None = None) -> None:
        self._config = config or StateMachineConfig()
        self.state = ScenarioState(
            current=self._config.start_scenario,
            entry_time=datetime.now(tz=timezone.utc),
        )
        self._manual_override: Optional[Scenario] = None

    def evaluate(self, snapshot: SystemSnapshot) -> Optional[Scenario]:
        """Evaluate whether a scenario transition is needed.

        Returns the target Scenario if transition should happen, None otherwise.
        The caller (main loop) is responsible for executing the transition
        via ModeChangeManager.
        """
        # Manual override takes absolute precedence
        if self._manual_override is not None:
            target = self._manual_override
            if target != self.state.current:
                logger.info(
                    "Manual override: %s → %s",
                    self.state.current.value, target.value,
                )
                return target
            return None

        # Check dwell time — must stay in current scenario for min_dwell_s
        if not self._can_transition():
            return None

        # Step 1: Check if current scenario should exit
        should_exit = self._should_exit(self.state.current, snapshot)

        if not should_exit:
            # Still valid in current scenario — but check if a higher-priority
            # allowed transition has become available (e.g., S8 PV_SURPLUS from S3)
            allowed = _TRANSITION_MATRIX.get(self.state.current, set())
            for scenario in _SCENARIO_PRIORITY:
                if scenario == self.state.current:
                    continue
                if scenario not in allowed:
                    continue
                if self._check_entry(scenario, snapshot):
                    logger.info(
                        "Transition (opportunistic): %s → %s",
                        self.state.current.value, scenario.value,
                    )
                    return scenario
            return None  # Stay in current

        # Step 2: Current scenario exiting — find best allowed target
        allowed = _TRANSITION_MATRIX.get(self.state.current, set())
        for scenario in _SCENARIO_PRIORITY:
            if scenario == self.state.current:
                continue
            if scenario not in allowed:
                continue
            if self._check_entry(scenario, snapshot):
                logger.info(
                    "Transition (exit): %s → %s",
                    self.state.current.value, scenario.value,
                )
                return scenario

        # M1: Catchall recovery — exit triggered but no allowed target is valid.
        # This handles stuck states where the transition matrix has no valid exit
        # (e.g., PV_SURPLUS_DAY at midnight: matrix only allows EVENING_DISCHARGE
        # and PV_SURPLUS, neither of which is valid at hour=0).
        # Force the correct scenario for the current time, bypassing the matrix.
        for scenario in _SCENARIO_PRIORITY:
            if scenario == self.state.current:
                continue
            if self._check_entry(scenario, snapshot):
                logger.warning(
                    "Catchall recovery: %s has no valid matrix exit → %s",
                    self.state.current.value, scenario.value,
                )
                return scenario

        # Truly stuck — no scenario is valid at all
        logger.warning(
            "Exit conditions met for %s but no valid transition found",
            self.state.current.value,
        )
        return None

    def transition_to(self, target: Scenario) -> None:
        """Execute a transition (called by main loop after standby)."""
        previous = self.state.current
        self.state = ScenarioState(
            current=target,
            entry_time=datetime.now(tz=timezone.utc),
            previous=previous,
        )
        logger.info(
            "Transitioned: %s → %s", previous.value, target.value,
        )

    def set_manual_override(self, scenario: Optional[Scenario]) -> None:
        """Set or clear manual override."""
        self._manual_override = scenario
        if scenario:
            logger.info("Manual override set: %s", scenario.value)
        else:
            logger.info("Manual override cleared")

    # ------------------------------------------------------------------
    # Dwell time check
    # ------------------------------------------------------------------

    def _can_transition(self) -> bool:
        """Check if minimum dwell time has elapsed."""
        return self.state.dwell_s >= self._config.min_dwell_s

    # ------------------------------------------------------------------
    # Entry conditions per scenario
    # ------------------------------------------------------------------

    # Class-level name map — avoids rebuilding the dict on every call.
    # Values are method names (str) resolved via getattr at call time.
    _ENTRY_METHODS: dict[Scenario, str] = {
        Scenario.MORNING_DISCHARGE: "_entry_s1",
        Scenario.FORENOON_PV_EV: "_entry_s2",
        Scenario.PV_SURPLUS_DAY: "_entry_s3",
        Scenario.EVENING_DISCHARGE: "_entry_s4",
        Scenario.NIGHT_HIGH_PV: "_entry_s5",
        Scenario.NIGHT_LOW_PV: "_entry_s6",
        Scenario.NIGHT_GRID_CHARGE: "_entry_s7",
        Scenario.PV_SURPLUS: "_entry_s8",
        Scenario.NIGHT_EV: "_entry_s9",
    }

    def _check_entry(self, scenario: Scenario, snap: SystemSnapshot) -> bool:
        """Check entry conditions for a scenario."""
        method_name = self._ENTRY_METHODS.get(scenario)
        if method_name is None:  # pragma: no cover
            return False
        result: bool = getattr(self, method_name)(snap)
        return result

    def _entry_s1(self, snap: SystemSnapshot) -> bool:
        """S1 MORNING_DISCHARGE: 06-09, high PV forecast, SoC > 30%."""
        cfg = self._config
        return (
            cfg.morning_start_h <= snap.hour < cfg.morning_end_h
            and snap.grid.pv_forecast_today_kwh > cfg.pv_medium_threshold_kwh
            and snap.total_battery_soc_pct > cfg.morning_min_soc_pct
        )

    def _entry_s2(self, snap: SystemSnapshot) -> bool:
        """S2 FORENOON_PV_EV: 06-12, high PV, EV connected + below target."""
        cfg = self._config
        return (
            cfg.morning_start_h <= snap.hour < cfg.forenoon_end_h
            and snap.grid.pv_forecast_today_kwh > cfg.pv_high_threshold_kwh
            and snap.ev.connected
            and snap.ev.soc_pct < cfg.ev_target_soc_pct
        )

    def _entry_s3(self, snap: SystemSnapshot) -> bool:
        """S3 PV_SURPLUS_DAY: 12-17, or ANY time if SoC critically low.

        When batteries are near floor, charge_pv is the safe fallback
        regardless of time — PV will charge if available, and standby
        prevents further drain.
        """
        cfg = self._config
        in_window = cfg.forenoon_end_h <= snap.hour < cfg.midday_end_h
        low_threshold = cfg.normal_floor_pct + cfg.evening_min_soc_above_floor_pct
        critically_low = snap.total_battery_soc_pct <= low_threshold
        return in_window or critically_low

    def _entry_s4(self, snap: SystemSnapshot) -> bool:
        """S4 EVENING_DISCHARGE: 17-22, SoC > floor + 10%."""
        cfg = self._config
        min_soc = cfg.normal_floor_pct + cfg.evening_min_soc_above_floor_pct
        return (
            cfg.midday_end_h <= snap.hour < cfg.evening_end_h
            and snap.total_battery_soc_pct > min_soc
        )

    def _entry_s5(self, snap: SystemSnapshot) -> bool:
        """S5 NIGHT_HIGH_PV: 22-06, high PV tomorrow."""
        cfg = self._config
        return (
            snap.is_night
            and snap.grid.pv_forecast_tomorrow_kwh > cfg.pv_high_threshold_kwh
        )

    def _entry_s6(self, snap: SystemSnapshot) -> bool:
        """S6 NIGHT_LOW_PV: 22-06, low PV tomorrow."""
        cfg = self._config
        return (
            snap.is_night
            and snap.grid.pv_forecast_tomorrow_kwh <= cfg.pv_high_threshold_kwh
        )

    def _entry_s7(self, snap: SystemSnapshot) -> bool:
        """S7 NIGHT_GRID_CHARGE: EV done, bat needs charge, price OK."""
        cfg = self._config
        ev_done = not snap.ev.connected or snap.ev.soc_pct >= cfg.ev_target_soc_pct
        return (
            snap.is_night
            and ev_done
            and snap.total_battery_soc_pct < cfg.grid_charge_max_soc_pct
            and snap.grid.price_ore < cfg.grid_charge_price_threshold_ore
        )

    def _entry_s8(self, snap: SystemSnapshot) -> bool:
        """S8 PV_SURPLUS: bat full, PV producing, exporting."""
        cfg = self._config
        return (
            snap.total_battery_soc_pct >= cfg.surplus_entry_soc_pct
            and snap.grid.pv_total_w > cfg.surplus_pv_min_w
            and snap.grid.grid_power_w < -cfg.surplus_export_min_w
        )

    def _entry_s9(self, snap: SystemSnapshot) -> bool:
        """S9 NIGHT_EV (PLAT-1674): natt + EV plugged + below target.

        Take precedence over NIGHT_HIGH_PV / NIGHT_LOW_PV / NIGHT_GRID_CHARGE
        when EV needs charging — engine prioritizes via scenario priority order.
        """
        cfg = self._config
        return (
            snap.is_night
            and snap.ev.connected
            and snap.ev.soc_pct < cfg.ev_target_soc_pct
        )

    # ------------------------------------------------------------------
    # Exit conditions per scenario
    # ------------------------------------------------------------------

    # Class-level name map — avoids rebuilding the dict on every call.
    _EXIT_METHODS: dict[Scenario, str] = {
        Scenario.MORNING_DISCHARGE: "_exit_s1",
        Scenario.FORENOON_PV_EV: "_exit_s2",
        Scenario.PV_SURPLUS_DAY: "_exit_s3",
        Scenario.EVENING_DISCHARGE: "_exit_s4",
        Scenario.NIGHT_HIGH_PV: "_exit_s5",
        Scenario.NIGHT_LOW_PV: "_exit_s6",
        Scenario.NIGHT_GRID_CHARGE: "_exit_s7",
        Scenario.PV_SURPLUS: "_exit_s8",
        Scenario.NIGHT_EV: "_exit_s9",
    }

    def _should_exit(self, scenario: Scenario, snap: SystemSnapshot) -> bool:
        """Check if current scenario should be exited."""
        method_name = self._EXIT_METHODS.get(scenario)
        if method_name is None:  # pragma: no cover
            return False
        result: bool = getattr(self, method_name)(snap)
        return result

    def _exit_s1(self, snap: SystemSnapshot) -> bool:
        """Exit S1: hour >= 9 or SoC at floor."""
        cfg = self._config
        return snap.hour >= cfg.morning_end_h or snap.total_battery_soc_pct <= cfg.normal_floor_pct

    def _exit_s2(self, snap: SystemSnapshot) -> bool:
        """Exit S2: hour >= 12 or EV at target or disconnected."""
        cfg = self._config
        return (
            snap.hour >= cfg.forenoon_end_h
            or snap.ev.soc_pct >= cfg.ev_target_soc_pct
            or not snap.ev.connected
        )

    def _exit_s3(self, snap: SystemSnapshot) -> bool:
        """Exit S3: outside midday window (12-17).

        Must handle midnight wrap: hour < 12 OR hour >= 17 both mean
        we're outside the midday window and should exit.
        """
        cfg = self._config
        return snap.hour >= cfg.midday_end_h or snap.hour < cfg.forenoon_end_h

    def _exit_s4(self, snap: SystemSnapshot) -> bool:
        """Exit S4: outside evening window (17-22) or SoC at floor.

        Must handle midnight wrap: hour < 17 OR hour >= 22 both mean
        we're outside the evening window.
        """
        cfg = self._config
        floor = cfg.normal_floor_pct + cfg.evening_min_soc_above_floor_pct
        outside_window = snap.hour >= cfg.evening_end_h or snap.hour < cfg.midday_end_h
        return outside_window or snap.total_battery_soc_pct <= floor

    def _exit_night(self, snap: SystemSnapshot) -> bool:
        """Exit any night scenario: hour >= 6 (daytime window)."""
        cfg = self._config
        return cfg.morning_start_h <= snap.hour < cfg.evening_end_h

    def _exit_s5(self, snap: SystemSnapshot) -> bool:
        """Exit S5 NIGHT_HIGH_PV: hour >= 6."""
        return self._exit_night(snap)

    def _exit_s6(self, snap: SystemSnapshot) -> bool:
        """Exit S6 NIGHT_LOW_PV: hour >= 6."""
        return self._exit_night(snap)

    def _exit_s7(self, snap: SystemSnapshot) -> bool:
        """Exit S7: hour >= 6 or bat full."""
        cfg = self._config
        return (
            (cfg.morning_start_h <= snap.hour < cfg.evening_end_h)
            or snap.total_battery_soc_pct >= cfg.grid_charge_max_soc_pct
        )

    def _exit_s8(self, snap: SystemSnapshot) -> bool:
        """Exit S8: bat SoC drops below threshold or PV too low."""
        cfg = self._config
        return (
            snap.total_battery_soc_pct < cfg.surplus_exit_soc_pct
            or snap.grid.pv_total_w < cfg.surplus_exit_pv_min_w
        )

    def _exit_s9(self, snap: SystemSnapshot) -> bool:
        """Exit S9 NIGHT_EV (PLAT-1674): EV target reached, EV unplugged,
        or daytime window. Hand-off to S7 NIGHT_GRID_CHARGE for bat charging.
        """
        cfg = self._config
        return (
            self._exit_night(snap)
            or not snap.ev.connected
            or snap.ev.soc_pct >= cfg.ev_target_soc_pct
        )
