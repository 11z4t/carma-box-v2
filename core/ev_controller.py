"""EV Controller with ramp logic for CARMA Box.

Manages EV charging with:
- Ramp up: 6A → 8A → 10A (configurable steps, 5 min between)
- Ramp down: reverse steps, then stop
- Cooldown timers: 2 min after start, 3 min after stop
- Emergency cut to 6A (not stop) at Ellevio breach
- XPENG G9 SoC=-1 fallback (last known value, max 1 hour)
- Scenario-specific thresholds

All thresholds from config — zero hardcoding.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum, unique
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EVControllerConfig:
    """EV controller thresholds — all from site.yaml."""

    start_amps: int = 6
    min_amps: int = 6
    max_amps: int = 10
    steps: tuple[int, ...] = (6, 8, 10)
    step_interval_s: float = 300.0       # 5 min between ramp steps
    cooldown_after_start_s: float = 120.0  # 2 min after start
    cooldown_after_stop_s: float = 180.0   # 3 min after stop
    emergency_cut_amps: int = 6
    target_soc_pct: float = 75.0
    max_soc_jump_pct: float = 20.0       # Max SoC increase per night
    soc_stale_max_s: float = 3600.0      # 1 hour max for stale SoC
    emergency_headroom_w: float = -1000.0  # Cut EV if headroom below this
    start_headroom_w: float = 1000.0       # Min headroom to start charging
    ramp_headroom_w: float = 500.0         # Min headroom for ramp up
    stop_headroom_w: float = -200.0        # Stop at min_amps if below this


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@unique
class EVAction(Enum):
    """Actions the EV controller can request."""
    NO_CHANGE = "no_change"
    START = "start"
    STOP = "stop"
    SET_CURRENT = "set_current"
    EMERGENCY_CUT = "emergency_cut"
    FIX_WAITING_IN_FULLY = "fix_waiting_in_fully"
    CONNECT_TRIGGER = "connect_trigger"


@dataclass(frozen=True)
class EVResult:
    """Result of EV controller evaluation."""

    action: EVAction
    target_amps: int = 0
    reason: str = ""


# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------


class EVTimers:
    """Cooldown and ramp timing for EV control."""

    def __init__(self, config: EVControllerConfig) -> None:
        self._config = config
        self._last_start: float = 0.0
        self._last_stop: float = 0.0
        self._last_ramp: float = 0.0

    def record_start(self) -> None:
        self._last_start = time.monotonic()

    def record_stop(self) -> None:
        self._last_stop = time.monotonic()

    def record_ramp(self) -> None:
        self._last_ramp = time.monotonic()

    def can_start(self) -> bool:
        """Can we start charging? (cooldown after stop expired)."""
        if self._last_stop == 0.0:
            return True
        return (time.monotonic() - self._last_stop) >= self._config.cooldown_after_stop_s

    def can_ramp(self) -> bool:
        """Can we change current? (cooldown after start + ramp interval)."""
        now = time.monotonic()
        if self._last_start > 0 and (now - self._last_start) < self._config.cooldown_after_start_s:
            return False
        if self._last_ramp > 0 and (now - self._last_ramp) < self._config.step_interval_s:
            return False
        return True


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class EVController:
    """EV charging controller with ramp logic.

    Pure evaluation — returns EVResult, does not execute.
    The executor handles actual adapter calls.
    """

    def __init__(self, config: EVControllerConfig | None = None) -> None:
        self._config = config or EVControllerConfig()
        self._timers = EVTimers(self._config)
        self._last_known_soc: float = -1.0
        self._last_soc_time: float = 0.0
        self._was_connected: bool = True  # Assume connected at startup (no false trigger)

    @property
    def timers(self) -> EVTimers:
        return self._timers

    def evaluate(
        self,
        ev_connected: bool,
        ev_soc_pct: float,
        charging: bool,
        current_amps: float,
        grid_import_w: float,
        ellevio_headroom_w: float,
        reason_for_no_current: str = "",
        is_night: bool = False,
        pv_surplus_w: float = 0.0,
    ) -> EVResult:
        """Evaluate EV charging decision.

        Returns EVResult with recommended action.
        """
        # XPENG SoC=-1 fallback
        soc = self._resolve_soc(ev_soc_pct)

        # Not connected → reset connect state
        if not ev_connected:
            self._was_connected = False
            return EVResult(action=EVAction.NO_CHANGE, reason="EV not connected")

        # Detect fresh connect — cable just plugged in
        just_connected = ev_connected and not self._was_connected
        self._was_connected = True
        if just_connected and not charging and soc < self._config.target_soc_pct:
            # Daytime: only trigger if PV surplus available
            if not is_night and pv_surplus_w < self._config.ramp_headroom_w:
                return EVResult(
                    action=EVAction.NO_CHANGE,
                    reason=f"EV connected but day, no PV surplus ({pv_surplus_w:.0f}W)",
                )
            # Proactive connect trigger — request consumer bump + start
            min_needed_w = float(self._config.start_amps * 230)
            headroom_short = ellevio_headroom_w < min_needed_w
            return EVResult(
                action=EVAction.CONNECT_TRIGGER,
                target_amps=self._config.start_amps,
                reason=(
                    f"EV connected (SoC {soc:.0f}%), "
                    f"headroom {ellevio_headroom_w:.0f}W"
                    f"{' — bump consumers needed' if headroom_short else ''}"
                ),
            )

        # Waiting in fully fix (B3)
        if reason_for_no_current == "waiting_in_fully":
            return EVResult(
                action=EVAction.FIX_WAITING_IN_FULLY,
                reason="B3: waiting_in_fully detected",
            )

        # At target → stop
        if soc >= self._config.target_soc_pct:
            if charging:
                self._timers.record_stop()
                return EVResult(
                    action=EVAction.STOP,
                    reason=f"SoC {soc:.0f}% >= target {self._config.target_soc_pct:.0f}%",
                )
            return EVResult(action=EVAction.NO_CHANGE, reason="At target, not charging")

        # RULE: Never charge EV from grid during daytime
        # Daytime = only charge if PV surplus (exporting)
        if not is_night and not charging and pv_surplus_w < self._config.ramp_headroom_w:
            return EVResult(
                action=EVAction.NO_CHANGE,
                reason=f"Day — no PV surplus ({pv_surplus_w:.0f}W), EV waits",
            )
        # Daytime charging active but PV gone → stop
        if not is_night and charging and pv_surplus_w < self._config.stop_headroom_w:
            return EVResult(
                action=EVAction.STOP,
                reason=f"Day — PV surplus lost ({pv_surplus_w:.0f}W), stop EV",
            )

        # Emergency cut at severe Ellevio breach (> 1kW over)
        if ellevio_headroom_w < self._config.emergency_headroom_w and charging:
            return EVResult(
                action=EVAction.EMERGENCY_CUT,
                target_amps=self._config.emergency_cut_amps,
                reason=f"Ellevio headroom negative ({ellevio_headroom_w:.0f}W)",
            )

        # Not charging → start?
        if not charging:
            if not self._timers.can_start():
                return EVResult(
                    action=EVAction.NO_CHANGE,
                    reason="Stop cooldown active",
                )
            if ellevio_headroom_w > self._config.start_headroom_w:
                self._timers.record_start()
                return EVResult(
                    action=EVAction.START,
                    target_amps=self._config.start_amps,
                    reason=(
                        f"Starting at {self._config.start_amps}A, "
                        f"headroom={ellevio_headroom_w:.0f}W"
                    ),
                )
            return EVResult(action=EVAction.NO_CHANGE, reason="Insufficient headroom to start")

        # Currently charging — ramp logic
        if not self._timers.can_ramp():
            return EVResult(action=EVAction.NO_CHANGE, reason="Ramp cooldown active")

        current_a = int(current_amps)

        # Ramp up if headroom allows
        next_up = self._next_step_up(current_a)
        if next_up is not None and ellevio_headroom_w > self._config.ramp_headroom_w:
            self._timers.record_ramp()
            return EVResult(
                action=EVAction.SET_CURRENT,
                target_amps=next_up,
                reason=f"Ramp up {current_a}A → {next_up}A, headroom={ellevio_headroom_w:.0f}W",
            )

        # Ramp down if importing too much
        if ellevio_headroom_w < self._config.stop_headroom_w and current_a <= self._config.min_amps:
            # Already at min and still over → stop
            self._timers.record_stop()
            return EVResult(
                action=EVAction.STOP,
                reason=f"At min {current_a}A with negative headroom, stopping",
            )
        next_down = self._next_step_down(current_a)
        if next_down is not None and ellevio_headroom_w < -200:
            self._timers.record_ramp()
            if next_down < self._config.min_amps:
                self._timers.record_stop()
                return EVResult(
                    action=EVAction.STOP,
                    reason=f"Ramp down below min ({current_a}A), stopping",
                )
            return EVResult(
                action=EVAction.SET_CURRENT,
                target_amps=next_down,
                reason=f"Ramp down {current_a}A → {next_down}A, headroom={ellevio_headroom_w:.0f}W",
            )

        return EVResult(action=EVAction.NO_CHANGE, reason="Stable")

    # ------------------------------------------------------------------
    # Ramp helpers
    # ------------------------------------------------------------------

    def _next_step_up(self, current_a: int) -> Optional[int]:
        """Find next ramp-up step from config.steps."""
        steps = self._config.steps
        for step in steps:
            if step > current_a:
                return step
        return None  # Already at max

    def _next_step_down(self, current_a: int) -> Optional[int]:
        """Find next ramp-down step from config.steps."""
        steps = self._config.steps
        for step in reversed(steps):
            if step < current_a:
                return step
        return None  # Already at min

    # ------------------------------------------------------------------
    # XPENG SoC fallback
    # ------------------------------------------------------------------

    def _resolve_soc(self, soc_pct: float) -> float:
        """Resolve SoC with XPENG fallback.

        XPENG G9 reports SoC=-1 when sleeping. Use last known value
        if within soc_stale_max_s (1 hour).
        """
        if soc_pct >= 0:
            self._last_known_soc = soc_pct
            self._last_soc_time = time.monotonic()
            return soc_pct

        # SoC is -1 (XPENG sleep)
        if self._last_known_soc >= 0:
            age = time.monotonic() - self._last_soc_time
            if age < self._config.soc_stale_max_s:
                logger.debug(
                    "XPENG SoC=-1, using last known %.0f%% (%.0fs old)",
                    self._last_known_soc, age,
                )
                return self._last_known_soc

        # No valid fallback — assume 50% (safe middle ground)
        logger.warning("XPENG SoC=-1, no valid fallback, assuming 50%%")
        return 50.0
