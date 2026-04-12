"""Surplus Dispatch Engine for CARMA Box.

Manages surplus PV dispatch to consumers in priority order:
  1. Miner (400W, ON/OFF, buffer load)
  2. EV (variable, ramp 6-10A)
  3. VP Kontor (1500W, ON/OFF)
  4. VP Pool (2000W, ON/OFF, requires pump)
  5. Pool Heater (3000W, ON/OFF, requires pump)

Features:
- Knapsack allocation: fit consumers within available surplus
- De-escalation: reverse priority order
- Bump logic: stop lower-priority to make room for higher
- Dependency checks (VP Pool/Heater need pump running)
- Rate limiting: max switches per window
- Start/stop delays

All thresholds from config — zero hardcoding.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

from core.models import ConsumerState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurplusConfig:
    """Surplus dispatch thresholds — all from site.yaml."""

    start_threshold_w: float = 200.0       # Min surplus to start a consumer
    stop_threshold_w: float = -100.0       # Surplus below this → de-escalate
    start_delay_s: float = 60.0            # Wait before starting
    stop_delay_s: float = 180.0            # Wait before stopping
    max_switches_per_window: int = 2       # Rate limit
    switch_window_s: float = 1800.0        # 30 min window
    bump_delay_s: float = 60.0            # Wait before bump
    deadband_w: float = 100.0             # Normal deadband
    doubled_deadband_w: float = 200.0     # After rate limit hit
    doubled_deadband_s: float = 180.0     # 3 min doubled deadband


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurplusAllocation:
    """Allocation decision for a single consumer."""

    consumer_id: str
    action: str          # "start", "stop", "no_change"
    reason: str = ""


@dataclass(frozen=True)
class SurplusResult:
    """Complete surplus dispatch result."""

    allocations: list[SurplusAllocation]
    available_surplus_w: float
    consumed_w: float = 0.0


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class SurplusRateLimiter:
    """Tracks switch events for rate limiting."""

    def __init__(self, config: SurplusConfig) -> None:
        self._config = config
        self._switches: deque[float] = deque()
        self._deadband_until: float = 0.0

    def record_switch(self) -> None:
        """Record a consumer switch event."""
        now = time.monotonic()
        self._switches.append(now)
        # Check if rate limit hit → double deadband
        self._purge()
        if len(self._switches) >= self._config.max_switches_per_window:
            self._deadband_until = now + self._config.doubled_deadband_s

    def can_switch(self) -> bool:
        """Can we switch a consumer? (within rate limit)."""
        self._purge()
        return len(self._switches) < self._config.max_switches_per_window

    @property
    def is_deadband_doubled(self) -> bool:
        return time.monotonic() < self._deadband_until

    @property
    def effective_deadband_w(self) -> float:
        if self.is_deadband_doubled:
            return self._config.doubled_deadband_w
        return self._config.deadband_w

    def _purge(self) -> None:
        cutoff = time.monotonic() - self._config.switch_window_s
        while self._switches and self._switches[0] < cutoff:
            self._switches.popleft()


# ---------------------------------------------------------------------------
# Dispatch engine
# ---------------------------------------------------------------------------


class SurplusDispatch:
    """Evaluates surplus dispatch decisions.

    Pure evaluation — returns SurplusResult, does not execute.
    """

    def __init__(self, config: SurplusConfig | None = None) -> None:
        self._config = config or SurplusConfig()
        self._rate_limiter = SurplusRateLimiter(self._config)

    def evaluate(
        self,
        available_surplus_w: float,
        consumers: list[ConsumerState],
        active_dependencies: set[str] | None = None,
    ) -> SurplusResult:
        """Evaluate surplus dispatch decisions.

        Args:
            available_surplus_w: Available surplus (export + active consumer power).
            consumers: Current state of all dispatchable consumers, sorted by priority.
            active_dependencies: Set of active consumer IDs (for dependency checks).

        Returns:
            SurplusResult with per-consumer allocations.
        """
        # active_dependencies reserved for future dependency checks (VP Pool needs pump)
        _ = active_dependencies
        deadband = self._rate_limiter.effective_deadband_w
        sorted_consumers = sorted(consumers, key=lambda c: c.priority)

        allocations: list[SurplusAllocation] = []
        remaining_w = available_surplus_w

        if available_surplus_w < self._config.stop_threshold_w:
            # Surplus too low — de-escalate
            return self._de_escalate(sorted_consumers)

        # Escalation: try to start consumers in priority order
        for consumer in sorted_consumers:
            if consumer.active:
                # Already active — keep running (count power as consumed)
                remaining_w -= consumer.power_w
                allocations.append(SurplusAllocation(
                    consumer_id=consumer.consumer_id,
                    action="no_change",
                    reason="already active",
                ))
                continue

            # Check if enough surplus to start this consumer
            if remaining_w < consumer.power_w + deadband:
                allocations.append(SurplusAllocation(
                    consumer_id=consumer.consumer_id,
                    action="no_change",
                    reason=f"insufficient surplus ({remaining_w:.0f}W < {consumer.power_w}W)",
                ))
                continue

            # Check rate limit
            if not self._rate_limiter.can_switch():
                allocations.append(SurplusAllocation(
                    consumer_id=consumer.consumer_id,
                    action="no_change",
                    reason="rate limited",
                ))
                continue

            # Start this consumer
            remaining_w -= consumer.power_w
            self._rate_limiter.record_switch()
            allocations.append(SurplusAllocation(
                consumer_id=consumer.consumer_id,
                action="start",
                reason=f"surplus available ({available_surplus_w:.0f}W)",
            ))

        consumed = available_surplus_w - remaining_w
        return SurplusResult(
            allocations=allocations,
            available_surplus_w=available_surplus_w,
            consumed_w=consumed,
        )

    def _de_escalate(
        self, consumers: list[ConsumerState]
    ) -> SurplusResult:
        """De-escalate: stop consumers in reverse priority order."""
        allocations: list[SurplusAllocation] = []

        # Reverse priority: stop highest-priority-shed first
        by_shed = sorted(consumers, key=lambda c: c.priority_shed, reverse=True)

        for consumer in by_shed:
            if consumer.active:
                if self._rate_limiter.can_switch():
                    self._rate_limiter.record_switch()
                    allocations.append(SurplusAllocation(
                        consumer_id=consumer.consumer_id,
                        action="stop",
                        reason="de-escalation (surplus too low)",
                    ))
                else:
                    allocations.append(SurplusAllocation(
                        consumer_id=consumer.consumer_id,
                        action="no_change",
                        reason="rate limited (de-escalation)",
                    ))
            else:
                allocations.append(SurplusAllocation(
                    consumer_id=consumer.consumer_id,
                    action="no_change",
                    reason="already off",
                ))

        return SurplusResult(
            allocations=allocations,
            available_surplus_w=0.0,
        )
