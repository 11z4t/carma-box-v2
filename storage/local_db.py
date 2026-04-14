"""SQLite Local Database for CARMA Box.

Stores cycle logs, event logs, audit logs, state persistence,
and energy session logs (battery/EV/PV).
Uses aiosqlite for async operations.

Tables:
- cycle_log: one row per 30s control cycle
- event_log: scenario transitions, EV events, guard triggers
- audit_log: every command sent to hardware
- state: key-value persistence (survives restart)
- battery_session: charge/discharge sessions per battery (PLAT-1534)
- ev_session: EV charging sessions (PLAT-1534)
- pv_daily_summary: daily PV production totals (PLAT-1534)

Auto-created on first run. Retention cleanup configurable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

# Allowlist of valid table names — guards against SQL injection in dynamic queries
ALLOWED_TABLES: frozenset[str] = frozenset(
    {
        "cycle_log",
        "event_log",
        "audit_log",
        "state",
        "battery_session",
        "ev_session",
        "pv_daily_summary",
    }
)


def _validate_table(table: str) -> str:
    """Validate table name against ALLOWED_TABLES allowlist.

    Raises ValueError if the name is not in the allowlist.
    """
    if table not in ALLOWED_TABLES:
        raise ValueError(f"Invalid table name '{table}'. Must be one of: {sorted(ALLOWED_TABLES)}")
    return table


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

# Increment when DDL changes require a migration.
_SCHEMA_VERSION = 2


async def _check_schema_version(db: aiosqlite.Connection) -> None:
    """Ensure schema_version table exists and version matches.

    Raises RuntimeError if the on-disk version is newer than this code.
    Future migrations can be added here as elif blocks.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    cursor = await db.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    row = await cursor.fetchone()
    on_disk = int(row[0]) if row else 0

    if on_disk == _SCHEMA_VERSION:
        return  # Up to date
    if on_disk > _SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {on_disk} is newer than code version "
            f"{_SCHEMA_VERSION} — upgrade the application."
        )
    # on_disk < _SCHEMA_VERSION: apply missing migrations
    now = datetime.now(tz=timezone.utc).isoformat()
    # Migration 0 → 1: initial schema (tables created by _DDL above)
    # Migration 1 → 2: energy session tables (battery_session, ev_session, pv_daily_summary)
    if on_disk < 2:  # noqa: PLR2004
        await db.executescript(_DDL_V2)
    await db.execute(
        "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
        (_SCHEMA_VERSION, now),
    )
    logger.info("Schema migrated to version %d", _SCHEMA_VERSION)


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

# DDL for schema version 2: energy session tables (PLAT-1534)
_DDL_V2 = """
CREATE TABLE IF NOT EXISTS battery_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT NOT NULL,
    battery_id TEXT NOT NULL,
    session_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL DEFAULT '',
    duration_s REAL NOT NULL DEFAULT 0.0,
    energy_kwh REAL NOT NULL DEFAULT 0.0,
    source TEXT NOT NULL,
    avg_power_w REAL NOT NULL DEFAULT 0.0,
    synced INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ev_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL DEFAULT '',
    duration_s REAL NOT NULL DEFAULT 0.0,
    energy_kwh REAL NOT NULL DEFAULT 0.0,
    soc_start_pct REAL NOT NULL DEFAULT 0.0,
    soc_end_pct REAL NOT NULL DEFAULT 0.0,
    avg_current_a REAL NOT NULL DEFAULT 0.0,
    synced INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pv_daily_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT NOT NULL,
    date TEXT NOT NULL,
    pv_kwh_total REAL NOT NULL,
    pv_kwh_kontor REAL NOT NULL,
    pv_kwh_forrad REAL NOT NULL,
    created_at TEXT NOT NULL,
    synced INTEGER NOT NULL DEFAULT 0,
    UNIQUE(site_id, date)
);

CREATE INDEX IF NOT EXISTS idx_battery_session_synced ON battery_session(synced);
CREATE INDEX IF NOT EXISTS idx_ev_session_synced ON ev_session(synced);
CREATE INDEX IF NOT EXISTS idx_pv_daily_summary_synced ON pv_daily_summary(synced);
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
# Energy session data models (PLAT-1534)
# ---------------------------------------------------------------------------

# Valid session_type values for battery_session
SESSION_TYPE_CHARGE = "charge"
SESSION_TYPE_DISCHARGE = "discharge"

# Valid source values for battery_session
SESSION_SOURCE_PV = "pv"
SESSION_SOURCE_GRID = "grid"
SESSION_SOURCE_MIXED = "mixed"


@dataclass(frozen=True)
class BatterySessionEntry:
    """One row in battery_session.

    Records a single charge or discharge session for one battery.
    """

    site_id: str
    battery_id: str
    session_type: str  # SESSION_TYPE_CHARGE or SESSION_TYPE_DISCHARGE
    started_at: str  # ISO-8601 UTC
    source: str  # SESSION_SOURCE_PV / GRID / MIXED
    ended_at: str = ""
    duration_s: float = 0.0
    energy_kwh: float = 0.0
    avg_power_w: float = 0.0


@dataclass(frozen=True)
class EVSessionEntry:
    """One row in ev_session.

    Records a single EV charging session.
    """

    site_id: str
    started_at: str  # ISO-8601 UTC
    soc_start_pct: float
    ended_at: str = ""
    duration_s: float = 0.0
    energy_kwh: float = 0.0
    soc_end_pct: float = 0.0
    avg_current_a: float = 0.0


@dataclass(frozen=True)
class PVDailySummaryEntry:
    """One row in pv_daily_summary.

    Daily aggregated PV production, split by CT placement.
    """

    site_id: str
    date: str  # YYYY-MM-DD
    pv_kwh_total: float
    pv_kwh_kontor: float
    pv_kwh_forrad: float
    created_at: str  # ISO-8601 UTC


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
        # WAL mode: allows concurrent readers + one writer without blocking,
        # reduces contention in the 30-second cycle loop.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_DDL)
        await _check_schema_version(self._db)
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

    async def write_cycle(self, entry: CycleLogEntry, *, commit: bool = True) -> None:
        """Write a cycle log entry.

        Args:
            entry: The cycle log row to insert.
            commit: If False, skip the immediate commit (use for batch writes).
                    Caller must call commit() manually when batching.
        """
        db = await self._ensure_db()
        await db.execute(
            "INSERT INTO cycle_log (cycle_id, timestamp, scenario, guard_level, "
            "headroom_kw, elapsed_s, violations) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.cycle_id,
                entry.timestamp,
                entry.scenario,
                entry.guard_level,
                entry.headroom_kw,
                entry.elapsed_s,
                entry.violations,
            ),
        )
        if commit:
            await db.commit()

    async def write_event(self, entry: EventLogEntry, *, commit: bool = True) -> None:
        """Write an event log entry.

        Args:
            entry: The event log row to insert.
            commit: If False, skip the immediate commit (use for batch writes).
        """
        db = await self._ensure_db()
        await db.execute(
            "INSERT INTO event_log (timestamp, event_type, source, message, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (entry.timestamp, entry.event_type, entry.source, entry.message, entry.data),
        )
        if commit:
            await db.commit()

    async def write_audit(self, entry: AuditLogEntry, *, commit: bool = True) -> None:
        """Write an audit log entry.

        Args:
            entry: The audit log row to insert.
            commit: If False, skip the immediate commit (use for batch writes).
        """
        db = await self._ensure_db()
        await db.execute(
            "INSERT INTO audit_log (timestamp, command_type, target_id, value, "
            "rule_id, reason, success, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.timestamp,
                entry.command_type,
                entry.target_id,
                entry.value,
                entry.rule_id,
                entry.reason,
                int(entry.success),
                entry.error,
            ),
        )
        if commit:
            await db.commit()

    async def write_battery_session(
        self, entry: BatterySessionEntry, *, commit: bool = True
    ) -> None:
        """Write a battery session entry (PLAT-1534).

        Args:
            entry: The battery session row to insert.
            commit: If False, skip the immediate commit (use for batch writes).
        """
        db = await self._ensure_db()
        await db.execute(
            "INSERT INTO battery_session "
            "(site_id, battery_id, session_type, started_at, ended_at, "
            "duration_s, energy_kwh, source, avg_power_w) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.site_id,
                entry.battery_id,
                entry.session_type,
                entry.started_at,
                entry.ended_at,
                entry.duration_s,
                entry.energy_kwh,
                entry.source,
                entry.avg_power_w,
            ),
        )
        if commit:
            await db.commit()

    async def write_ev_session(self, entry: EVSessionEntry, *, commit: bool = True) -> None:
        """Write an EV session entry (PLAT-1534).

        Args:
            entry: The EV session row to insert.
            commit: If False, skip the immediate commit (use for batch writes).
        """
        db = await self._ensure_db()
        await db.execute(
            "INSERT INTO ev_session "
            "(site_id, started_at, ended_at, duration_s, energy_kwh, "
            "soc_start_pct, soc_end_pct, avg_current_a) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.site_id,
                entry.started_at,
                entry.ended_at,
                entry.duration_s,
                entry.energy_kwh,
                entry.soc_start_pct,
                entry.soc_end_pct,
                entry.avg_current_a,
            ),
        )
        if commit:
            await db.commit()

    async def upsert_pv_daily_summary(
        self, entry: PVDailySummaryEntry, *, commit: bool = True
    ) -> None:
        """Upsert a PV daily summary entry (PLAT-1534).

        Uses INSERT OR REPLACE so the same (site_id, date) pair is idempotent.

        Args:
            entry: The PV daily summary row to upsert.
            commit: If False, skip the immediate commit (use for batch writes).
        """
        db = await self._ensure_db()
        await db.execute(
            "INSERT OR REPLACE INTO pv_daily_summary "
            "(site_id, date, pv_kwh_total, pv_kwh_kontor, pv_kwh_forrad, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                entry.site_id,
                entry.date,
                entry.pv_kwh_total,
                entry.pv_kwh_kontor,
                entry.pv_kwh_forrad,
                entry.created_at,
            ),
        )
        if commit:
            await db.commit()

    async def commit(self) -> None:
        """Explicitly commit pending writes (for batch operations).

        Use when calling write_* methods with commit=False to accumulate
        multiple inserts in a single transaction for efficiency.
        """
        db = await self._ensure_db()
        await db.commit()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    async def save_state(self, key: str, value: str) -> None:
        """Save key-value state (upsert)."""
        db = await self._ensure_db()
        now = datetime.now(tz=timezone.utc).isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )
        await db.commit()

    async def load_state(self, key: str) -> Optional[str]:
        """Load state by key. Returns None if not found."""
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT value FROM state WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Retention cleanup
    # ------------------------------------------------------------------

    async def cleanup_retention(self, days: int) -> int:
        """Delete rows older than specified days. Returns count deleted.

        PLAT-1352: days is validated as int to prevent SQL injection.
        SQLite's datetime() function only accepts integer day offsets, so
        we pass the value as a bound parameter using string concatenation
        inside the SQL expression — safe because days is enforced as int.
        """
        if not isinstance(days, int) or days < 0:
            raise ValueError(f"days must be a non-negative integer, got {days!r}")
        db = await self._ensure_db()
        total = 0
        for table in ("cycle_log", "event_log", "audit_log"):
            _validate_table(table)  # Always valid — just enforcing the pattern
            # Use parameterized query: '-' || cast(? as text) || ' days'
            # avoids f-string interpolation of user input into SQL.
            cursor = await db.execute(
                f"DELETE FROM {table} WHERE timestamp < "  # noqa: S608
                "datetime('now', '-' || cast(? as text) || ' days')",
                (days,),
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
        _validate_table(table)
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
        _validate_table(table)
        db = await self._ensure_db()
        await db.execute(
            f"UPDATE {table} SET synced = 1 WHERE id <= ? AND synced = 0",  # noqa: S608
            (last_id,),
        )
        await db.commit()
