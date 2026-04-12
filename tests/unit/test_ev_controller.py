"""Tests for EV Controller (Ramp Logic).

Covers:
- Ramp up sequence 6→8→10
- Ramp down sequence 10→8→6→stop
- Cooldown blocks early action
- Emergency cut at Ellevio breach
- XPENG SoC=-1 fallback
- Start at 6A always (never jump)
- At target → stop
- Waiting in fully → fix (B3)
- Regression B6: never jump to 16A
"""

from __future__ import annotations


import pytest

from core.ev_controller import (
    EVAction,
    EVController,
    EVControllerConfig,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctrl() -> EVController:
    """Controller with no cooldowns for testing."""
    return EVController(EVControllerConfig(
        step_interval_s=0,
        cooldown_after_start_s=0,
        cooldown_after_stop_s=0,
    ))


# ===========================================================================
# Ramp up
# ===========================================================================


class TestRampUp:
    """Ramp up sequence: 6→8→10."""

    def test_ramp_6_to_8(self, ctrl: EVController) -> None:
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=6, grid_import_w=500, ellevio_headroom_w=2000,
        )
        assert result.action == EVAction.SET_CURRENT
        assert result.target_amps == 8

    def test_ramp_8_to_10(self, ctrl: EVController) -> None:
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=8, grid_import_w=500, ellevio_headroom_w=2000,
        )
        assert result.action == EVAction.SET_CURRENT
        assert result.target_amps == 10

    def test_at_max_no_ramp(self, ctrl: EVController) -> None:
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=10, grid_import_w=500, ellevio_headroom_w=2000,
        )
        assert result.action == EVAction.NO_CHANGE

    def test_never_jump_above_max(self, ctrl: EVController) -> None:
        """B6 regression: never jump to 16A or any value above max_amps."""
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=10, grid_import_w=0, ellevio_headroom_w=5000,
        )
        # Should not produce any current above 10A
        if result.action == EVAction.SET_CURRENT:
            assert result.target_amps <= 10


# ===========================================================================
# Ramp down
# ===========================================================================


class TestRampDown:
    """Ramp down: 10→8→6→stop."""

    def test_ramp_10_to_8(self, ctrl: EVController) -> None:
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=10, grid_import_w=4000, ellevio_headroom_w=-500,
        )
        assert result.action == EVAction.SET_CURRENT
        assert result.target_amps == 8

    def test_ramp_8_to_6(self, ctrl: EVController) -> None:
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=8, grid_import_w=4000, ellevio_headroom_w=-500,
        )
        assert result.action == EVAction.SET_CURRENT
        assert result.target_amps == 6

    def test_at_min_stop(self, ctrl: EVController) -> None:
        """At 6A with negative headroom → stop."""
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=6, grid_import_w=5000, ellevio_headroom_w=-500,
        )
        assert result.action == EVAction.STOP


# ===========================================================================
# Cooldown
# ===========================================================================


class TestCooldown:
    """Cooldown timers block early action."""

    def test_start_cooldown_blocks_restart(self) -> None:
        """After stop, must wait cooldown_after_stop_s before restart."""
        ctrl = EVController(EVControllerConfig(
            cooldown_after_stop_s=300.0,
            step_interval_s=0,
            cooldown_after_start_s=0,
        ))
        ctrl.timers.record_stop()

        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=False,
            current_amps=0, grid_import_w=0, ellevio_headroom_w=5000,
        )
        assert result.action == EVAction.NO_CHANGE
        assert "cooldown" in result.reason.lower()

    def test_ramp_cooldown_blocks_change(self) -> None:
        """After ramp, must wait step_interval_s before next change."""
        ctrl = EVController(EVControllerConfig(
            step_interval_s=300.0,
            cooldown_after_start_s=0,
            cooldown_after_stop_s=0,
        ))
        ctrl.timers.record_ramp()

        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=6, grid_import_w=0, ellevio_headroom_w=5000,
        )
        assert result.action == EVAction.NO_CHANGE


# ===========================================================================
# Emergency cut
# ===========================================================================


class TestEmergencyCut:
    """Emergency cut at Ellevio breach."""

    def test_emergency_cut_at_severe_breach(self, ctrl: EVController) -> None:
        """Emergency cut fires at > 1kW over Ellevio tak."""
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=10, grid_import_w=7000, ellevio_headroom_w=-1500,
        )
        assert result.action == EVAction.EMERGENCY_CUT
        assert result.target_amps == 6


# ===========================================================================
# XPENG SoC fallback
# ===========================================================================


class TestXpengSocFallback:
    """XPENG G9 SoC=-1 uses last known value."""

    def test_normal_soc_stored(self, ctrl: EVController) -> None:
        ctrl.evaluate(
            ev_connected=True, ev_soc_pct=65.0, charging=True,
            current_amps=6, grid_import_w=0, ellevio_headroom_w=5000,
        )
        assert ctrl._last_known_soc == 65.0

    def test_negative_soc_uses_fallback(self, ctrl: EVController) -> None:
        """SoC=-1 should use last known value."""
        # First set a valid SoC
        ctrl.evaluate(
            ev_connected=True, ev_soc_pct=70.0, charging=True,
            current_amps=6, grid_import_w=0, ellevio_headroom_w=5000,
        )
        # Now simulate XPENG sleep
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=-1.0, charging=True,
            current_amps=6, grid_import_w=0, ellevio_headroom_w=5000,
        )
        # Should NOT stop (70% < 75% target)
        assert result.action != EVAction.STOP


# ===========================================================================
# Start always at 6A
# ===========================================================================


class TestStartAt6A:
    """ALWAYS start at 6A, never jump to higher."""

    def test_start_at_start_amps(self, ctrl: EVController) -> None:
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=False,
            current_amps=0, grid_import_w=0, ellevio_headroom_w=5000,
        )
        assert result.action == EVAction.START
        assert result.target_amps == 6


# ===========================================================================
# At target → stop
# ===========================================================================


class TestAtTarget:
    """At or above target SoC → stop charging."""

    def test_at_target_stops(self, ctrl: EVController) -> None:
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=75.0, charging=True,
            current_amps=10, grid_import_w=0, ellevio_headroom_w=5000,
        )
        assert result.action == EVAction.STOP

    def test_above_target_stops(self, ctrl: EVController) -> None:
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=90.0, charging=True,
            current_amps=8, grid_import_w=0, ellevio_headroom_w=5000,
        )
        assert result.action == EVAction.STOP


# ===========================================================================
# Waiting in fully (B3)
# ===========================================================================


class TestWaitingInFully:
    """B3: waiting_in_fully should trigger fix."""

    def test_triggers_fix(self, ctrl: EVController) -> None:
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=False,
            current_amps=0, grid_import_w=0, ellevio_headroom_w=5000,
            reason_for_no_current="waiting_in_fully",
        )
        assert result.action == EVAction.FIX_WAITING_IN_FULLY


# ===========================================================================
# Not connected
# ===========================================================================


class TestNotConnected:
    """EV not connected → no change."""

    def test_not_connected_no_action(self, ctrl: EVController) -> None:
        result = ctrl.evaluate(
            ev_connected=False, ev_soc_pct=50.0, charging=False,
            current_amps=0, grid_import_w=0, ellevio_headroom_w=5000,
        )
        assert result.action == EVAction.NO_CHANGE
