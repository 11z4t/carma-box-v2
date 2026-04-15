"""CARMA Box — SLO-aware Health Model (PLAT-1601).

Tracks per-session runtime metrics and evaluates system health against
defined Service Level Objectives (SLOs).  Exposed via periodic log
summaries every HEALTH_LOG_INTERVAL_S seconds.

SLOs:
    CYCLE_SUCCESS_RATE_TARGET  — minimum fraction of cycles that must succeed.
    MAX_DEGRADED_SECONDS_PER_HOUR — maximum degraded-mode time per rolling hour.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── SLO constants ─────────────────────────────────────────────────────────────

# Minimum cycle success rate to be considered healthy (AC2).
CYCLE_SUCCESS_RATE_TARGET: float = 0.99

# Maximum seconds per hour the system may spend in degraded mode (AC2).
MAX_DEGRADED_SECONDS_PER_HOUR: int = 120

# How often (seconds) the health model writes a log summary (AC4).
HEALTH_LOG_INTERVAL_S: int = 900  # 15 minutes

# Minimum number of cycles required before evaluating the success-rate SLO.
# Below this threshold there is too little data to make a reliable judgement.
MIN_CYCLES_FOR_SLO: int = 10

# Seconds per hour — used to normalise degraded_mode_seconds into a per-hour rate.
SECONDS_PER_HOUR: int = 3_600


# ── Health model ──────────────────────────────────────────────────────────────


@dataclass
class HealthModel:
    """SLO-aware runtime health tracker for a CARMA Box session.

    Counters are monotonically increasing within a session.  Call
    is_healthy() to evaluate current state against SLOs, and
    maybe_log_summary() on every cycle to emit periodic log entries.
    """

    cycles_total: int = 0
    cycles_failed: int = 0
    cycles_overrun: int = 0
    degraded_mode_seconds: float = 0.0
    commands_issued: int = 0
    commands_failed: int = 0

    _session_start: float = field(default_factory=time.monotonic, repr=False)
    _last_log_time: float = field(default_factory=time.monotonic, repr=False)

    # ── Mutation helpers ──────────────────────────────────────────────────────

    def record_cycle(self, *, failed: bool = False, overrun: bool = False) -> None:
        """Record the outcome of one control cycle."""
        self.cycles_total += 1
        if failed:
            self.cycles_failed += 1
        if overrun:
            self.cycles_overrun += 1

    def record_commands(self, *, issued: int, failed: int) -> None:
        """Record the number of commands dispatched in one cycle."""
        self.commands_issued += issued
        self.commands_failed += failed

    def add_degraded_seconds(self, seconds: float) -> None:
        """Accumulate time spent in degraded mode."""
        self.degraded_mode_seconds += seconds

    # ── SLO evaluation ────────────────────────────────────────────────────────

    def is_healthy(self) -> tuple[bool, str]:
        """Evaluate health against SLOs.

        Returns:
            (True, "healthy") if all SLOs are met.
            (False, reason) if any SLO is violated.
        """
        # Only evaluate success-rate SLO once we have enough data.
        if self.cycles_total >= MIN_CYCLES_FOR_SLO:
            success_rate = (self.cycles_total - self.cycles_failed) / self.cycles_total
            if success_rate < CYCLE_SUCCESS_RATE_TARGET:
                return False, (
                    f"cycle success rate {success_rate:.3f} below target "
                    f"{CYCLE_SUCCESS_RATE_TARGET}"
                )

        uptime_s = time.monotonic() - self._session_start
        if uptime_s >= SECONDS_PER_HOUR:
            degraded_rate = (self.degraded_mode_seconds / uptime_s) * SECONDS_PER_HOUR
            if degraded_rate > MAX_DEGRADED_SECONDS_PER_HOUR:
                return False, (
                    f"degraded mode {degraded_rate:.1f}s/h exceeds limit "
                    f"{MAX_DEGRADED_SECONDS_PER_HOUR}s/h"
                )

        return True, "healthy"

    # ── Log summary (AC4) ─────────────────────────────────────────────────────

    def maybe_log_summary(self, now: float | None = None) -> bool:
        """Emit a log summary if HEALTH_LOG_INTERVAL_S seconds have elapsed.

        Args:
            now: Current monotonic timestamp (defaults to time.monotonic()).

        Returns:
            True if a summary was logged, False otherwise.
        """
        if now is None:
            now = time.monotonic()
        if now - self._last_log_time < HEALTH_LOG_INTERVAL_S:
            return False
        self._last_log_time = now
        healthy, reason = self.is_healthy()
        logger.info(
            "[health] cycles=%d failed=%d overrun=%d degraded_s=%.1f"
            " commands=%d cmd_failed=%d healthy=%s reason=%s",
            self.cycles_total,
            self.cycles_failed,
            self.cycles_overrun,
            self.degraded_mode_seconds,
            self.commands_issued,
            self.commands_failed,
            healthy,
            reason,
        )
        return True
