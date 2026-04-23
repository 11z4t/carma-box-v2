"""Tests for core/bat_smoother.py — night bat smoothing (PLAT-1790 R-natt-bat).

Tests cover Alt B smoothing logic: cap, SoC floor, transient detection, config flag.
"""

from __future__ import annotations

from core.bat_smoother import compute_night_smoothing


# ── Test 1: transient within cap → bat covers exactly ───────────────────────

def test_night_bat_smoothing_covers_grid_transient_within_cap() -> None:
    """Grid transient 400 W < 500 W cap → bat provides exactly 400 W."""
    result = compute_night_smoothing(
        grid_transient_w=400.0,
        bat_soc_pct=90.0,
        cap_w=500,
        soc_floor_pct=80.0,
        enabled=True,
    )
    assert result == 400.0


# ── Test 2: transient exceeds cap → bat capped at 500 W ─────────────────────

def test_night_bat_smoothing_caps_at_500w() -> None:
    """Grid transient 800 W > 500 W cap → bat provides 500 W, grid takes the rest."""
    result = compute_night_smoothing(
        grid_transient_w=800.0,
        bat_soc_pct=95.0,
        cap_w=500,
        soc_floor_pct=80.0,
        enabled=True,
    )
    assert result == 500.0


# ── Test 3: bat SoC at floor → smoothing disabled ────────────────────────────

def test_night_bat_smoothing_stops_at_soc_floor() -> None:
    """bat_soc exactly at soc_floor_pct (80%) → smoothing disabled, returns 0."""
    result = compute_night_smoothing(
        grid_transient_w=400.0,
        bat_soc_pct=80.0,   # at floor — not above it
        cap_w=500,
        soc_floor_pct=80.0,
        enabled=True,
    )
    assert result == 0.0


def test_night_bat_smoothing_stops_below_soc_floor() -> None:
    """bat_soc below soc_floor_pct → smoothing disabled, returns 0."""
    result = compute_night_smoothing(
        grid_transient_w=400.0,
        bat_soc_pct=75.0,   # below floor
        cap_w=500,
        soc_floor_pct=80.0,
        enabled=True,
    )
    assert result == 0.0


# ── Test 4: transient passes (zero) → bat back to idle ──────────────────────

def test_night_bat_smoothing_recovers_after_transient() -> None:
    """grid_transient_w=0 (transient resolved) → bat returns to idle (0 W)."""
    result = compute_night_smoothing(
        grid_transient_w=0.0,
        bat_soc_pct=90.0,
        cap_w=500,
        soc_floor_pct=80.0,
        enabled=True,
    )
    assert result == 0.0


# ── Test 5: smoothing disabled via config ────────────────────────────────────

def test_night_bat_no_smoothing_when_config_disabled() -> None:
    """enabled=False → bat idle regardless of transient or SoC."""
    result = compute_night_smoothing(
        grid_transient_w=600.0,  # large transient
        bat_soc_pct=99.0,        # bat nearly full — but config says no
        cap_w=500,
        soc_floor_pct=80.0,
        enabled=False,
    )
    assert result == 0.0
