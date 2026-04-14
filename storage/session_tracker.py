"""Energy session tracker for CARMA Box (PLAT-1534).

Tracks open battery charge/discharge sessions, EV charging sessions,
and daily PV production. Writes completed sessions to LocalDB.

Integration: called from core/engine.py in run_cycle() after EXECUTE phase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.models import BatteryState, CTPlacement, EVState, SystemSnapshot
from storage.local_db import (
    SESSION_SOURCE_GRID,
    SESSION_SOURCE_MIXED,
    SESSION_SOURCE_PV,
    SESSION_TYPE_CHARGE,
    SESSION_TYPE_DISCHARGE,
    BatterySessionEntry,
    EVSessionEntry,
    LocalDB,
    PVDailySummaryEntry,
)

logger = logging.getLogger(__name__)

# EMS modes that represent active battery charging
_CHARGING_MODES: frozenset[str] = frozenset({"charge_pv", "import_ac"})

# EMS modes that represent active battery discharging
_DISCHARGING_MODES: frozenset[str] = frozenset({"discharge_pv", "export_ac"})

# EV event types (mirror CommandType values)
EV_EVENT_START = "start_ev_charging"
EV_EVENT_STOP = "stop_ev_charging"

# Cycle interval used for energy accumulation (seconds)
# Matches the CARMA Box 30-second control loop
CYCLE_INTERVAL_S: float = 30.0

# Watts-to-kWh conversion factor for one cycle interval
_WH_PER_CYCLE = CYCLE_INTERVAL_S / 3600.0

# PV source inference thresholds (fraction of total power that is PV-sourced)
_PV_DOMINANT_THRESHOLD: float = 0.8  # PV covers ≥80% → source = "pv"
_GRID_DOMINANT_THRESHOLD: float = 0.2  # PV covers ≤20% → source = "grid"


@dataclass
class _OpenBatterySession:
    """In-memory state for a battery session in progress."""

    battery_id: str
    session_type: str  # SESSION_TYPE_CHARGE or SESSION_TYPE_DISCHARGE
    started_at: str  # ISO-8601 UTC
    source: str  # SESSION_SOURCE_PV / GRID / MIXED
    sum_power_w: float = 0.0
    sample_count: int = 0


@dataclass
class _OpenEVSession:
    """In-memory state for an EV charging session in progress."""

    started_at: str  # ISO-8601 UTC
    soc_start_pct: float
    sum_current_a: float = 0.0
    sum_energy_kwh: float = 0.0
    sample_count: int = 0


@dataclass
class _PVDailyAccumulator:
    """Accumulates PV energy within a calendar day."""

    date: str  # YYYY-MM-DD
    kwh_kontor: float = 0.0
    kwh_forrad: float = 0.0

    @property
    def kwh_total(self) -> float:
        """Total PV production across both CT placements."""
        return self.kwh_kontor + self.kwh_forrad


class EnergySessionTracker:
    """Tracks open energy sessions and writes completed ones to LocalDB.

    Thread-safety: single-writer, single async event loop — no locking needed.
    """

    def __init__(self, db: LocalDB, site_id: str) -> None:
        """Initialise tracker.

        Args:
            db: Initialised LocalDB instance (initialize() already called).
            site_id: Site identifier written to every row.
        """
        self._db = db
        self._site_id = site_id
        self._open_battery: dict[str, _OpenBatterySession] = {}
        self._open_ev: Optional[_OpenEVSession] = None
        self._pv_accum: Optional[_PVDailyAccumulator] = None

    # ------------------------------------------------------------------
    # Public inspection helpers (used by tests)
    # ------------------------------------------------------------------

    def has_open_battery_session(self, battery_id: str) -> bool:
        """Return True if there is an open battery session for this battery."""
        return battery_id in self._open_battery

    def has_open_ev_session(self) -> bool:
        """Return True if there is an open EV session."""
        return self._open_ev is not None

    # ------------------------------------------------------------------
    # Battery sessions
    # ------------------------------------------------------------------

    async def on_battery_mode_change(
        self,
        battery_id: str,
        new_mode: str,
        snapshot: SystemSnapshot,
    ) -> None:
        """Handle a battery EMS mode observation.

        Opens a session when mode enters a charging/discharging state.
        Closes and persists the session when mode leaves that state.

        Called every cycle with the current ems_mode from the snapshot.

        Args:
            battery_id: Identifier of the battery.
            new_mode: Current EMSMode value string (e.g. "charge_pv").
            snapshot: SystemSnapshot (used for timestamp only).
        """
        now = _utc_now()
        is_active = new_mode in _CHARGING_MODES or new_mode in _DISCHARGING_MODES
        existing = self._open_battery.get(battery_id)

        if is_active and existing is None:
            # Open a new session
            session_type = (
                SESSION_TYPE_CHARGE if new_mode in _CHARGING_MODES else SESSION_TYPE_DISCHARGE
            )
            self._open_battery[battery_id] = _OpenBatterySession(
                battery_id=battery_id,
                session_type=session_type,
                started_at=now,
                source=SESSION_SOURCE_PV,  # default; refined on close
            )
            logger.debug("Battery session opened: %s (%s)", battery_id, session_type)
            return

        if is_active and existing is not None:
            # Still in the same active mode: accumulate power sample
            bat = _find_battery(snapshot, battery_id)
            if bat is not None:
                existing.sum_power_w += abs(bat.power_w)
                existing.sample_count += 1
            return

        if not is_active and existing is not None:
            # Mode changed away from active → close session
            await self._close_battery_session(existing, ended_at=now, snapshot=snapshot)
            del self._open_battery[battery_id]

    async def _close_battery_session(
        self,
        sess: _OpenBatterySession,
        ended_at: str,
        snapshot: SystemSnapshot,
    ) -> None:
        """Compute final metrics and write completed battery session to DB."""
        avg_power_w = sess.sum_power_w / sess.sample_count if sess.sample_count > 0 else 0.0
        duration_s = _iso_diff_s(sess.started_at, ended_at)
        energy_kwh = avg_power_w * duration_s / 3600.0

        # Determine source from last known snapshot
        source = _infer_source(snapshot, sess.battery_id)

        entry = BatterySessionEntry(
            site_id=self._site_id,
            battery_id=sess.battery_id,
            session_type=sess.session_type,
            started_at=sess.started_at,
            ended_at=ended_at,
            duration_s=duration_s,
            energy_kwh=energy_kwh,
            source=source,
            avg_power_w=avg_power_w,
        )
        await self._db.write_battery_session(entry)
        logger.debug(
            "Battery session closed: %s (%s) %.3f kWh in %.0fs",
            sess.battery_id,
            sess.session_type,
            energy_kwh,
            duration_s,
        )

    # ------------------------------------------------------------------
    # EV sessions
    # ------------------------------------------------------------------

    async def on_ev_event(self, event_type: str, snapshot: SystemSnapshot) -> None:
        """Handle an EV charging event.

        Opens a session on EV_EVENT_START, closes and persists on EV_EVENT_STOP.

        Args:
            event_type: EV_EVENT_START or EV_EVENT_STOP.
            snapshot: SystemSnapshot (used for EV state and timestamp).
        """
        now = _utc_now()
        ev = _get_ev(snapshot)

        if event_type == EV_EVENT_START and self._open_ev is None:
            soc_start = ev.soc_pct if ev is not None else 0.0
            self._open_ev = _OpenEVSession(
                started_at=now,
                soc_start_pct=soc_start,
            )
            logger.debug("EV session opened (soc_start=%.1f%%)", soc_start)

        elif event_type == EV_EVENT_STOP and self._open_ev is not None:
            await self._close_ev_session(self._open_ev, ended_at=now, snapshot=snapshot)
            self._open_ev = None

    async def _close_ev_session(
        self,
        sess: _OpenEVSession,
        ended_at: str,
        snapshot: SystemSnapshot,
    ) -> None:
        """Compute final metrics and write completed EV session to DB."""
        ev = _get_ev(snapshot)
        soc_end = ev.soc_pct if ev is not None else 0.0
        avg_current_a = sess.sum_current_a / sess.sample_count if sess.sample_count > 0 else 0.0
        duration_s = _iso_diff_s(sess.started_at, ended_at)
        energy_kwh = sess.sum_energy_kwh

        entry = EVSessionEntry(
            site_id=self._site_id,
            started_at=sess.started_at,
            ended_at=ended_at,
            duration_s=duration_s,
            energy_kwh=energy_kwh,
            soc_start_pct=sess.soc_start_pct,
            soc_end_pct=soc_end,
            avg_current_a=avg_current_a,
        )
        await self._db.write_ev_session(entry)
        logger.debug(
            "EV session closed: soc %.1f%%→%.1f%%, %.3f kWh in %.0fs",
            sess.soc_start_pct,
            soc_end,
            energy_kwh,
            duration_s,
        )

    # ------------------------------------------------------------------
    # PV daily accumulation
    # ------------------------------------------------------------------

    async def update_pv_daily(self, snapshot: SystemSnapshot) -> None:
        """Accumulate PV production for today's summary.

        Called every cycle. Splits production by CT placement (kontor/förråd).

        Args:
            snapshot: SystemSnapshot with battery list and grid state.
        """
        today = _today_date()

        if self._pv_accum is None or self._pv_accum.date != today:
            # New day — flush previous day if any, start fresh
            if self._pv_accum is not None:
                await self._write_pv_daily(self._pv_accum)
            self._pv_accum = _PVDailyAccumulator(date=today)

        for bat in snapshot.batteries:
            kwh = abs(bat.pv_power_w) * _WH_PER_CYCLE
            # Kontor inverter uses LOCAL_LOAD CT; Förråd inverter uses HOUSE_GRID CT
            if bat.ct_placement == CTPlacement.LOCAL_LOAD:
                self._pv_accum.kwh_kontor += kwh
            else:
                self._pv_accum.kwh_forrad += kwh

    async def flush(self) -> None:
        """Write current PV daily accumulator to DB (call at midnight or shutdown).

        Idempotent — safe to call multiple times for the same day.
        """
        if self._pv_accum is not None:
            await self._write_pv_daily(self._pv_accum)

    async def _write_pv_daily(self, accum: _PVDailyAccumulator) -> None:
        """Upsert a PV daily summary entry from an accumulator."""
        entry = PVDailySummaryEntry(
            site_id=self._site_id,
            date=accum.date,
            pv_kwh_total=accum.kwh_total,
            pv_kwh_kontor=accum.kwh_kontor,
            pv_kwh_forrad=accum.kwh_forrad,
            created_at=_utc_now(),
        )
        await self._db.upsert_pv_daily_summary(entry)
        logger.debug(
            "PV daily upserted: %s total=%.3f kWh (kontor=%.3f, forrad=%.3f)",
            accum.date,
            accum.kwh_total,
            accum.kwh_kontor,
            accum.kwh_forrad,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


def _today_date() -> str:
    """Return today's date as YYYY-MM-DD string (UTC)."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _iso_diff_s(started_at: str, ended_at: str) -> float:
    """Return duration in seconds between two ISO-8601 timestamps.

    Returns 0.0 if either timestamp cannot be parsed.
    """
    try:
        t_start = datetime.fromisoformat(started_at)
        t_end = datetime.fromisoformat(ended_at)
        return max(0.0, (t_end - t_start).total_seconds())
    except ValueError:
        logger.warning("Cannot compute duration: %r → %r", started_at, ended_at)
        return 0.0


def _find_battery(snapshot: SystemSnapshot, battery_id: str) -> Optional[BatteryState]:
    """Return the BatteryState with matching battery_id, or None."""
    for bat in snapshot.batteries:
        if bat.battery_id == battery_id:
            return bat
    return None


def _get_batteries(snapshot: SystemSnapshot) -> list[BatteryState]:
    """Return batteries list from snapshot."""
    return snapshot.batteries


def _get_ev(snapshot: SystemSnapshot) -> Optional[EVState]:
    """Return EV state from snapshot."""
    return snapshot.ev


def _infer_source(snapshot: SystemSnapshot, battery_id: str) -> str:
    """Infer charge source (pv/grid/mixed) from current battery snapshot.

    Uses PV power vs grid power ratio as heuristic:
    - pv: PV power covers ≥ 80% of battery charge power
    - grid: grid power covers ≥ 80% of battery charge power
    - mixed: everything in between
    """
    bat = _find_battery(snapshot, battery_id)
    if bat is None:
        return SESSION_SOURCE_MIXED

    pv_w = abs(bat.pv_power_w)
    grid_w = abs(bat.grid_power_w)
    total_w = pv_w + grid_w
    if total_w <= 0:
        return SESSION_SOURCE_MIXED

    pv_ratio = pv_w / total_w
    if pv_ratio >= _PV_DOMINANT_THRESHOLD:
        return SESSION_SOURCE_PV
    if pv_ratio <= _GRID_DOMINANT_THRESHOLD:
        return SESSION_SOURCE_GRID
    return SESSION_SOURCE_MIXED
