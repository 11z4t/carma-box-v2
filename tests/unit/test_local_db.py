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
