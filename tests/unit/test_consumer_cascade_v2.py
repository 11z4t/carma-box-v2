"""Tests for core/consumer_cascade_v2.py (PLAT-1790).

Covers: ConsumerStateTracker lifecycle, wear guards, cascade preemption,
priority ordering, daily reset, and persistence.
"""

from __future__ import annotations

import pytest

from config.schema import WearLimits
from core.consumer_cascade_v2 import (
    CascadeConsumer,
    ConsumerStateTracker,
    evaluate_cascade,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _tracker() -> ConsumerStateTracker:
    return ConsumerStateTracker()


def _consumer(
    id: str,
    priority: int,
    is_running: bool = False,
    min_power_w: float = 500.0,
    **wear_kwargs: object,
) -> CascadeConsumer:
    return CascadeConsumer(
        id=id,
        priority=priority,
        is_running=is_running,
        min_power_w=min_power_w,
        wear_limits=WearLimits(**wear_kwargs),  # type: ignore[arg-type]
    )


# ── Test 1: Priority order — bat before EV ───────────────────────────────────

def test_priority_order_bat_before_ev() -> None:
    """bat (priority=1) takes surplus before EV (priority=2)."""
    tracker = _tracker()
    # Only enough surplus for one consumer (600W)
    result = evaluate_cascade(
        [
            _consumer("bat", priority=1, is_running=False, min_power_w=500.0),
            _consumer("ev", priority=2, is_running=False, min_power_w=500.0),
        ],
        available_surplus_w=600.0,
        tracker=tracker,
        now=1000.0,
    )
    assert "bat" in result.to_start
    assert "ev" not in result.to_start


# ── Test 2: EV before VP ─────────────────────────────────────────────────────

def test_ev_before_vp() -> None:
    """EV (priority=2) takes surplus before VP (priority=3)."""
    tracker = _tracker()
    result = evaluate_cascade(
        [
            _consumer("ev", priority=2, is_running=False, min_power_w=4000.0),
            _consumer("vp", priority=3, is_running=False, min_power_w=1500.0),
        ],
        available_surplus_w=4500.0,  # enough for ev (4000) but vp gets remaining 500 < 1500
        tracker=tracker,
        now=1000.0,
    )
    assert "ev" in result.to_start
    assert "vp" not in result.to_start


# ── Test 3: Preemption — miner preempted for EV ──────────────────────────────

def test_preemption_low_priority_yields_to_high() -> None:
    """Miner (priority=6) is preempted when EV (priority=2) takes the surplus.

    Scenario: EV starts (takes 4200W of 4700W surplus), leaving only 500W.
    Miner needs 600W → falls below minimum → miner is stopped.
    Since EV (higher priority) started this cycle, miner is marked as preempted.
    """
    tracker = _tracker()
    # Miner has been running for 1000s (well above any min_on_time)
    tracker.record_on("miner", now=0.0)

    result = evaluate_cascade(
        [
            _consumer("ev", priority=2, is_running=False, min_power_w=4200.0),
            _consumer("miner", priority=6, is_running=True, min_power_w=600.0),
        ],
        available_surplus_w=4700.0,  # Enough for EV (4200), leaves 500 < miner (600)
        tracker=tracker,
        now=1000.0,
    )
    assert "ev" in result.to_start
    assert "miner" in result.to_stop
    assert "miner" in result.preempted


# ── Test 4: Wear guard — miner not preemptable < 30min ───────────────────────

def test_wear_guard_miner_min_30min_blocks_preemption() -> None:
    """Miner with min_on_time=30min cannot be preempted if running for < 30min."""
    tracker = _tracker()
    tracker.record_on("miner", now=0.0)  # Just turned on

    result = evaluate_cascade(
        [
            _consumer("ev", priority=2, is_running=False, min_power_w=4000.0),
            _consumer(
                "miner",
                priority=6,
                is_running=True,
                min_power_w=500.0,
                min_on_time_s=1800,  # 30 min
            ),
        ],
        available_surplus_w=3800.0,
        tracker=tracker,
        now=100.0,  # Only 100s of 1800s elapsed
    )
    assert "miner" not in result.preempted
    assert "miner" not in result.to_stop
    assert "miner" in result.blocked_by_wear or "ev" not in result.to_start


# ── Test 5: Wear guard — VP min 10min respected ───────────────────────────────

def test_wear_guard_vp_min_10min_respected() -> None:
    """VP with min_on_time=10min cannot be stopped before 10min."""
    tracker = _tracker()
    tracker.record_on("vp", now=0.0)

    result = evaluate_cascade(
        [_consumer("vp", priority=3, is_running=True, min_power_w=1500.0, min_on_time_s=600)],
        available_surplus_w=0.0,  # No surplus — normally would stop
        tracker=tracker,
        now=200.0,  # Only 200s < 600s min_on
    )
    assert "vp" not in result.to_stop
    assert "vp" in result.blocked_by_wear


# ── Test 6: No preemption within min_on_time ─────────────────────────────────

def test_no_preemption_within_min_on_time() -> None:
    """No preemption happens if low-priority consumer is within min_on_time window."""
    tracker = _tracker()
    tracker.record_on("pool", now=0.0)

    result = evaluate_cascade(
        [
            _consumer("ev", priority=2, is_running=False, min_power_w=4000.0),
            _consumer("pool", priority=5, is_running=True, min_power_w=3000.0, min_on_time_s=600),
        ],
        available_surplus_w=3500.0,
        tracker=tracker,
        now=100.0,
    )
    assert "pool" not in result.preempted
    assert "pool" not in result.to_stop


# ── Test 7: Surplus too small for EV ─────────────────────────────────────────

def test_surplus_too_small_for_ev_not_started() -> None:
    """EV is not started if surplus < ev min_power."""
    tracker = _tracker()
    result = evaluate_cascade(
        [_consumer("ev", priority=2, is_running=False, min_power_w=4000.0)],
        available_surplus_w=1000.0,
        tracker=tracker,
        now=1000.0,
    )
    assert "ev" not in result.to_start


# ── Test 8: Graceful ramp-down — all consumers stop when no surplus ───────────

def test_all_consumers_stop_when_no_surplus() -> None:
    """All running consumers stop (subject to wear) when surplus = 0."""
    tracker = _tracker()
    tracker.record_on("miner", now=0.0)
    tracker.record_on("vp", now=0.0)

    # Both miner and vp have been running 1000s (well above any min_on_time)
    result = evaluate_cascade(
        [
            _consumer("miner", priority=6, is_running=True, min_power_w=500.0),
            _consumer("vp", priority=3, is_running=True, min_power_w=1500.0),
        ],
        available_surplus_w=0.0,
        tracker=tracker,
        now=1000.0,
    )
    assert "miner" in result.to_stop
    assert "vp" in result.to_stop


# ── Test 9: All consumers off when no surplus and not running ─────────────────

def test_all_consumers_off_no_surplus_no_starts() -> None:
    """No consumers are started when surplus is 0."""
    tracker = _tracker()
    result = evaluate_cascade(
        [
            _consumer("miner", priority=6, is_running=False, min_power_w=500.0),
            _consumer("vp", priority=3, is_running=False, min_power_w=1500.0),
        ],
        available_surplus_w=0.0,
        tracker=tracker,
        now=1000.0,
    )
    assert result.to_start == []


# ── Test 10: Priority from consumer list ─────────────────────────────────────

def test_priority_ordering_correct() -> None:
    """Cascade respects priority order regardless of list order."""
    tracker = _tracker()
    # Pass consumers in reverse priority order
    result = evaluate_cascade(
        [
            _consumer("miner", priority=6, is_running=False, min_power_w=500.0),
            _consumer("ev", priority=2, is_running=False, min_power_w=500.0),
            _consumer("bat", priority=1, is_running=False, min_power_w=500.0),
        ],
        available_surplus_w=1100.0,  # Enough for bat + ev (2x500=1000), not miner
        tracker=tracker,
        now=1000.0,
    )
    assert "bat" in result.to_start
    assert "ev" in result.to_start
    assert "miner" not in result.to_start


# ── Tests for ConsumerStateTracker ────────────────────────────────────────────

def test_tracker_record_on_increments_cycles() -> None:
    """record_on increments cycles_today on each new ON event."""
    tracker = _tracker()
    tracker.record_on("miner", now=100.0)
    assert tracker.cycles_today("miner") == 1
    tracker.record_off("miner", now=200.0)
    tracker.record_on("miner", now=300.0)
    assert tracker.cycles_today("miner") == 2


def test_tracker_on_duration() -> None:
    """on_duration_s returns correct elapsed time."""
    tracker = _tracker()
    tracker.record_on("miner", now=100.0)
    assert tracker.on_duration_s("miner", now=700.0) == pytest.approx(600.0)


def test_tracker_off_duration() -> None:
    """off_duration_s returns correct elapsed time since OFF."""
    tracker = _tracker()
    tracker.record_on("miner", now=0.0)
    tracker.record_off("miner", now=100.0)
    assert tracker.off_duration_s("miner", now=800.0) == pytest.approx(700.0)


def test_tracker_can_turn_off_respects_min_on_time() -> None:
    """can_turn_off returns False if on_duration < min_on_time_s."""
    tracker = _tracker()
    tracker.record_on("miner", now=0.0)
    limits = WearLimits(min_on_time_s=1800)
    assert tracker.can_turn_off("miner", limits, now=100.0) is False
    assert tracker.can_turn_off("miner", limits, now=2000.0) is True


def test_tracker_can_turn_on_respects_daily_max() -> None:
    """can_turn_on returns False after daily_max_cycles."""
    tracker = _tracker()
    limits = WearLimits(daily_max_cycles=2)
    # Simulate 2 cycles
    for i in range(2):
        tracker.record_on("miner", now=float(i * 200))
        tracker.record_off("miner", now=float(i * 200 + 100))
    assert tracker.can_turn_on("miner", limits, now=500.0) is False


def test_tracker_reset_daily_counts() -> None:
    """reset_daily_counts clears cycles_today for all consumers."""
    tracker = _tracker()
    tracker.record_on("miner", now=0.0)
    tracker.record_off("miner", now=100.0)
    assert tracker.cycles_today("miner") == 1
    tracker.reset_daily_counts()
    assert tracker.cycles_today("miner") == 0


def test_tracker_persists_and_restores() -> None:
    """snapshot() + restore() round-trips state correctly."""
    tracker = _tracker()
    tracker.record_on("miner", now=100.0)
    tracker.record_off("miner", now=500.0)

    snap = tracker.snapshot()
    tracker2 = _tracker()
    tracker2.restore(snap)

    assert tracker2.cycles_today("miner") == 1
    assert tracker2.off_duration_s("miner", now=600.0) == pytest.approx(100.0)
    assert tracker2.is_on("miner") is False
