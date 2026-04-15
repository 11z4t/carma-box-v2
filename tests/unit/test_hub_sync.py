"""Tests for PostgreSQL Hub Sync.

Covers:
- Transform row adds site_id, removes synced/id
- Sync marks rows as synced
- PLAT-1565: ValueError when host set but site_id or database missing
- Empty table returns 0
- Sync error caught gracefully
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from storage.hub_sync import HubSync, HubSyncConfig
from storage.local_db import CycleLogEntry, LocalDB


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def db(tmp_path: Path) -> Generator[LocalDB, None, None]:
    local_db = LocalDB(str(tmp_path / "test.db"))
    _run(local_db.initialize())
    yield local_db
    _run(local_db.close())


@pytest.fixture()
def sync(db: LocalDB) -> HubSync:
    return HubSync(HubSyncConfig(site_id="test-site"), db)


class TestTransformRow:
    """Row transformation for PostgreSQL."""

    def test_adds_site_id(self, sync: HubSync) -> None:
        row: dict[str, Any] = {"cycle_id": "abc", "synced": 0, "id": 1}
        result = sync._transform_row("cycle_log", row)
        assert result["site_id"] == "test-site"
        assert "synced" not in result
        assert "id" not in result
        assert result["cycle_id"] == "abc"


class TestSyncTable:
    """Sync table operations."""

    def test_empty_table_returns_zero(self, sync: HubSync) -> None:
        count = _run(sync._sync_table("cycle_log"))
        assert count == 0

    def test_dry_run_does_not_mark_rows_synced(self, db: LocalDB) -> None:
        """PLAT-1353: dry_run=True (default) must NOT mark rows as synced."""
        sync = HubSync(HubSyncConfig(site_id="test-site"), db, dry_run=True)
        _run(db.write_cycle(CycleLogEntry(
            cycle_id="c1", timestamp="2026-04-12T22:00:00",
            scenario="PV_SURPLUS_DAY", guard_level="ok",
            headroom_kw=1.0, elapsed_s=0.05,
        )))
        count = _run(sync._sync_table("cycle_log"))
        # dry_run returns 0 (nothing was actually synced to PG)
        assert count == 0
        # Row remains unsynced because PG insert didn't happen
        unsynced = _run(db.get_unsynced_rows("cycle_log"))
        assert len(unsynced) == 1

    def test_non_dry_run_marks_rows_synced(self, db: LocalDB) -> None:
        """PLAT-1353: dry_run=False marks rows synced after 'insert'."""
        sync = HubSync(HubSyncConfig(site_id="test-site"), db, dry_run=False)
        _run(db.write_cycle(CycleLogEntry(
            cycle_id="c1", timestamp="2026-04-12T22:00:00",
            scenario="PV_SURPLUS_DAY", guard_level="ok",
            headroom_kw=1.0, elapsed_s=0.05,
        )))
        count = _run(sync._sync_table("cycle_log"))
        assert count == 1
        # Row should now be synced
        unsynced = _run(db.get_unsynced_rows("cycle_log"))
        assert len(unsynced) == 0


class TestSyncAll:
    """Full sync across all tables."""

    def test_sync_returns_zero_in_dry_run(self, sync: HubSync, db: LocalDB) -> None:
        """PLAT-1353: default dry_run mode returns 0 (nothing sent to PG)."""
        _run(db.write_cycle(CycleLogEntry(
            cycle_id="c1", timestamp="2026-04-12T22:00:00",
            scenario="PV_SURPLUS_DAY", guard_level="ok",
            headroom_kw=1.0, elapsed_s=0.05,
        )))
        results = _run(sync.sync())
        # dry_run=True → 0 rows "synced" (no PG)
        assert results["cycle_log"] == 0
        assert results["event_log"] == 0

    def test_sync_returns_counts_when_live(self, db: LocalDB) -> None:
        """Non-dry-run mode returns actual row counts."""
        sync = HubSync(HubSyncConfig(site_id="test-site"), db, dry_run=False)
        _run(db.write_cycle(CycleLogEntry(
            cycle_id="c1", timestamp="2026-04-12T22:00:00",
            scenario="PV_SURPLUS_DAY", guard_level="ok",
            headroom_kw=1.0, elapsed_s=0.05,
        )))
        results = _run(sync.sync())
        assert results["cycle_log"] == 1
        assert results["event_log"] == 0

    def test_sync_error_caught(self, db: LocalDB) -> None:
        """Sync error on a table should not crash, returns 0 for that table."""
        sync = HubSync(HubSyncConfig(site_id="test-site"), db, dry_run=False)
        # Monkey-patch to simulate error
        original = sync._sync_table

        async def failing_sync(table: str) -> int:
            if table == "event_log":
                raise ConnectionError("PG down")
            return await original(table)

        sync._sync_table = failing_sync  # type: ignore[method-assign]
        results = _run(sync.sync())
        assert results["event_log"] == 0  # Error caught


# ===========================================================================
# PLAT-1565: Validation — no production defaults
# ===========================================================================


class TestHubSyncConfigValidation:
    """HubSyncConfig raises ValueError when host is set but required fields missing."""

    # A non-empty host triggers the validation check
    _HOST: str = "db.example.com"
    _SITE_ID: str = "site-test"
    _DATABASE: str = "energy_test"

    def test_missing_site_id_raises_value_error(self, db: LocalDB) -> None:
        """PLAT-1565: ValueError when host is set but site_id is None."""
        cfg = HubSyncConfig(host=self._HOST, database=self._DATABASE)
        with pytest.raises(ValueError, match="site_id"):
            HubSync(cfg, db)

    def test_missing_database_raises_value_error(self, db: LocalDB) -> None:
        """PLAT-1565: ValueError when host is set but database is None."""
        cfg = HubSyncConfig(host=self._HOST, site_id=self._SITE_ID)
        with pytest.raises(ValueError, match="database"):
            HubSync(cfg, db)

    def test_valid_config_does_not_raise(self, db: LocalDB) -> None:
        """All required fields set → no ValueError."""
        cfg = HubSyncConfig(
            host=self._HOST,
            site_id=self._SITE_ID,
            database=self._DATABASE,
        )
        sync = HubSync(cfg, db)
        assert sync is not None

    def test_no_host_skips_validation(self, db: LocalDB) -> None:
        """No host (dry-run config) → validation skipped even without site_id."""
        cfg = HubSyncConfig()  # No host, no site_id, no database
        sync = HubSync(cfg, db)  # Must not raise
        assert sync is not None
