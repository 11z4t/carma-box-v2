"""Per-cycle decision audit log for CARMA Box.

PLAT-1381: Every control cycle produces a structured decision record.
Records are logged and optionally persisted to SQLite.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Optional

from core.models import ModelEncoder, Scenario

# Seconds-to-milliseconds conversion.
_MS_PER_S: int = 1000

logger = logging.getLogger(__name__)


@dataclass
class DecisionRecord:
    """One cycle's complete decision chain."""

    cycle_id: str
    timestamp: str
    elapsed_ms: int
    scenario: str
    guard_level: str
    guard_commands: list[str]
    balance_total_w: Optional[int]
    commands_succeeded: int
    commands_failed: int
    error: Optional[str]

    def to_json(self) -> str:
        """Serialize to compact JSON."""
        return json.dumps(
            asdict(self), cls=ModelEncoder, separators=(",", ":"),
        )


@dataclass(frozen=True)
class DecisionTrace:
    """Per-cycle trace capturing the full reasoning chain.

    Complements DecisionRecord with suppressed commands, degraded modes,
    and guard reasoning — for post-mortem analysis and audit.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0"

    cycle_id: str
    timestamp: datetime
    scenario: str
    active_guard_level: str
    guard_reason: str
    plan_used: str
    commands_sent: list[str] = field(default_factory=list)
    commands_suppressed: list[str] = field(default_factory=list)
    degraded_modes_active: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-safe dict with schema version."""
        return {
            "schema_version": self.SCHEMA_VERSION,
            "cycle_id": self.cycle_id,
            "timestamp": self.timestamp.isoformat(),
            "scenario": self.scenario,
            "active_guard_level": self.active_guard_level,
            "guard_reason": self.guard_reason,
            "plan_used": self.plan_used,
            "commands_sent": list(self.commands_sent),
            "commands_suppressed": list(self.commands_suppressed),
            "degraded_modes_active": list(self.degraded_modes_active),
        }


class DecisionLog:
    """Structured decision logger — one record per cycle."""

    def __init__(
        self,
        persist_callback: Optional[Any] = None,
    ) -> None:
        self._persist = persist_callback
        self._cycle_count = 0

    def record(
        self,
        cycle_id: str,
        timestamp: datetime,
        elapsed_s: float,
        scenario: Scenario,
        guard_level: str = "ok",
        guard_commands: Optional[list[str]] = None,
        balance_total_w: Optional[int] = None,
        commands_succeeded: int = 0,
        commands_failed: int = 0,
        error: Optional[str] = None,
    ) -> DecisionRecord:
        """Create and log a decision record."""
        self._cycle_count += 1

        rec = DecisionRecord(
            cycle_id=cycle_id,
            timestamp=timestamp.isoformat(),
            elapsed_ms=int(elapsed_s * _MS_PER_S),
            scenario=scenario.value,
            guard_level=guard_level,
            guard_commands=guard_commands or [],
            balance_total_w=balance_total_w,
            commands_succeeded=commands_succeeded,
            commands_failed=commands_failed,
            error=error,
        )

        logger.info(
            "DECISION cycle=%s scenario=%s guard=%s "
            "cmds=%d/%d elapsed=%dms",
            cycle_id, scenario.value, guard_level,
            commands_succeeded,
            commands_succeeded + commands_failed,
            rec.elapsed_ms,
        )

        if self._persist is not None:
            try:
                self._persist(rec)
            except Exception as exc:
                logger.error("Decision persist failed: %s", exc)

        return rec

    @property
    def cycle_count(self) -> int:
        return self._cycle_count
