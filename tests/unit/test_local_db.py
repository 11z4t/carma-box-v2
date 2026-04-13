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

import asyncio
from collections.abc import Generator
from pathlib import Path
from typing import Any, Coroutine

import pytest

from storage.local_db import (
    AuditLogEntry,
    CycleLogEntry,
    EventLogEntry,
    LocalDB,
)


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run async code in a new event loop (avoids fixture thread issues)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def db(tmp_path: Path) -> Generator[LocalDB, None, None]:
    """Create a temp database (sync fixture, async ops via _run)."""
    path = tmp_path / "test.db"
    local_db = LocalDB(str(path))
    _run(local_db.initialize())
    yield local_db
    _run(local_db.close())


# ===========================================================================
# Cycle log
# ===========================================================================


class TestCycleLog:
    """Write + read cycle log entries."""

    def test_write_and_read(self, db: LocalDB) -> None:
        entry = CycleLogEntry(
            cycle_id="abc123",
            timestamp="2026-04-12T22:00:00",
            scenario="MIDDAY_CHARGE",
            guard_level="ok",
            headroom_kw=1.5,
            elapsed_s=0.05,
        )
        _run(db.write_cycle(entry))

        rows = _run(db.get_unsynced_rows("cycle_log"))
        assert len(rows) == 1
        assert rows[0]["cycle_id"] == "abc123"
        assert rows[0]["scenario"] == "MIDDAY_CHARGE"


# ===========================================================================
# Event log
# ===========================================================================


class TestEventLog:
    """Write + read event log entries."""

    def test_write_and_read(self, db: LocalDB) -> None:
        entry = EventLogEntry(
            timestamp="2026-04-12T22:00:00",
            event_type="scenario_transition",
            source="state_machine",
            message="S3 → S4",
        )
        _run(db.write_event(entry))

        rows = _run(db.get_unsynced_rows("event_log"))
        assert len(rows) == 1
        assert rows[0]["event_type"] == "scenario_transition"


# ===========================================================================
# Audit log
# ===========================================================================


class TestAuditLog:
    """Write + read audit log entries."""

    def test_write_success(self, db: LocalDB) -> None:
        entry = AuditLogEntry(
            timestamp="2026-04-12T22:00:00",
            command_type="set_ems_mode",
            target_id="kontor",
            value="discharge_pv",
            rule_id="S4",
            reason="Evening discharge",
            success=True,
        )
        _run(db.write_audit(entry))

        rows = _run(db.get_unsynced_rows("audit_log"))
        assert len(rows) == 1
        assert rows[0]["success"] == 1
        assert rows[0]["command_type"] == "set_ems_mode"

    def test_write_failure(self, db: LocalDB) -> None:
        entry = AuditLogEntry(
            timestamp="2026-04-12T22:00:00",
            command_type="set_ems_mode",
            target_id="kontor",
            success=False,
            error="timeout",
        )
        _run(db.write_audit(entry))

        rows = _run(db.get_unsynced_rows("audit_log"))
        assert len(rows) == 1
        assert rows[0]["success"] == 0
        assert rows[0]["error"] == "timeout"


# ===========================================================================
# State persistence
# ===========================================================================


class TestStatePersistence:
    """State key-value store survives across operations."""

    def test_save_and_load(self, db: LocalDB) -> None:
        _run(db.save_state("last_scenario", "EVENING_DISCHARGE"))
        result = _run(db.load_state("last_scenario"))
        assert result == "EVENING_DISCHARGE"

    def test_load_missing_returns_none(self, db: LocalDB) -> None:
        result = _run(db.load_state("nonexistent"))
        assert result is None

    def test_upsert_overwrites(self, db: LocalDB) -> None:
        _run(db.save_state("key1", "value1"))
        _run(db.save_state("key1", "value2"))
        result = _run(db.load_state("key1"))
        assert result == "value2"


# ===========================================================================
# Sync tracking
# ===========================================================================


class TestSyncTracking:
    """get_unsynced_rows + mark_synced."""

    def test_mark_synced(self, db: LocalDB) -> None:
        for i in range(3):
            _run(db.write_cycle(CycleLogEntry(
                cycle_id=f"c{i}",
                timestamp=f"2026-04-12T22:0{i}:00",
                scenario="MIDDAY_CHARGE",
                guard_level="ok",
                headroom_kw=1.0,
                elapsed_s=0.05,
            )))

        # All unsynced
        rows = _run(db.get_unsynced_rows("cycle_log"))
        assert len(rows) == 3

        # Mark first 2 as synced
        _run(db.mark_synced("cycle_log", last_id=2))

        # Only 1 unsynced
        rows = _run(db.get_unsynced_rows("cycle_log"))
        assert len(rows) == 1
        assert rows[0]["cycle_id"] == "c2"


# ===========================================================================
# Coverage: uncovered branches
# ===========================================================================


class TestCoverageBranches:
    """Tests targeting specific uncovered code paths."""

    def test_ensure_db_raises_when_not_initialized(self, tmp_path: Path) -> None:
        """_ensure_db raises RuntimeError when called before initialize() (line 152)."""
        db = LocalDB(str(tmp_path / "uninit.db"))
        with pytest.raises(RuntimeError, match="not initialized"):
            _run(db._ensure_db())

    def test_cleanup_retention_deletes_old_rows(self, db: LocalDB) -> None:
        """cleanup_retention removes rows older than cutoff (lines 220-230)."""
        # Write a cycle entry with a very old timestamp
        entry = CycleLogEntry(
            cycle_id="old_cycle",
            timestamp="2020-01-01T00:00:00",
            scenario="MIDDAY_CHARGE",
            guard_level="ok",
            headroom_kw=1.0,
            elapsed_s=0.05,
        )
        _run(db.write_cycle(entry))

        rows_before = _run(db.get_unsynced_rows("cycle_log"))
        assert len(rows_before) == 1

        # Delete rows older than 1 day (2020 entry qualifies)
        deleted = _run(db.cleanup_retention(days=1))
        assert deleted >= 1

        rows_after = _run(db.get_unsynced_rows("cycle_log"))
        assert len(rows_after) == 0

    def test_vacuum_runs_without_error(self, db: LocalDB) -> None:
        """vacuum executes successfully on an initialized database (lines 234-236)."""
        # Should not raise
        _run(db.vacuum())


# ===========================================================================
# PLAT-1352: SQL injection prevention in cleanup_retention
# ===========================================================================


class TestSQLInjectionPrevention:
    """PLAT-1352: cleanup_retention must reject non-integer days parameters."""

    def test_cleanup_retention_rejects_negative_days(self, db: LocalDB) -> None:
        """Negative days value raises ValueError."""
        with pytest.raises(ValueError, match="non-negative integer"):
            _run(db.cleanup_retention(-1))

    def test_cleanup_retention_rejects_string_input(self, db: LocalDB) -> None:
        """String days value raises ValueError (SQL injection attempt)."""
        with pytest.raises((ValueError, TypeError)):
            _run(db.cleanup_retention("1 OR 1=1 --"))  # type: ignore[arg-type]

    def test_cleanup_retention_rejects_float_input(self, db: LocalDB) -> None:
        """Float days value raises ValueError (only int allowed)."""
        with pytest.raises(ValueError, match="non-negative integer"):
            _run(db.cleanup_retention(1.5))  # type: ignore[arg-type]

    def test_cleanup_retention_accepts_valid_int(self, db: LocalDB) -> None:
        """Valid integer days value works without raising."""
        # Write a row with old timestamp so it gets deleted
        _run(db.write_cycle(CycleLogEntry(
            cycle_id="old1", timestamp="2020-01-01T00:00:00",
            scenario="MIDDAY_CHARGE", guard_level="ok",
            headroom_kw=1.0, elapsed_s=0.05,
        )))
        deleted = _run(db.cleanup_retention(1))
        assert deleted >= 1

    def test_cleanup_retention_zero_days(self, db: LocalDB) -> None:
        """Zero days is valid — deletes rows older than now."""
        result = _run(db.cleanup_retention(0))
        assert isinstance(result, int)


# ===========================================================================
# PLAT-1365: New test gaps
# ===========================================================================


class TestBatchCommit:
    """Batch write (commit=False) defers the commit until explicit commit()."""

    def test_batch_write_cycle_deferred_commit(self, db: LocalDB) -> None:
        """write_cycle with commit=False doesn't persist until commit() called."""
        entry = CycleLogEntry(
            cycle_id="batch1",
            timestamp="2026-04-12T23:00:00",
            scenario="EVENING_DISCHARGE",
            guard_level="ok",
            headroom_kw=0.5,
            elapsed_s=0.03,
        )
        # Write without commit
        _run(db.write_cycle(entry, commit=False))
        _run(db.commit())
        # After explicit commit, the row must be visible
        rows = _run(db.get_unsynced_rows("cycle_log"))
        assert any(r["cycle_id"] == "batch1" for r in rows)

    def test_batch_write_multiple_entries_single_commit(self, db: LocalDB) -> None:
        """Multiple deferred writes committed in one transaction."""
        for i in range(3):
            _run(db.write_cycle(CycleLogEntry(
                cycle_id=f"batch_{i}",
                timestamp=f"2026-04-12T23:0{i}:00",
                scenario="MIDDAY_CHARGE",
                guard_level="ok",
                headroom_kw=1.0,
                elapsed_s=0.04,
            ), commit=False))
        _run(db.commit())
        rows = _run(db.get_unsynced_rows("cycle_log"))
        assert len(rows) == 3


class TestSQLInjectionCleanupRetention:
    """SQL injection attempts in cleanup_retention are rejected."""

    def test_sql_injection_string_days(self, db: LocalDB) -> None:
        """Malicious string as days raises TypeError/ValueError (not executed as SQL)."""
        with pytest.raises((ValueError, TypeError)):
            _run(db.cleanup_retention("1 OR 1=1; DROP TABLE cycle_log; --"))  # type: ignore[arg-type]

    def test_sql_injection_none_days(self, db: LocalDB) -> None:
        """None as days raises TypeError/ValueError."""
        with pytest.raises((ValueError, TypeError)):
            _run(db.cleanup_retention(None))  # type: ignore[arg-type]

    def test_sql_injection_float_days(self, db: LocalDB) -> None:
        """Float as days raises ValueError (int enforced)."""
        with pytest.raises(ValueError, match="non-negative integer"):
            _run(db.cleanup_retention(7.5))  # type: ignore[arg-type]

    def test_table_still_intact_after_injection_attempt(self, db: LocalDB) -> None:
        """cycle_log table is unaffected by a rejected injection attempt."""
        _run(db.write_cycle(CycleLogEntry(
            cycle_id="safe1", timestamp="2026-04-12T22:00:00",
            scenario="MIDDAY_CHARGE", guard_level="ok",
            headroom_kw=1.0, elapsed_s=0.05,
        )))
        with pytest.raises((ValueError, TypeError)):
            _run(db.cleanup_retention("9999' OR '1'='1"))  # type: ignore[arg-type]
        # Table should still have the row
        rows = _run(db.get_unsynced_rows("cycle_log"))
        assert len(rows) == 1


class TestConcurrentWriteSafety:
    """WAL mode + concurrent async writes don't corrupt the database."""

    def test_sequential_writes_from_two_loops(self, tmp_path: Path) -> None:
        """Simulate two event loops writing to the same WAL DB sequentially."""
        db_path = str(tmp_path / "concurrent.db")

        # Loop 1: initialize + write
        loop1 = asyncio.new_event_loop()
        db1 = LocalDB(db_path)
        try:
            loop1.run_until_complete(db1.initialize())
            loop1.run_until_complete(db1.write_cycle(CycleLogEntry(
                cycle_id="loop1_c1", timestamp="2026-04-12T22:00:00",
                scenario="MIDDAY_CHARGE", guard_level="ok",
                headroom_kw=1.0, elapsed_s=0.05,
            )))
            loop1.run_until_complete(db1.close())
        finally:
            loop1.close()

        # Loop 2: open same DB, write more rows
        loop2 = asyncio.new_event_loop()
        db2 = LocalDB(db_path)
        try:
            loop2.run_until_complete(db2.initialize())
            loop2.run_until_complete(db2.write_cycle(CycleLogEntry(
                cycle_id="loop2_c1", timestamp="2026-04-12T22:01:00",
                scenario="EVENING_DISCHARGE", guard_level="ok",
                headroom_kw=0.8, elapsed_s=0.04,
            )))
            rows = loop2.run_until_complete(db2.get_unsynced_rows("cycle_log"))
            assert len(rows) == 2
            cycle_ids = {r["cycle_id"] for r in rows}
            assert "loop1_c1" in cycle_ids
            assert "loop2_c1" in cycle_ids
            loop2.run_until_complete(db2.close())
        finally:
            loop2.close()
