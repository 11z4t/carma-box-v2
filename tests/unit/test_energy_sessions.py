"""Tests for PLAT-1534: Energy session logging (battery/EV/PV) to SQLite.

TDD guard tests — these verify:
- New schema tables exist after migration
- Write/read round-trips for all 3 entry types
- Session tracker state machine logic
- Migration from version 1 → 2
- RuntimeError if disk version > code version
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Coroutine
from unittest.mock import MagicMock

import pytest

from storage.local_db import (
    BatterySessionEntry,
    EVSessionEntry,
    LocalDB,
    PVDailySummaryEntry,
    _SCHEMA_VERSION,
)
from storage.session_tracker import EnergySessionTracker


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


def _make_snapshot(
    *,
    ev_charging: bool = False,
    ev_soc: float = 50.0,
    ev_current_a: float = 0.0,
    ev_power_w: float = 0.0,
    pv_total_w: float = 2000.0,
    bat_pv_w: float = 1000.0,
    bat_pv_kontor_w: float = 1000.0,
    bat_pv_forrad_w: float = 1000.0,
    ems_mode: str = "charge_pv",
) -> Any:
    """Build a minimal SystemSnapshot mock for session tracker tests."""
    from core.models import CTPlacement, EMSMode

    bat_kontor = MagicMock()
    bat_kontor.battery_id = "bat-kontor"
    bat_kontor.ems_mode = EMSMode(ems_mode)
    bat_kontor.pv_power_w = bat_pv_kontor_w
    bat_kontor.power_w = -bat_pv_kontor_w  # negative = charging
    bat_kontor.grid_power_w = 0.0
    bat_kontor.ct_placement = CTPlacement.LOCAL_LOAD  # Kontor = local_load

    bat_forrad = MagicMock()
    bat_forrad.battery_id = "bat-forrad"
    bat_forrad.ems_mode = EMSMode(ems_mode)
    bat_forrad.pv_power_w = bat_pv_forrad_w
    bat_forrad.power_w = bat_pv_forrad_w  # positive = discharging
    bat_forrad.grid_power_w = 0.0
    bat_forrad.ct_placement = CTPlacement.HOUSE_GRID  # Förråd = house_grid

    ev = MagicMock()
    ev.charging = ev_charging
    ev.soc_pct = ev_soc
    ev.current_a = ev_current_a
    ev.power_w = ev_power_w

    grid = MagicMock()
    grid.pv_total_w = pv_total_w

    snap = MagicMock()
    snap.batteries = [bat_kontor, bat_forrad]
    snap.ev = ev
    snap.grid = grid
    snap.timestamp = datetime.now(tz=timezone.utc)
    return snap


# ===========================================================================
# Guard test — schema version
# ===========================================================================


class TestSchemaVersion:
    """Verify schema versioning and migration guard."""

    def test_schema_version_is_2(self) -> None:
        """PLAT-1534: _SCHEMA_VERSION must be 2 after migration."""
        assert _SCHEMA_VERSION == 2

    def test_schema_migration_v2(self, tmp_path: Path) -> None:
        """Empty DB must migrate to version 2 with all new tables present."""
        path = tmp_path / "migrate.db"
        local_db = LocalDB(str(path))
        _run(local_db.initialize())

        async def _check() -> list[str]:
            db = local_db._db
            assert db is not None
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

        tables = _run(_check())
        _run(local_db.close())

        assert "battery_session" in tables
        assert "ev_session" in tables
        assert "pv_daily_summary" in tables

    def test_schema_version_rejected_if_newer(self, tmp_path: Path) -> None:
        """RuntimeError raised when disk schema version is newer than code."""
        import aiosqlite

        path = tmp_path / "future.db"

        async def _seed_future_version() -> None:
            async with aiosqlite.connect(str(path)) as db:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                await db.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (_SCHEMA_VERSION + 1, "2099-01-01T00:00:00+00:00"),
                )
                await db.commit()

        _run(_seed_future_version())

        local_db = LocalDB(str(path))
        with pytest.raises(RuntimeError, match="newer than code version"):
            _run(local_db.initialize())


# ===========================================================================
# BatterySessionEntry write + read
# ===========================================================================


class TestBatterySession:
    """Write and read battery_session entries."""

    def test_battery_session_write_read(self, db: LocalDB) -> None:
        """Written BatterySessionEntry must round-trip to identical values."""
        entry = BatterySessionEntry(
            site_id="site-malmgren",
            battery_id="bat-kontor",
            session_type="charge",
            started_at="2026-04-14T10:00:00+00:00",
            ended_at="2026-04-14T10:30:00+00:00",
            duration_s=1800.0,
            energy_kwh=1.5,
            source="pv",
            avg_power_w=3000.0,
        )
        _run(db.write_battery_session(entry))

        rows = _run(db.get_unsynced_rows("battery_session"))
        assert len(rows) == 1
        row = rows[0]
        assert row["site_id"] == "site-malmgren"
        assert row["battery_id"] == "bat-kontor"
        assert row["session_type"] == "charge"
        assert row["source"] == "pv"
        assert row["duration_s"] == pytest.approx(1800.0)
        assert row["energy_kwh"] == pytest.approx(1.5)
        assert row["avg_power_w"] == pytest.approx(3000.0)
        assert row["synced"] == 0


# ===========================================================================
# EVSessionEntry write + read
# ===========================================================================


class TestEVSession:
    """Write and read ev_session entries."""

    def test_ev_session_write_read(self, db: LocalDB) -> None:
        """Written EVSessionEntry must round-trip to identical values."""
        entry = EVSessionEntry(
            site_id="site-malmgren",
            started_at="2026-04-14T18:00:00+00:00",
            ended_at="2026-04-14T20:00:00+00:00",
            duration_s=7200.0,
            energy_kwh=11.0,
            soc_start_pct=20.0,
            soc_end_pct=80.0,
            avg_current_a=16.0,
        )
        _run(db.write_ev_session(entry))

        rows = _run(db.get_unsynced_rows("ev_session"))
        assert len(rows) == 1
        row = rows[0]
        assert row["site_id"] == "site-malmgren"
        assert row["soc_start_pct"] == pytest.approx(20.0)
        assert row["soc_end_pct"] == pytest.approx(80.0)
        assert row["avg_current_a"] == pytest.approx(16.0)
        assert row["energy_kwh"] == pytest.approx(11.0)
        assert row["synced"] == 0


# ===========================================================================
# PVDailySummaryEntry upsert
# ===========================================================================


class TestPVDailySummary:
    """Upsert (INSERT + UPDATE) behaviour for pv_daily_summary."""

    def test_pv_daily_upsert_insert(self, db: LocalDB) -> None:
        """First upsert inserts a new row."""
        entry = PVDailySummaryEntry(
            site_id="site-malmgren",
            date="2026-04-14",
            pv_kwh_total=12.5,
            pv_kwh_kontor=7.5,
            pv_kwh_forrad=5.0,
            created_at="2026-04-14T23:59:00+00:00",
        )
        _run(db.upsert_pv_daily_summary(entry))

        rows = _run(db.get_unsynced_rows("pv_daily_summary"))
        assert len(rows) == 1
        assert rows[0]["pv_kwh_total"] == pytest.approx(12.5)
        assert rows[0]["date"] == "2026-04-14"

    def test_pv_daily_upsert_is_idempotent(self, db: LocalDB) -> None:
        """Second upsert for same (site_id, date) replaces the row, not duplicates."""
        entry_v1 = PVDailySummaryEntry(
            site_id="site-malmgren",
            date="2026-04-14",
            pv_kwh_total=10.0,
            pv_kwh_kontor=6.0,
            pv_kwh_forrad=4.0,
            created_at="2026-04-14T22:00:00+00:00",
        )
        entry_v2 = PVDailySummaryEntry(
            site_id="site-malmgren",
            date="2026-04-14",
            pv_kwh_total=14.2,
            pv_kwh_kontor=8.2,
            pv_kwh_forrad=6.0,
            created_at="2026-04-14T23:59:00+00:00",
        )
        _run(db.upsert_pv_daily_summary(entry_v1))
        _run(db.upsert_pv_daily_summary(entry_v2))

        rows = _run(db.get_unsynced_rows("pv_daily_summary"))
        assert len(rows) == 1, "Upsert must not create duplicate rows"
        assert rows[0]["pv_kwh_total"] == pytest.approx(14.2)


# ===========================================================================
# EnergySessionTracker — battery session lifecycle
# ===========================================================================


class TestBatterySessionTracker:
    """EnergySessionTracker battery session start/end logic."""

    def test_battery_session_starts_on_charge_mode(self, tmp_path: Path) -> None:
        """Tracker opens a battery session when ems_mode = charge_pv."""
        path = tmp_path / "tracker.db"
        local_db = LocalDB(str(path))
        _run(local_db.initialize())

        tracker = EnergySessionTracker(db=local_db, site_id="site-test")
        snap = _make_snapshot(ems_mode="charge_pv")

        _run(tracker.on_battery_mode_change("bat-kontor", "charge_pv", snap))

        # Session should be open (in-memory) — not yet written to DB
        assert tracker.has_open_battery_session("bat-kontor")
        _run(local_db.close())

    def test_battery_session_ends_on_mode_change(self, tmp_path: Path) -> None:
        """Tracker closes and persists session when mode changes away from charging."""
        path = tmp_path / "tracker.db"
        local_db = LocalDB(str(path))
        _run(local_db.initialize())

        tracker = EnergySessionTracker(db=local_db, site_id="site-test")
        snap_charge = _make_snapshot(ems_mode="charge_pv")
        snap_standby = _make_snapshot(ems_mode="battery_standby")

        _run(tracker.on_battery_mode_change("bat-kontor", "charge_pv", snap_charge))
        _run(tracker.on_battery_mode_change("bat-kontor", "battery_standby", snap_standby))

        # Session must now be written to DB
        rows = _run(local_db.get_unsynced_rows("battery_session"))
        assert len(rows) == 1
        assert rows[0]["battery_id"] == "bat-kontor"
        assert rows[0]["session_type"] == "charge"
        assert rows[0]["ended_at"] != ""
        assert not tracker.has_open_battery_session("bat-kontor")
        _run(local_db.close())


# ===========================================================================
# EnergySessionTracker — EV session lifecycle
# ===========================================================================


class TestEVSessionTracker:
    """EnergySessionTracker EV session start/end logic."""

    def test_ev_session_lifecycle(self, tmp_path: Path) -> None:
        """EV session opens on START_EV_CHARGING and closes on STOP_EV_CHARGING."""
        path = tmp_path / "ev_tracker.db"
        local_db = LocalDB(str(path))
        _run(local_db.initialize())

        tracker = EnergySessionTracker(db=local_db, site_id="site-test")

        snap_start = _make_snapshot(
            ev_charging=True, ev_soc=25.0, ev_current_a=16.0, ev_power_w=3680.0
        )
        snap_stop = _make_snapshot(ev_charging=False, ev_soc=75.0, ev_current_a=0.0, ev_power_w=0.0)

        _run(tracker.on_ev_event("start_ev_charging", snap_start))
        assert tracker.has_open_ev_session()

        _run(tracker.on_ev_event("stop_ev_charging", snap_stop))
        assert not tracker.has_open_ev_session()

        rows = _run(local_db.get_unsynced_rows("ev_session"))
        assert len(rows) == 1
        row = rows[0]
        assert row["soc_start_pct"] == pytest.approx(25.0)
        assert row["soc_end_pct"] == pytest.approx(75.0)
        assert row["ended_at"] != ""
        _run(local_db.close())


# ===========================================================================
# EnergySessionTracker — PV accumulation
# ===========================================================================


class TestPVAccumulation:
    """EnergySessionTracker PV accumulation and flush."""

    def test_pv_accumulation(self, tmp_path: Path) -> None:
        """update_pv_daily accumulates PV kWh from successive snapshots."""
        path = tmp_path / "pv.db"
        local_db = LocalDB(str(path))
        _run(local_db.initialize())

        tracker = EnergySessionTracker(db=local_db, site_id="site-test")

        # Two snapshots 30 seconds apart: each bat produces 1000W PV
        # Energy per snapshot = 1000W * 30s / 3600 = 0.00833 kWh per battery
        snap = _make_snapshot(bat_pv_kontor_w=1000.0, bat_pv_forrad_w=1000.0)
        _run(tracker.update_pv_daily(snap))
        _run(tracker.update_pv_daily(snap))

        # Flush writes to DB
        _run(tracker.flush())

        rows = _run(local_db.get_unsynced_rows("pv_daily_summary"))
        assert len(rows) == 1
        row = rows[0]
        assert row["pv_kwh_kontor"] > 0.0
        assert row["pv_kwh_forrad"] > 0.0
        assert row["pv_kwh_total"] == pytest.approx(row["pv_kwh_kontor"] + row["pv_kwh_forrad"])
        _run(local_db.close())
