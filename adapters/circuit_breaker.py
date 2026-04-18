"""Circuit breaker for adapter calls (PLAT-1707).

Protects HA REST + inverter writes from noisy repeated failures.
Classic 3-state breaker:

  CLOSED     normal — every call is tried
  OPEN       too many consecutive failures — fail fast without trying
  HALF_OPEN  cooldown expired — allow ONE probe; success → CLOSED,
             failure → OPEN with fresh cooldown

Pure module — stateful per instance, no global state, deterministic
given the same call sequence. Time source is injectable for tests.
"""

from __future__ import annotations

import time as _time_mod
from dataclasses import dataclass, field
from typing import Callable, Literal

BreakerState = Literal["closed", "open", "half_open"]


@dataclass
class CircuitBreakerConfig:
    """Tunable thresholds — every knob from this config, no magic numbers."""

    failure_threshold: int = 5          # consecutive failures → OPEN
    cooldown_s: float = 30.0            # OPEN dwell before HALF_OPEN probe
    success_threshold: int = 1          # HALF_OPEN successes → CLOSED


@dataclass
class CircuitBreaker:
    """A single protected call-path.

    Usage::

        cb = CircuitBreaker("goodwe.kontor.set_ems_mode")
        if not cb.allow():
            return False                # short-circuit — fail fast
        try:
            ok = await inverter.set_ems_mode(mode)
        except Exception:
            cb.on_failure()
            raise
        if ok:
            cb.on_success()
        else:
            cb.on_failure()
        return ok
    """

    name: str
    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    # Injectable monotonic clock for deterministic tests.
    clock: Callable[[], float] = field(default=_time_mod.monotonic)

    state: BreakerState = "closed"
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    opened_at: float = 0.0

    def allow(self) -> bool:
        """Is the next call permitted?

        - CLOSED → always allow.
        - OPEN → allow only if cooldown has elapsed (caller-visible
          transition to HALF_OPEN happens here).
        - HALF_OPEN → allow a single probe at a time; caller is expected
          to call on_success/on_failure before the next allow().
        """
        if self.state == "closed":
            return True
        if self.state == "open":
            if self.clock() - self.opened_at >= self.config.cooldown_s:
                self.state = "half_open"
                self.consecutive_successes = 0
                return True
            return False
        # half_open — one probe at a time
        return True

    def on_success(self) -> None:
        """Report a successful call."""
        if self.state == "half_open":
            self.consecutive_successes += 1
            if self.consecutive_successes >= self.config.success_threshold:
                self.state = "closed"
                self.consecutive_failures = 0
                self.consecutive_successes = 0
            return
        # closed: reset failure count; open: ignore (shouldn't happen)
        self.consecutive_failures = 0

    def on_failure(self) -> None:
        """Report a failed call (exception OR adapter returned False)."""
        if self.state == "half_open":
            # Probe failed → back to OPEN, reset cooldown.
            self.state = "open"
            self.opened_at = self.clock()
            self.consecutive_successes = 0
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.config.failure_threshold:
            self.state = "open"
            self.opened_at = self.clock()

    def snapshot(self) -> dict[str, object]:
        """Introspection helper for metrics / diagnostics."""
        return {
            "name": self.name,
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "opened_at": self.opened_at,
        }
