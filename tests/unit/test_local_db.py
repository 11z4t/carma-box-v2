"""Tests for SQLite Local Database.

Covers:
- Write + read cycle log
- Write + read event log
- Write + read audit log
- State persistence (save + load)
- Retention cleanup
- Sync tracking
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from storage.local_db import (
    AuditLogEntry,
    CycleLogEntry,
    EventLogEntry,
    LocalDB,
)


@pytest_asyncio.fixture()
async def db(tmp_path: object) -> LocalDB:  # type: ignore[misc]
    """Create a temp database for testing."""
    from pathlib import Path

    path = Path(str(tmp_path)) / "test.db"
    local_db = LocalDB(str(path))
    await local_db.initialize()
    yield local_db
    await local_db.close()


# ===========================================================================
# Cycle log
# ===========================================================================


@pytest.mark.asyncio
class TestCycleLog:
    """Write + read cycle log entries."""

    async def test_write_and_read(self, db: LocalDB) -> None:
        entry = CycleLogEntry(
            cycle_id="abc123",
            timestamp="2026-04-12T22:00:00",
            scenario="MIDDAY_CHARGE",
            guard_level="ok",
            headroom_kw=1.5,
            elapsed_s=0.05,
        )
        await db.write_cycle(entry)

        rows = await db.get_unsynced_rows("cycle_log")
        assert len(rows) == 1
        assert rows[0]["cycle_id"] == "abc123"
        assert rows[0]["scenario"] == "MIDDAY_CHARGE"


# ===========================================================================
# Event log
# ===========================================================================


@pytest.mark.asyncio
class TestEventLog:
    """Write + read event log entries."""

    async def test_write_and_read(self, db: LocalDB) -> None:
        entry = EventLogEntry(
            timestamp="2026-04-12T22:00:00",
            event_type="scenario_transition",
            source="state_machine",
            message="S3 → S4",
        )
        await db.write_event(entry)

        rows = await db.get_unsynced_rows("event_log")
        assert len(rows) == 1
        assert rows[0]["event_type"] == "scenario_transition"


# ===========================================================================
# Audit log
# ===========================================================================


@pytest.mark.asyncio
class TestAuditLog:
    """Write + read audit log entries."""

    async def test_write_success(self, db: LocalDB) -> None:
        entry = AuditLogEntry(
            timestamp="2026-04-12T22:00:00",
            command_type="set_ems_mode",
            target_id="kontor",
            value="discharge_pv",
            rule_id="S4",
            reason="Evening discharge",
            success=True,
        )
        await db.write_audit(entry)

        rows = await db.get_unsynced_rows("audit_log")
        assert len(rows) == 1
        assert rows[0]["success"] == 1
        assert rows[0]["command_type"] == "set_ems_mode"

    async def test_write_failure(self, db: LocalDB) -> None:
        entry = AuditLogEntry(
            timestamp="2026-04-12T22:00:00",
            command_type="set_ems_mode",
            target_id="kontor",
            success=False,
            error="timeout",
        )
        await db.write_audit(entry)

        rows = await db.get_unsynced_rows("audit_log")
        assert len(rows) == 1
        assert rows[0]["success"] == 0
        assert rows[0]["error"] == "timeout"


# ===========================================================================
# State persistence
# ===========================================================================


@pytest.mark.asyncio
class TestStatePersistence:
    """State key-value store survives across operations."""

    async def test_save_and_load(self, db: LocalDB) -> None:
        await db.save_state("last_scenario", "EVENING_DISCHARGE")
        result = await db.load_state("last_scenario")
        assert result == "EVENING_DISCHARGE"

    async def test_load_missing_returns_none(self, db: LocalDB) -> None:
        result = await db.load_state("nonexistent")
        assert result is None

    async def test_upsert_overwrites(self, db: LocalDB) -> None:
        await db.save_state("key1", "value1")
        await db.save_state("key1", "value2")
        result = await db.load_state("key1")
        assert result == "value2"


# ===========================================================================
# Sync tracking
# ===========================================================================


@pytest.mark.asyncio
class TestSyncTracking:
    """get_unsynced_rows + mark_synced."""

    async def test_mark_synced(self, db: LocalDB) -> None:
        for i in range(3):
            await db.write_cycle(CycleLogEntry(
                cycle_id=f"c{i}",
                timestamp=f"2026-04-12T22:0{i}:00",
                scenario="MIDDAY_CHARGE",
                guard_level="ok",
                headroom_kw=1.0,
                elapsed_s=0.05,
            ))

        # All unsynced
        rows = await db.get_unsynced_rows("cycle_log")
        assert len(rows) == 3

        # Mark first 2 as synced
        await db.mark_synced("cycle_log", last_id=2)

        # Only 1 unsynced
        rows = await db.get_unsynced_rows("cycle_log")
        assert len(rows) == 1
        assert rows[0]["cycle_id"] == "c2"
