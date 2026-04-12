"""SQLite Local Database for CARMA Box.

Stores cycle logs, event logs, audit logs, and state persistence.
Uses aiosqlite for async operations.

Tables:
- cycle_log: one row per 30s control cycle
- event_log: scenario transitions, EV events, guard triggers
- audit_log: every command sent to hardware
- state: key-value persistence (survives restart)

Auto-created on first run. Retention cleanup configurable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS cycle_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    scenario TEXT NOT NULL,
    guard_level TEXT NOT NULL,
    headroom_kw REAL NOT NULL,
    elapsed_s REAL NOT NULL,
    violations TEXT,
    synced INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT,
    synced INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    command_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    value TEXT,
    rule_id TEXT,
    reason TEXT,
    success INTEGER NOT NULL,
    error TEXT,
    synced INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cycle_log_synced ON cycle_log(synced);
CREATE INDEX IF NOT EXISTS idx_event_log_synced ON event_log(synced);
CREATE INDEX IF NOT EXISTS idx_audit_log_synced ON audit_log(synced);
CREATE INDEX IF NOT EXISTS idx_cycle_log_timestamp ON cycle_log(timestamp);
"""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CycleLogEntry:
    """One row in cycle_log."""

    cycle_id: str
    timestamp: str
    scenario: str
    guard_level: str
    headroom_kw: float
    elapsed_s: float
    violations: str = ""


@dataclass(frozen=True)
class EventLogEntry:
    """One row in event_log."""

    timestamp: str
    event_type: str
    source: str
    message: str
    data: str = ""


@dataclass(frozen=True)
class AuditLogEntry:
    """One row in audit_log."""

    timestamp: str
    command_type: str
    target_id: str
    value: str = ""
    rule_id: str = ""
    reason: str = ""
    success: bool = True
    error: str = ""


# ---------------------------------------------------------------------------
# LocalDB
# ---------------------------------------------------------------------------


class LocalDB:
    """Async SQLite database for local storage.

    Auto-creates tables on initialize(). All writes are async.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Create/open database and ensure tables exist."""
        self._db = await aiosqlite.connect(self._path)
        await self._db.executescript(_DDL)
        await self._db.commit()
        logger.info("LocalDB initialized at %s", self._path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("LocalDB not initialized — call initialize() first")
        return self._db

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    async def write_cycle(self, entry: CycleLogEntry) -> None:
        """Write a cycle log entry."""
        db = await self._ensure_db()
        await db.execute(
            "INSERT INTO cycle_log (cycle_id, timestamp, scenario, guard_level, "
            "headroom_kw, elapsed_s, violations) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entry.cycle_id, entry.timestamp, entry.scenario,
             entry.guard_level, entry.headroom_kw, entry.elapsed_s, entry.violations),
        )
        await db.commit()

    async def write_event(self, entry: EventLogEntry) -> None:
        """Write an event log entry."""
        db = await self._ensure_db()
        await db.execute(
            "INSERT INTO event_log (timestamp, event_type, source, message, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (entry.timestamp, entry.event_type, entry.source, entry.message, entry.data),
        )
        await db.commit()

    async def write_audit(self, entry: AuditLogEntry) -> None:
        """Write an audit log entry."""
        db = await self._ensure_db()
        await db.execute(
            "INSERT INTO audit_log (timestamp, command_type, target_id, value, "
            "rule_id, reason, success, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.timestamp, entry.command_type, entry.target_id, entry.value,
             entry.rule_id, entry.reason, int(entry.success), entry.error),
        )
        await db.commit()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    async def save_state(self, key: str, value: str) -> None:
        """Save key-value state (upsert)."""
        db = await self._ensure_db()
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )
        await db.commit()

    async def load_state(self, key: str) -> Optional[str]:
        """Load state by key. Returns None if not found."""
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT value FROM state WHERE key = ?", (key,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Retention cleanup
    # ------------------------------------------------------------------

    async def cleanup_retention(self, days: int) -> int:
        """Delete rows older than specified days. Returns count deleted."""
        db = await self._ensure_db()
        cutoff = f"datetime('now', '-{days} days')"
        total = 0
        for table in ("cycle_log", "event_log", "audit_log"):
            cursor = await db.execute(
                f"DELETE FROM {table} WHERE timestamp < {cutoff}",  # noqa: S608
            )
            total += cursor.rowcount
        await db.commit()
        logger.info("Retention cleanup: deleted %d rows older than %d days", total, days)
        return total

    async def vacuum(self) -> None:
        """Reclaim disk space after cleanup."""
        db = await self._ensure_db()
        await db.execute("VACUUM")
        logger.info("Database vacuum complete")

    # ------------------------------------------------------------------
    # Sync tracking (for PostgreSQL hub sync)
    # ------------------------------------------------------------------

    async def get_unsynced_rows(self, table: str, limit: int = 1000) -> list[dict[str, object]]:
        """Get unsynced rows for hub sync."""
        db = await self._ensure_db()
        cursor = await db.execute(
            f"SELECT * FROM {table} WHERE synced = 0 ORDER BY id LIMIT ?",  # noqa: S608
            (limit,),
        )
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = await cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]

    async def mark_synced(self, table: str, last_id: int) -> None:
        """Mark rows as synced up to last_id."""
        db = await self._ensure_db()
        await db.execute(
            f"UPDATE {table} SET synced = 1 WHERE id <= ? AND synced = 0",  # noqa: S608
            (last_id,),
        )
        await db.commit()
