"""Unit tests for EV PV Surplus Controller — PLAT-1623.

Tests start/stop, ramp up/down, cloud stop, grid import protection.
All thresholds via named constants.
"""

from __future__ import annotations

from core.ev_surplus import EVSurplusConfig, EVSurplusController
from core.models import CommandType

# ---------------------------------------------------------------------------
# Named test constants
# ---------------------------------------------------------------------------

_MIN_AMPS: int = 6
_MAX_AMPS: int = 10
_PHASES: int = 3
_VOLTAGE: int = 230
_STEP_AMPS: int = 1

_MIN_SURPLUS_W: float = float(_MIN_AMPS * _PHASES * _VOLTAGE)  # 4140W
_RAMP_UP_EXPORT_W: float = 500.0
_RAMP_DOWN_IMPORT_W: float = 100.0
_CLOUD_STOP_W: float = 2000.0
_CLOUD_STOP_CYCLES: int = 3

_SURPLUS_5KW: float = 5000.0
_SURPLUS_3KW: float = 3000.0
_SURPLUS_1KW: float = 1000.0
_SURPLUS_6KW: float = 6000.0

_EXPORT_800W: float = -800.0  # negative = exporting
_IMPORT_200W: float = 200.0   # positive = importing
_IMPORT_500W: float = 500.0
_GRID_ZERO: float = 0.0

_EV_SOC_30: float = 30.0
_EV_SOC_80: float = 80.0
_EV_TARGET: float = 75.0


def _cfg() -> EVSurplusConfig:
    return EVSurplusConfig(
        min_amps=_MIN_AMPS,
        max_amps=_MAX_AMPS,
        phases=_PHASES,
        voltage_v=_VOLTAGE,
        step_amps=_STEP_AMPS,
        ramp_up_export_threshold_w=_RAMP_UP_EXPORT_W,
        ramp_down_import_threshold_w=_RAMP_DOWN_IMPORT_W,
        cloud_stop_surplus_w=_CLOUD_STOP_W,
        cloud_stop_cycles=_CLOUD_STOP_CYCLES,
    )


def _ctrl() -> EVSurplusController:
    return EVSurplusController(_cfg())


# ---------------------------------------------------------------------------
# Start tests
# ---------------------------------------------------------------------------


class TestEVSurplusStart:
    """Tests for EV surplus start logic."""

    def test_starts_at_min_amps(self) -> None:
        """Surplus >= min → start at min_amps."""
        ctrl = _ctrl()
        cmds = ctrl.evaluate(
            surplus_w=_SURPLUS_5KW,
            grid_power_w=_EXPORT_800W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )
        assert len(cmds) == 2
        assert cmds[0].command_type == CommandType.START_EV_CHARGING
        assert cmds[1].command_type == CommandType.SET_EV_CURRENT
        assert cmds[1].value == _MIN_AMPS

    def test_no_start_below_min_surplus(self) -> None:
        """Surplus < min → no start."""
        ctrl = _ctrl()
        cmds = ctrl.evaluate(
            surplus_w=_SURPLUS_3KW,
            grid_power_w=_EXPORT_800W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )
        assert len(cmds) == 0
        assert not ctrl.is_charging

    def test_no_start_ev_disconnected(self) -> None:
        """EV not connected → no start."""
        ctrl = _ctrl()
        cmds = ctrl.evaluate(
            surplus_w=_SURPLUS_5KW,
            grid_power_w=_EXPORT_800W,
            ev_connected=False,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )
        assert len(cmds) == 0

    def test_no_start_ev_at_target(self) -> None:
        """EV already at target → no start."""
        ctrl = _ctrl()
        cmds = ctrl.evaluate(
            surplus_w=_SURPLUS_5KW,
            grid_power_w=_EXPORT_800W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_80,
            ev_target_soc_pct=_EV_TARGET,
        )
        assert len(cmds) == 0


# ---------------------------------------------------------------------------
# Ramp tests
# ---------------------------------------------------------------------------


class TestEVSurplusRamp:
    """Tests for EV ramp up/down logic."""

    def _start_charging(self, ctrl: EVSurplusController) -> None:
        """Helper: start charging at min_amps."""
        ctrl.evaluate(
            surplus_w=_SURPLUS_5KW,
            grid_power_w=_EXPORT_800W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )
        assert ctrl.is_charging

    def test_ramp_up_on_export(self) -> None:
        """Exporting > threshold → ramp up 1A."""
        ctrl = _ctrl()
        self._start_charging(ctrl)

        cmds = ctrl.evaluate(
            surplus_w=_SURPLUS_6KW,
            grid_power_w=_EXPORT_800W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )
        assert len(cmds) == 1
        assert cmds[0].command_type == CommandType.SET_EV_CURRENT
        _EXPECTED_AMPS: int = _MIN_AMPS + _STEP_AMPS
        assert cmds[0].value == _EXPECTED_AMPS

    def test_ramp_down_on_import(self) -> None:
        """Importing > threshold → ramp down 1A."""
        ctrl = _ctrl()
        self._start_charging(ctrl)
        # First ramp up to 7A
        ctrl.evaluate(
            surplus_w=_SURPLUS_6KW,
            grid_power_w=_EXPORT_800W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )
        assert ctrl.current_amps == _MIN_AMPS + _STEP_AMPS

        # Now import → ramp down
        cmds = ctrl.evaluate(
            surplus_w=_SURPLUS_3KW,
            grid_power_w=_IMPORT_200W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )
        assert len(cmds) == 1
        assert cmds[0].value == _MIN_AMPS

    def test_never_below_min_amps(self) -> None:
        """Ramp down clamped at min_amps."""
        ctrl = _ctrl()
        self._start_charging(ctrl)

        # Import but already at min → no ramp command (or stop)
        _cmds = ctrl.evaluate(
            surplus_w=_SURPLUS_3KW,
            grid_power_w=_IMPORT_200W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )
        # Already at min, import not severe enough to stop
        assert ctrl.current_amps == _MIN_AMPS

    def test_never_above_max_amps(self) -> None:
        """Ramp up clamped at max_amps."""
        ctrl = _ctrl()
        self._start_charging(ctrl)

        # Ramp up multiple times
        for _ in range(_MAX_AMPS):
            ctrl.evaluate(
                surplus_w=_SURPLUS_6KW,
                grid_power_w=_EXPORT_800W,
                ev_connected=True,
                ev_soc_pct=_EV_SOC_30,
                ev_target_soc_pct=_EV_TARGET,
            )

        assert ctrl.current_amps == _MAX_AMPS


# ---------------------------------------------------------------------------
# Stop tests
# ---------------------------------------------------------------------------


class TestEVSurplusStop:
    """Tests for EV surplus stop logic."""

    def _start_charging(self, ctrl: EVSurplusController) -> None:
        ctrl.evaluate(
            surplus_w=_SURPLUS_5KW,
            grid_power_w=_EXPORT_800W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )

    def test_stops_on_sustained_low_surplus(self) -> None:
        """Low surplus for N cycles → stop."""
        ctrl = _ctrl()
        self._start_charging(ctrl)

        for _ in range(_CLOUD_STOP_CYCLES):
            cmds = ctrl.evaluate(
                surplus_w=_SURPLUS_1KW,
                grid_power_w=_GRID_ZERO,
                ev_connected=True,
                ev_soc_pct=_EV_SOC_30,
                ev_target_soc_pct=_EV_TARGET,
            )

        assert any(c.command_type == CommandType.STOP_EV_CHARGING for c in cmds)
        assert not ctrl.is_charging

    def test_stops_on_ev_disconnect(self) -> None:
        """EV disconnects mid-charge → stop."""
        ctrl = _ctrl()
        self._start_charging(ctrl)

        cmds = ctrl.evaluate(
            surplus_w=_SURPLUS_5KW,
            grid_power_w=_EXPORT_800W,
            ev_connected=False,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )
        assert any(c.command_type == CommandType.STOP_EV_CHARGING for c in cmds)

    def test_stops_on_ev_at_target(self) -> None:
        """EV reaches target → stop."""
        ctrl = _ctrl()
        self._start_charging(ctrl)

        cmds = ctrl.evaluate(
            surplus_w=_SURPLUS_5KW,
            grid_power_w=_EXPORT_800W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_80,
            ev_target_soc_pct=_EV_TARGET,
        )
        assert any(c.command_type == CommandType.STOP_EV_CHARGING for c in cmds)

    def test_low_surplus_counter_resets(self) -> None:
        """Good surplus resets low-surplus counter."""
        ctrl = _ctrl()
        self._start_charging(ctrl)

        # 2 low cycles (< cloud_stop_cycles)
        _CYCLES_BELOW_THRESHOLD: int = _CLOUD_STOP_CYCLES - 1
        for _ in range(_CYCLES_BELOW_THRESHOLD):
            ctrl.evaluate(
                surplus_w=_SURPLUS_1KW,
                grid_power_w=_GRID_ZERO,
                ev_connected=True,
                ev_soc_pct=_EV_SOC_30,
                ev_target_soc_pct=_EV_TARGET,
            )

        # Good surplus resets counter
        ctrl.evaluate(
            surplus_w=_SURPLUS_5KW,
            grid_power_w=_EXPORT_800W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )

        assert ctrl.is_charging  # Still charging

    def test_never_grid_import_for_ev(self) -> None:
        """EV never causes sustained grid import — stops at min amps + heavy import."""
        ctrl = _ctrl()
        self._start_charging(ctrl)

        cmds = ctrl.evaluate(
            surplus_w=_SURPLUS_1KW,
            grid_power_w=_IMPORT_500W,
            ev_connected=True,
            ev_soc_pct=_EV_SOC_30,
            ev_target_soc_pct=_EV_TARGET,
        )
        # At min amps with import > 2× threshold → stop
        assert any(c.command_type == CommandType.STOP_EV_CHARGING for c in cmds)
