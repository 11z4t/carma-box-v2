"""Tests for core.grid_tuner (PLAT-1737).

Tiered grid-sensor controller: direkt bat-tuning vid ±50/75/100 W samt
5 min rolling average för mode-change-stabilitet (anti-flap).

Pure function — no I/O. Deterministic.
"""

from __future__ import annotations

from collections import deque

from core.grid_tuner import (
    GridRollingState,
    GridTunerConfig,
    should_block_mode_change,
    tune_grid_delta,
)


_DISABLED = GridTunerConfig(enabled=False)
_DEFAULT = GridTunerConfig(
    enabled=True,
    tiers_w=(50.0, 75.0, 100.0),
    corrections_w=(100, 300, 500),
    rolling_window_s=300,
    mode_change_stability_w=50.0,
)


# ---------------------------------------------------------------------------
# tune_grid_delta — tiered response
# ---------------------------------------------------------------------------


def test_disabled_returns_zero() -> None:
    """Feature flag off → always zero delta."""
    assert tune_grid_delta(500.0, _DISABLED) == 0
    assert tune_grid_delta(-2000.0, _DISABLED) == 0
    assert tune_grid_delta(0.0, _DISABLED) == 0


def test_deadband_below_tier1_returns_zero() -> None:
    """|grid| < tier1 (50 W) → no correction (dödband)."""
    assert tune_grid_delta(0.0, _DEFAULT) == 0
    assert tune_grid_delta(30.0, _DEFAULT) == 0
    assert tune_grid_delta(-49.9, _DEFAULT) == 0


def test_tier1_band_applies_first_correction() -> None:
    """50 ≤ |grid| < 75 → ±100 W correction."""
    # grid import → positive delta (bat should discharge more / charge less)
    assert tune_grid_delta(50.0, _DEFAULT) == 100
    assert tune_grid_delta(70.0, _DEFAULT) == 100
    # grid export → negative delta (bat should charge more / discharge less)
    assert tune_grid_delta(-50.0, _DEFAULT) == -100
    assert tune_grid_delta(-74.9, _DEFAULT) == -100


def test_tier2_band_applies_second_correction() -> None:
    """75 ≤ |grid| < 100 → ±300 W correction."""
    assert tune_grid_delta(75.0, _DEFAULT) == 300
    assert tune_grid_delta(99.0, _DEFAULT) == 300
    assert tune_grid_delta(-80.0, _DEFAULT) == -300
    assert tune_grid_delta(-99.9, _DEFAULT) == -300


def test_tier3_band_applies_max_correction() -> None:
    """|grid| ≥ 100 → ±500 W correction (no upper clamp in tuner itself —
    caller clamps to bat physical max)."""
    assert tune_grid_delta(100.0, _DEFAULT) == 500
    assert tune_grid_delta(500.0, _DEFAULT) == 500
    assert tune_grid_delta(5000.0, _DEFAULT) == 500
    assert tune_grid_delta(-100.0, _DEFAULT) == -500
    assert tune_grid_delta(-2000.0, _DEFAULT) == -500


def test_sign_convention_import_positive() -> None:
    """Import (grid_w > 0) must produce positive delta — bat should raise
    net power (discharge more or charge less) to drive grid toward 0."""
    for g in (60.0, 80.0, 500.0):
        assert tune_grid_delta(g, _DEFAULT) > 0, f"grid={g}W should yield positive delta"


def test_sign_convention_export_negative() -> None:
    """Export (grid_w < 0) must produce negative delta — bat should lower
    net power (charge more or discharge less)."""
    for g in (-60.0, -80.0, -500.0):
        assert tune_grid_delta(g, _DEFAULT) < 0, f"grid={g}W should yield negative delta"


def test_custom_tiers_honoured() -> None:
    """Tiers and corrections come from config — no magic numbers."""
    cfg = GridTunerConfig(
        enabled=True,
        tiers_w=(100.0, 250.0, 500.0),
        corrections_w=(50, 150, 400),
        rolling_window_s=300,
        mode_change_stability_w=80.0,
    )
    assert tune_grid_delta(80.0, cfg) == 0    # below tier1
    assert tune_grid_delta(100.0, cfg) == 50  # tier1
    assert tune_grid_delta(260.0, cfg) == 150 # tier2
    assert tune_grid_delta(600.0, cfg) == 400 # tier3


def test_tiers_must_be_strictly_increasing_at_runtime() -> None:
    """If caller passes non-increasing tiers the result is still defined:
    highest tier the grid exceeds wins (naïve-but-safe)."""
    cfg = GridTunerConfig(
        enabled=True,
        tiers_w=(100.0, 100.0, 100.0),
        corrections_w=(50, 150, 400),
        rolling_window_s=300,
        mode_change_stability_w=50.0,
    )
    # all three tiers hit at |grid|>=100 → last correction wins
    assert tune_grid_delta(100.0, cfg) == 400


# ---------------------------------------------------------------------------
# GridRollingState — 5-min rolling window
# ---------------------------------------------------------------------------


def test_rolling_state_empty_avg_is_zero() -> None:
    s = GridRollingState(history=deque())
    assert s.avg() == 0.0


def test_rolling_state_accumulates_samples() -> None:
    s = GridRollingState(history=deque())
    s.add(0.0, 100.0, window_s=300)
    s.add(10.0, 200.0, window_s=300)
    s.add(20.0, 300.0, window_s=300)
    assert s.avg() == 200.0


def test_rolling_state_prunes_samples_outside_window() -> None:
    """Samples older than window_s are pruned when new one arrives."""
    s = GridRollingState(history=deque())
    s.add(0.0, 100.0, window_s=300)
    s.add(100.0, 200.0, window_s=300)
    s.add(301.0, 300.0, window_s=300)  # first sample now > 300 s old → pruned
    assert len(s.history) == 2
    assert s.avg() == 250.0


def test_rolling_state_handles_monotonic_jumps() -> None:
    """Non-uniform timestamps are OK — avg is plain arithmetic mean of
    surviving samples."""
    s = GridRollingState(history=deque())
    for ts, gw in [(0.0, 100.0), (50.0, -50.0), (200.0, 150.0)]:
        s.add(ts, gw, window_s=300)
    assert abs(s.avg() - 66.666666) < 0.001


# ---------------------------------------------------------------------------
# should_block_mode_change — anti-flap guard
# ---------------------------------------------------------------------------


def test_mode_change_blocked_when_rolling_avg_within_stability_band() -> None:
    """Rolling avg within ±mode_change_stability_w → mode is stable, block
    any charge↔discharge flip."""
    assert should_block_mode_change(0.0, _DEFAULT) is True
    assert should_block_mode_change(40.0, _DEFAULT) is True
    assert should_block_mode_change(-40.0, _DEFAULT) is True


def test_mode_change_allowed_when_rolling_avg_outside_band() -> None:
    """Rolling avg > stability_w → trend is real, mode-change allowed."""
    assert should_block_mode_change(100.0, _DEFAULT) is False
    assert should_block_mode_change(-100.0, _DEFAULT) is False


def test_mode_change_boundary_is_strict() -> None:
    """Exactly at stability_w → NOT blocked (strict inequality)."""
    assert should_block_mode_change(50.0, _DEFAULT) is False
    assert should_block_mode_change(-50.0, _DEFAULT) is False


def test_mode_change_respects_config_stability_width() -> None:
    """Custom stability width must be honoured."""
    cfg = GridTunerConfig(
        enabled=True,
        tiers_w=(50.0, 75.0, 100.0),
        corrections_w=(100, 300, 500),
        rolling_window_s=300,
        mode_change_stability_w=150.0,
    )
    assert should_block_mode_change(100.0, cfg) is True
    assert should_block_mode_change(149.9, cfg) is True
    assert should_block_mode_change(150.0, cfg) is False


# ---------------------------------------------------------------------------
# Determinism / purity
# ---------------------------------------------------------------------------


def test_tune_is_deterministic() -> None:
    """Same input always produces same output (pure function)."""
    for g in (-500.0, -75.0, 0.0, 60.0, 250.0):
        a = tune_grid_delta(g, _DEFAULT)
        b = tune_grid_delta(g, _DEFAULT)
        assert a == b


def test_config_is_frozen_dataclass() -> None:
    """GridTunerConfig immutable — safe to share across threads/cycles."""
    import dataclasses
    assert dataclasses.is_dataclass(GridTunerConfig)
    # attempt to mutate raises FrozenInstanceError
    cfg = GridTunerConfig(enabled=True)
    try:
        cfg.enabled = False  # type: ignore[misc]
        raise AssertionError("GridTunerConfig should be frozen")
    except dataclasses.FrozenInstanceError:
        pass
