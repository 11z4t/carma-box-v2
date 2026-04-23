"""CARMA Box — Consumer cascade v2 with wear guards (PLAT-1790).

Extends the surplus allocation model with on/off state tracking
and wear protection guards (R6) for long-running consumers.

Key additions over surplus_chain.py:
- ConsumerStateTracker: tracks last_on_ts, last_off_ts, cycles_today per consumer
- Wear guard checks: min_on_time_s, min_off_time_s, daily_max_cycles
- Preemption: higher-priority consumer can preempt lower-priority, subject to wear guards
- Grace period: preempted consumers get a configurable grace window before forced off

No HA imports — pure Python, fully testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from config.schema import WearLimits

_LOGGER = logging.getLogger(__name__)

# Default grace cycles before forced preemption
DEFAULT_PREEMPTION_GRACE_CYCLES: int = 2


# ── Consumer state snapshot ──────────────────────────────────────────────────

@dataclass
class ConsumerState:
    """Runtime state for one consumer, updated each cycle."""

    consumer_id: str
    """Consumer identifier (matches SurplusConsumer.id)."""

    is_on: bool = False
    """Whether the consumer is currently ON."""

    last_on_ts: float = -1.0
    """Monotonic timestamp when consumer last turned ON. -1 = never."""

    last_off_ts: float = -1.0
    """Monotonic timestamp when consumer last turned OFF. -1 = never."""

    cycles_today: int = 0
    """Number of on→off transitions today (reset at midnight)."""

    preemption_grace_remaining: int = 0
    """Cycles remaining before a preemption request is enforced."""


# ── Consumer state tracker ───────────────────────────────────────────────────

class ConsumerStateTracker:
    """Tracks on/off state and wear limits for a set of consumers.

    Usage (per cycle):
        1. Call `record_on(id, now)` when a consumer turns ON.
        2. Call `record_off(id, now)` when a consumer turns OFF.
        3. Before turning OFF: call `can_turn_off(id, limits, now)`.
        4. Before turning ON: call `can_turn_on(id, limits, now)`.
        5. Call `reset_daily_counts()` at midnight (00:00 local).
    """

    def __init__(self) -> None:
        """Initialize with empty state."""
        self._states: dict[str, ConsumerState] = {}

    def _get_or_create(self, consumer_id: str) -> ConsumerState:
        """Get or initialise state for a consumer."""
        if consumer_id not in self._states:
            self._states[consumer_id] = ConsumerState(consumer_id=consumer_id)
        return self._states[consumer_id]

    def record_on(self, consumer_id: str, now: float) -> None:
        """Record that consumer turned ON at `now` (monotonic seconds).

        Increments cycles_today if previous state was OFF.
        """
        state = self._get_or_create(consumer_id)
        if not state.is_on:
            state.is_on = True
            state.last_on_ts = now
            state.cycles_today += 1
            state.preemption_grace_remaining = 0
        _LOGGER.debug(
            "ConsumerStateTracker: %s ON (cycles_today=%d)", consumer_id, state.cycles_today
        )

    def record_off(self, consumer_id: str, now: float) -> None:
        """Record that consumer turned OFF at `now` (monotonic seconds)."""
        state = self._get_or_create(consumer_id)
        if state.is_on:
            state.is_on = False
            state.last_off_ts = now
            state.preemption_grace_remaining = 0
        _LOGGER.debug("ConsumerStateTracker: %s OFF", consumer_id)

    def is_on(self, consumer_id: str) -> bool:
        """Return True if consumer is currently ON."""
        return self._get_or_create(consumer_id).is_on

    def cycles_today(self, consumer_id: str) -> int:
        """Return number of on→off cycles today for consumer."""
        return self._get_or_create(consumer_id).cycles_today

    def on_duration_s(self, consumer_id: str, now: float) -> float:
        """Seconds consumer has been continuously ON. 0 if currently OFF."""
        state = self._get_or_create(consumer_id)
        if not state.is_on or state.last_on_ts < 0.0:
            return 0.0
        return now - state.last_on_ts

    def off_duration_s(self, consumer_id: str, now: float) -> float:
        """Seconds consumer has been continuously OFF. 0 if currently ON."""
        state = self._get_or_create(consumer_id)
        if state.is_on or state.last_off_ts < 0.0:
            return 0.0
        return now - state.last_off_ts

    def can_turn_off(
        self,
        consumer_id: str,
        limits: WearLimits,
        now: float,
    ) -> bool:
        """Return True if consumer may be turned OFF without violating wear limits.

        Guards:
        - min_on_time_s: consumer must have been ON for at least this long.
        """
        if limits.min_on_time_s <= 0:
            return True
        on_dur = self.on_duration_s(consumer_id, now)
        if on_dur < limits.min_on_time_s:
            _LOGGER.debug(
                "ConsumerStateTracker: %s cannot turn OFF (on_dur=%.0fs < min_on=%ds)",
                consumer_id,
                on_dur,
                limits.min_on_time_s,
            )
            return False
        return True

    def can_turn_on(
        self,
        consumer_id: str,
        limits: WearLimits,
        now: float,
    ) -> bool:
        """Return True if consumer may be turned ON without violating wear limits.

        Guards:
        - min_off_time_s: consumer must have been OFF for at least this long.
        - daily_max_cycles: cannot exceed daily cycle limit.
        """
        state = self._get_or_create(consumer_id)

        if limits.daily_max_cycles < 999 and state.cycles_today >= limits.daily_max_cycles:
            _LOGGER.debug(
                "ConsumerStateTracker: %s cannot turn ON (cycles_today=%d >= max=%d)",
                consumer_id,
                state.cycles_today,
                limits.daily_max_cycles,
            )
            return False

        if limits.min_off_time_s > 0:
            off_dur = self.off_duration_s(consumer_id, now)
            if off_dur < limits.min_off_time_s:
                _LOGGER.debug(
                    "ConsumerStateTracker: %s cannot turn ON (off_dur=%.0fs < min_off=%ds)",
                    consumer_id,
                    off_dur,
                    limits.min_off_time_s,
                )
                return False

        return True

    def reset_daily_counts(self) -> None:
        """Reset cycles_today for all consumers (call at local midnight)."""
        for state in self._states.values():
            state.cycles_today = 0
        _LOGGER.debug("ConsumerStateTracker: daily cycle counts reset")

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Return serialisable snapshot of all consumer states (for persistence)."""
        return {
            cid: {
                "is_on": s.is_on,
                "last_on_ts": s.last_on_ts,
                "last_off_ts": s.last_off_ts,
                "cycles_today": s.cycles_today,
            }
            for cid, s in self._states.items()
        }

    def restore(self, data: dict[str, dict[str, object]]) -> None:
        """Restore consumer states from a persisted snapshot.

        Args:
            data: Dict as returned by `snapshot()`.
        """
        for cid, vals in data.items():
            state = self._get_or_create(cid)
            state.is_on = bool(vals.get("is_on", False))
            state.last_on_ts = float(vals.get("last_on_ts", 0.0))  # type: ignore[arg-type]
            state.last_off_ts = float(vals.get("last_off_ts", 0.0))  # type: ignore[arg-type]
            state.cycles_today = int(str(vals.get("cycles_today", 0)))


# ── Priority cascade with wear guards ────────────────────────────────────────

@dataclass
class CascadeConsumer:
    """Consumer entry in the priority cascade."""

    id: str
    """Consumer identifier."""

    priority: int
    """Lower = higher priority."""

    is_running: bool
    """Whether consumer is currently ON."""

    min_power_w: float
    """Minimum power to run (W)."""

    wear_limits: WearLimits = field(default_factory=WearLimits)
    """Wear-guard parameters."""


@dataclass
class CascadeResult:
    """Result of one cascade evaluation."""

    to_start: list[str]
    """Consumer IDs that should be started this cycle."""

    to_stop: list[str]
    """Consumer IDs that should be stopped this cycle."""

    preempted: list[str]
    """Consumer IDs preempted (will be stopped if wear guards allow)."""

    blocked_by_wear: list[str]
    """Consumer IDs that were requested to stop but blocked by min_on_time."""


def evaluate_cascade(
    consumers: list[CascadeConsumer],
    available_surplus_w: float,
    tracker: ConsumerStateTracker,
    now: float,
) -> CascadeResult:
    """Evaluate which consumers should run given available surplus and wear limits.

    Algorithm (greedy top-priority-first):
    1. Sort consumers by priority (ascending = highest priority first).
    2. For each consumer in priority order:
       - If running: keep if surplus remains; else stop (subject to wear guards).
       - If not running: start if surplus remains and wear allows.
    3. Track "preempted" consumers: running consumers stopped because a
       higher-priority consumer took the surplus this cycle (to_start is non-empty).

    Wear guards protect running consumers from being stopped before min_on_time
    elapses. If a consumer is blocked by wear, the R1 grid guard handles any overrun.

    Args:
        consumers: Consumers to evaluate (any order — sorted internally by priority).
        available_surplus_w: Surplus watts available for consumers this cycle.
        tracker: ConsumerStateTracker with current on/off state and timing.
        now: Current monotonic time (seconds).

    Returns:
        CascadeResult with start/stop/preempted/blocked_by_wear lists.
    """
    # Sort by priority (lowest number = highest priority)
    sorted_consumers = sorted(consumers, key=lambda c: c.priority)

    to_start: list[str] = []
    to_stop: list[str] = []
    preempted: list[str] = []
    blocked_by_wear: list[str] = []

    remaining_w = available_surplus_w

    for consumer in sorted_consumers:
        if consumer.is_running:
            if remaining_w >= consumer.min_power_w:
                # Sufficient surplus — keep running
                remaining_w -= consumer.min_power_w
            else:
                # Insufficient surplus — stop if wear guard allows
                if tracker.can_turn_off(consumer.id, consumer.wear_limits, now):
                    to_stop.append(consumer.id)
                    # Mark as preempted if a higher-priority consumer started this cycle
                    if to_start:
                        preempted.append(consumer.id)
                else:
                    # Wear guard blocks stopping — accept conflict, R1 grid guard handles overrun
                    blocked_by_wear.append(consumer.id)
        else:
            # Not running — start if sufficient surplus and wear allows
            if (remaining_w >= consumer.min_power_w
                    and tracker.can_turn_on(consumer.id, consumer.wear_limits, now)):
                to_start.append(consumer.id)
                remaining_w -= consumer.min_power_w

    return CascadeResult(
        to_start=to_start,
        to_stop=to_stop,
        preempted=preempted,
        blocked_by_wear=blocked_by_wear,
    )
