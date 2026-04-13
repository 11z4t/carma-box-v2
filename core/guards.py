"""Safety Guards (G0-G7) for CARMA Box.

Guards are the VETO layer — they run FIRST every cycle and their output
constrains the decision engine. This is the single most critical safety
component.

Priority order (highest first):
  G0 (Grid Charging) > G1 (SoC Floor) > G2 (INV-3) > G3 (Ellevio) >
  G4 (Temperature) > G5 (Oscillation) > G6 (Stale Data) > G7 (Comm Lost)

G0 is absolute: if grid charging is detected, NOTHING else matters.
G1 is per-battery: one battery at floor doesn't stop the other.
G3 VETO overrides all decision engine output.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Optional

from core.models import BatteryState, CommandType, Scenario, effective_min_soc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@unique
class GuardLevel(Enum):
    """Severity level of a guard trigger."""
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    BREACH = "breach"
    ALARM = "alarm"
    FREEZE = "freeze"


@dataclass(frozen=True)
class GuardCommand:
    """A command emitted by a guard — takes precedence over decision engine."""

    guard_id: str           # e.g. "G0", "G1", "G3"
    command_type: CommandType
    target_id: str          # battery_id, "ev", or "all"
    value: int | float | str | bool | None = None
    reason: str = ""


@dataclass
class GuardEvaluation:
    """Result of evaluating all guards for one cycle."""

    level: GuardLevel = GuardLevel.OK
    commands: list[GuardCommand] = field(default_factory=list)
    headroom_kw: float = 0.0
    violations: list[str] = field(default_factory=list)
    replan_needed: bool = False
    # Per-battery floor tracking for G1 hysteresis
    batteries_at_floor: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Guard configuration (from site.yaml)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardConfig:
    """Guard thresholds — populated from CarmaConfig.guards."""

    # Ellevio (G3)
    tak_kw: float = 3.0
    night_weight: float = 0.5
    day_weight: float = 1.0
    night_start_hour: int = 22
    night_end_hour: int = 6
    margin: float = 0.85
    emergency_factor: float = 1.10
    recovery_hold_s: int = 60

    # SoC floor (G1)
    normal_floor_pct: float = 15.0
    cold_floor_pct: float = 20.0
    freeze_floor_pct: float = 25.0
    cold_temp_c: float = 4.0
    freeze_temp_c: float = 0.0
    hysteresis_pct: float = 5.0
    soh_warn_pct: float = 80.0       # SoH below this → raise floor
    soh_crit_pct: float = 70.0       # SoH below this → raise floor more
    soh_warn_raise_pct: float = 5.0  # Added to floor when SoH < soh_warn
    soh_crit_raise_pct: float = 10.0  # Added to floor when SoH < soh_crit

    # Oscillation (G5)
    max_changes_per_window: int = 3
    window_s: int = 300
    doubled_deadband_s: int = 180

    # Stale data (G6)
    stale_threshold_s: int = 300

    # Communication (G7)
    ha_health_timeout_s: int = 30


# ---------------------------------------------------------------------------
# GridGuard
# ---------------------------------------------------------------------------


class GridGuard:
    """Evaluates all 8 safety guards every cycle.

    Thread-safe: designed for single-threaded asyncio, no locks needed.
    Stateful: tracks oscillation history and G1 hysteresis across cycles.
    """

    def __init__(self, config: GuardConfig) -> None:
        self._config = config
        # G1: Track which batteries are currently at floor (for hysteresis)
        self._at_floor: set[str] = set()
        # G5: Sliding window of mode change timestamps
        self._mode_changes: deque[float] = deque()
        self._deadband_doubled_until: float = 0.0
        # G7: Track last successful HA contact
        self._last_ha_contact: float = time.monotonic()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def evaluate(
        self,
        batteries: list[BatteryState],
        current_scenario: Scenario,
        weighted_avg_kw: float,
        hour: int,
        ha_connected: bool,
        data_age_s: float = 0.0,
        stale_entities: Optional[list[str]] = None,
    ) -> GuardEvaluation:
        """Run all guards in priority order, collect commands.

        Returns a GuardEvaluation with level, commands, and metadata.
        """
        result = GuardEvaluation()

        # Update HA contact tracking
        if ha_connected:
            self._last_ha_contact = time.monotonic()

        # Guards in priority order — each appends to result
        self._check_g0_grid_charging(batteries, current_scenario, result)
        self._check_g1_soc_floor(batteries, result)
        self._check_g2_fast_charging_conflict(batteries, result)
        self._check_g3_ellevio(weighted_avg_kw, hour, result)
        self._check_g4_temperature(batteries, result)
        self._check_g5_oscillation(result)
        self._check_g6_stale_data(data_age_s, stale_entities or [], result)
        self._check_g7_communication(ha_connected, result)

        # Calculate headroom
        effective_tak = self._effective_tak_kw(hour)
        result.headroom_kw = effective_tak - weighted_avg_kw

        return result

    def record_mode_change(self) -> None:
        """Record a mode change for oscillation detection (G5)."""
        self._mode_changes.append(time.monotonic())

    @property
    def is_deadband_doubled(self) -> bool:
        """Whether oscillation guard has doubled the deadband."""
        return time.monotonic() < self._deadband_doubled_until

    # ------------------------------------------------------------------
    # G0: Grid Charging Detection
    # ------------------------------------------------------------------

    def _check_g0_grid_charging(
        self,
        batteries: list[BatteryState],
        scenario: Scenario,
        result: GuardEvaluation,
    ) -> None:
        """Detect unintentional grid charging.

        EXCEPTION: During NIGHT_GRID_CHARGE, grid charging is intentional.
        """
        if scenario == Scenario.NIGHT_GRID_CHARGE:
            return

        for bat in batteries:
            # Condition A: ems_power_limit > 0 in charge_pv
            if (
                bat.ems_mode == "charge_pv"
                and bat.ems_power_limit_w > 0
            ):
                logger.critical(
                    "G0 GRID CHARGING: %s ems_power_limit=%dW in charge_pv",
                    bat.battery_id, bat.ems_power_limit_w,
                )
                result.commands.append(GuardCommand(
                    guard_id="G0",
                    command_type=CommandType.SET_EMS_POWER_LIMIT,
                    target_id=bat.battery_id,
                    value=0,
                    reason=f"G0: ems_power_limit={bat.ems_power_limit_w}W in charge_pv",
                ))
                result.level = GuardLevel.CRITICAL
                result.violations.append(
                    f"G0: {bat.battery_id} grid charging (limit={bat.ems_power_limit_w}W)"
                )

            # Condition B: charging at SoC floor (autonomous)
            effective_floor = self._effective_min_soc(bat)
            if (
                bat.ems_power_limit_w > 0
                and bat.soc_pct <= effective_floor + 1.0
            ):
                logger.critical(
                    "G0 GRID CHARGING AT FLOOR: %s soc=%.1f%%, floor=%.1f%%, limit=%dW",
                    bat.battery_id, bat.soc_pct, effective_floor,
                    bat.ems_power_limit_w,
                )
                result.commands.append(GuardCommand(
                    guard_id="G0",
                    command_type=CommandType.SET_EMS_POWER_LIMIT,
                    target_id=bat.battery_id,
                    value=0,
                    reason=f"G0: grid charging at floor soc={bat.soc_pct:.1f}%",
                ))
                result.level = GuardLevel.CRITICAL
                result.violations.append(
                    f"G0: {bat.battery_id} grid charging at floor"
                )

            # Condition C: charging from grid at night (no PV)
            if (
                bat.power_w < -100  # Charging
                and bat.ems_mode not in ("charge_pv", "import_ac")
                and bat.pv_power_w < 50  # No significant PV
            ):
                logger.critical(
                    "G0 GRID CHARGING (night): %s power=%dW, mode=%s, pv=%dW",
                    bat.battery_id, bat.power_w, bat.ems_mode,
                    bat.pv_power_w,
                )
                # Full correction: standby + zero limit + fast_charging OFF
                result.commands.append(GuardCommand(
                    guard_id="G0",
                    command_type=CommandType.SET_EMS_POWER_LIMIT,
                    target_id=bat.battery_id,
                    value=0,
                    reason="G0: zero limit, night grid charging",
                ))
                result.commands.append(GuardCommand(
                    guard_id="G0",
                    command_type=CommandType.SET_FAST_CHARGING,
                    target_id=bat.battery_id,
                    value=False,
                    reason="G0: fast_charging OFF, night grid charging",
                ))
                result.commands.append(GuardCommand(
                    guard_id="G0",
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bat.battery_id,
                    value="battery_standby",
                    reason=f"G0: grid charging at night, power={bat.power_w}W",
                ))
                result.level = GuardLevel.CRITICAL
                result.violations.append(
                    f"G0: {bat.battery_id} night grid charging"
                )

    # ------------------------------------------------------------------
    # G1: SoC Floor
    # ------------------------------------------------------------------

    def _check_g1_soc_floor(
        self,
        batteries: list[BatteryState],
        result: GuardEvaluation,
    ) -> None:
        """Prevent discharge below effective minimum SoC.

        Hysteresis: resume only when SoC > floor + 5%.
        """
        for bat in batteries:
            effective_floor = self._effective_min_soc(bat)
            bat_id = bat.battery_id

            if bat.soc_pct <= effective_floor:
                # At or below floor — enforce standby
                if bat_id not in self._at_floor:
                    logger.warning(
                        "G1 SOC FLOOR: %s at %.1f%%, floor=%.1f%%",
                        bat_id, bat.soc_pct, effective_floor,
                    )
                self._at_floor.add(bat_id)
                result.batteries_at_floor.add(bat_id)
                result.commands.append(GuardCommand(
                    guard_id="G1",
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bat_id,
                    value="battery_standby",
                    reason=f"G1: soc={bat.soc_pct:.1f}% <= floor={effective_floor:.1f}%",
                ))
                if result.level.value == "ok":
                    result.level = GuardLevel.WARNING

            elif bat_id in self._at_floor:
                # Was at floor — check hysteresis
                if bat.soc_pct > effective_floor + self._config.hysteresis_pct:
                    logger.info(
                        "G1 RECOVERY: %s at %.1f%%, above floor+hysteresis (%.1f%%)",
                        bat_id, bat.soc_pct,
                        effective_floor + self._config.hysteresis_pct,
                    )
                    self._at_floor.discard(bat_id)
                else:
                    # Still in hysteresis zone — keep at standby
                    result.batteries_at_floor.add(bat_id)
                    result.commands.append(GuardCommand(
                        guard_id="G1",
                        command_type=CommandType.SET_EMS_MODE,
                        target_id=bat_id,
                        value="battery_standby",
                        reason=(
                            f"G1: hysteresis, soc={bat.soc_pct:.1f}% "
                            f"< floor+5%={effective_floor + self._config.hysteresis_pct:.1f}%"
                        ),
                    ))

    # ------------------------------------------------------------------
    # G2: INV-3 fast_charging Conflict
    # ------------------------------------------------------------------

    def _check_g2_fast_charging_conflict(
        self,
        batteries: list[BatteryState],
        result: GuardEvaluation,
    ) -> None:
        """Detect fast_charging ON + discharge_pv = firmware bug (INV-3)."""
        for bat in batteries:
            if bat.fast_charging and bat.ems_mode == "discharge_pv":
                logger.critical(
                    "G2 INV-3: %s fast_charging=ON + discharge_pv",
                    bat.battery_id,
                )
                result.commands.append(GuardCommand(
                    guard_id="G2",
                    command_type=CommandType.SET_FAST_CHARGING,
                    target_id=bat.battery_id,
                    value=False,
                    reason="G2: INV-3 fast_charging conflict with discharge_pv",
                ))
                result.level = GuardLevel.CRITICAL
                result.violations.append(
                    f"G2: {bat.battery_id} INV-3 conflict"
                )

    # ------------------------------------------------------------------
    # G3: Ellevio Breach
    # ------------------------------------------------------------------

    def _check_g3_ellevio(
        self,
        weighted_avg_kw: float,
        hour: int,
        result: GuardEvaluation,
    ) -> None:
        """Check weighted hourly average against Ellevio tak.

        Three levels:
          WARNING:  projected > tak * margin (85%)
          CRITICAL: projected > tak * emergency_factor (110%)
          BREACH:   actual > tak (100%)
        """
        effective_tak = self._effective_tak_kw(hour)
        warning_threshold = effective_tak * self._config.margin
        critical_threshold = effective_tak * self._config.emergency_factor

        # Check order: highest severity FIRST (CRITICAL > BREACH > WARNING)
        # critical_threshold (tak*1.10) > effective_tak > warning_threshold (tak*0.85)
        if weighted_avg_kw > critical_threshold:
            # CRITICAL — far above tak, emergency action needed
            logger.critical(
                "G3 CRITICAL: weighted_avg=%.2fkW > emergency=%.2fkW",
                weighted_avg_kw, critical_threshold,
            )
            result.level = GuardLevel.CRITICAL
            result.violations.append(
                f"G3: Ellevio CRITICAL {weighted_avg_kw:.2f}kW > {critical_threshold:.2f}kW"
            )
            result.replan_needed = True
            # Emergency commands: stop EV, shed consumers, max discharge
            result.commands.append(GuardCommand(
                guard_id="G3",
                command_type=CommandType.STOP_EV_CHARGING,
                target_id="ev",
                reason="G3 CRITICAL: stop EV to reduce grid import",
            ))
            result.commands.append(GuardCommand(
                guard_id="G3",
                command_type=CommandType.SET_EV_CURRENT,
                target_id="ev",
                value=6,
                reason="G3 CRITICAL: emergency cut to 6A",
            ))

        elif weighted_avg_kw > effective_tak:
            # BREACH — actual exceeds tak
            logger.critical(
                "G3 BREACH: weighted_avg=%.2fkW > tak=%.2fkW (effective)",
                weighted_avg_kw, effective_tak,
            )
            result.level = GuardLevel.BREACH
            result.violations.append(
                f"G3: Ellevio BREACH {weighted_avg_kw:.2f}kW > {effective_tak:.2f}kW"
            )
            result.replan_needed = True
            # Corrective: cut EV to 6A
            result.commands.append(GuardCommand(
                guard_id="G3",
                command_type=CommandType.SET_EV_CURRENT,
                target_id="ev",
                value=6,
                reason="G3 BREACH: cut EV to 6A",
            ))

        elif weighted_avg_kw > warning_threshold:
            # WARNING — getting close
            logger.info(
                "G3 WARNING: weighted_avg=%.2fkW > margin=%.2fkW",
                weighted_avg_kw, warning_threshold,
            )
            if result.level == GuardLevel.OK:
                result.level = GuardLevel.WARNING
            result.violations.append(
                f"G3: Ellevio WARNING {weighted_avg_kw:.2f}kW > {warning_threshold:.2f}kW"
            )

    # ------------------------------------------------------------------
    # G4: Temperature Guard
    # ------------------------------------------------------------------

    def _check_g4_temperature(
        self,
        batteries: list[BatteryState],
        result: GuardEvaluation,
    ) -> None:
        """Raise SoC floor in cold weather, block discharge at freeze."""
        for bat in batteries:
            if bat.cell_temp_c < 0.0:
                # Freeze — block discharge
                logger.warning(
                    "G4 FREEZE: %s cell_temp=%.1f°C, blocking discharge",
                    bat.battery_id, bat.cell_temp_c,
                )
                result.commands.append(GuardCommand(
                    guard_id="G4",
                    command_type=CommandType.SET_EMS_MODE,
                    target_id=bat.battery_id,
                    value="battery_standby",
                    reason=f"G4: freeze temp={bat.cell_temp_c:.1f}°C",
                ))
            # Cold (< 4°C) floor raising is handled by G1 via _effective_min_soc

    # ------------------------------------------------------------------
    # G5: Oscillation Detection
    # ------------------------------------------------------------------

    def _check_g5_oscillation(self, result: GuardEvaluation) -> None:
        """Detect rapid mode changes and double deadband."""
        now = time.monotonic()
        window_start = now - self._config.window_s

        # Purge old entries
        while self._mode_changes and self._mode_changes[0] < window_start:
            self._mode_changes.popleft()

        change_count = len(self._mode_changes)

        if change_count >= self._config.max_changes_per_window:
            logger.warning(
                "G5 OSCILLATION: %d changes in %ds window, doubling deadband",
                change_count, self._config.window_s,
            )
            self._deadband_doubled_until = now + self._config.doubled_deadband_s
            if result.level == GuardLevel.OK:
                result.level = GuardLevel.WARNING
            result.violations.append(
                f"G5: {change_count} mode changes in {self._config.window_s}s"
            )

    # ------------------------------------------------------------------
    # G6: Stale Data
    # ------------------------------------------------------------------

    def _check_g6_stale_data(
        self,
        data_age_s: float,
        stale_entities: list[str],
        result: GuardEvaluation,
    ) -> None:
        """FREEZE (not standby) on stale data.

        IMPORTANT: Do NOT go to standby on stale data. If battery is
        currently discharging, standby would cause grid spike → Ellevio breach.
        """
        if data_age_s > self._config.stale_threshold_s:
            logger.warning(
                "G6 STALE DATA: data_age=%.0fs > threshold=%ds, entities=%s",
                data_age_s, self._config.stale_threshold_s, stale_entities,
            )
            result.level = GuardLevel.FREEZE
            result.violations.append(
                f"G6: stale data ({data_age_s:.0f}s > {self._config.stale_threshold_s}s)"
            )

    # ------------------------------------------------------------------
    # G7: Communication Lost
    # ------------------------------------------------------------------

    def _check_g7_communication(
        self,
        ha_connected: bool,
        result: GuardEvaluation,
    ) -> None:
        """FREEZE on HA connection loss."""
        if not ha_connected:
            elapsed = time.monotonic() - self._last_ha_contact
            if elapsed > self._config.ha_health_timeout_s:
                logger.critical(
                    "G7 COMM LOST: HA unreachable for %.0fs", elapsed,
                )
                result.level = GuardLevel.FREEZE
                result.violations.append(
                    f"G7: HA unreachable for {elapsed:.0f}s"
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _effective_tak_kw(self, hour: int) -> float:
        """Calculate effective Ellevio tak considering night weight.

        Night (22-06): tak / night_weight = 2.0 / 0.5 = 4.0 kW
        Day  (06-22): tak / day_weight   = 2.0 / 1.0 = 2.0 kW

        B13 regression: MUST use effective_tak, never raw tak.
        """
        if self._is_night(hour):
            return self._config.tak_kw / self._config.night_weight
        return self._config.tak_kw / self._config.day_weight

    def _is_night(self, hour: int) -> bool:
        """Is the given hour in the night window?"""
        start = self._config.night_start_hour
        end = self._config.night_end_hour
        if start > end:  # Wraps midnight (22-06)
            return hour >= start or hour < end
        return start <= hour < end

    def _effective_min_soc(self, bat: BatteryState) -> float:
        """Calculate effective minimum SoC considering temperature and SoH.

        H3: Delegates to the shared pure function in core.models to avoid
        logic duplication between GridGuard and BatteryBalancer.
        """
        return effective_min_soc(bat.cell_temp_c, bat.soh_pct, self._config)
