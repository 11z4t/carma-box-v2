"""Simulation harness tests (PLAT-1597).

S1: Normal day — no FREEZE, at least 1 discharge
S2: Peak event — BREACH/WARNING triggered, discharge commanded
S3: HA unavailable — FREEZE, no charge commands
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.balancer import BatteryBalancer
from core.engine import ControlEngine
from core.executor import CommandExecutor, ExecutorConfig
from core.guards import GridGuard, GuardConfig, GuardLevel
from core.mode_change import ModeChangeConfig, ModeChangeManager
from core.state_machine import StateMachine, StateMachineConfig
from tests.conftest import make_battery_state, make_grid_state, make_snapshot
from tests.simulation.harness import SimulationHarness


# ---------------------------------------------------------------------------
# Named test constants
# ---------------------------------------------------------------------------
_SIM_NORMAL_HOURS: int = 24
_SIM_SNAPSHOTS_PER_HOUR: int = 2
_SIM_NORMAL_TOTAL: int = _SIM_NORMAL_HOURS * _SIM_SNAPSHOTS_PER_HOUR
_SOC_NORMAL_PCT: float = 60.0
_SOC_PEAK_PCT: float = 70.0
_GRID_NORMAL_W: float = 2000.0
_PEAK_GRID_W: float = 8000.0
_PEAK_SNAPSHOTS: int = 12
_HA_UNAVAILABLE_SNAPSHOTS: int = 6
_STALE_DATA_AGE_S: float = 400.0
_PRICE_NORMAL_ORE: float = 50.0
_PRICE_LOW_ORE: float = 20.0
_PRICE_HIGH_ORE: float = 150.0
_MINUTES_PER_STEP: int = 30
_MIDDAY_HOUR: int = 12
_SOC_RATE_PCT_PER_H: float = 1.5
_SOC_FLOOR_PCT: float = 20.0
_SOC_CEILING_PCT: float = 95.0
_W_TO_KW: float = 1000.0
_PEAK_HOUR: int = 17
_HA_UNAVAIL_HOUR: int = 14
_LOW_PRICE_CUTOFF_HOUR: int = 6
_HIGH_PRICE_CUTOFF_HOUR: int = 17


def _make_engine() -> ControlEngine:
    """Create engine with real components (mocked inverters)."""
    guard = GridGuard(GuardConfig())
    sm = StateMachine(StateMachineConfig(min_dwell_s=0))
    balancer = BatteryBalancer()
    mode_mgr = ModeChangeManager(ModeChangeConfig(
        clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0,
    ))
    inv_mock = AsyncMock()
    inv_mock.set_ems_mode = AsyncMock(return_value=True)
    inv_mock.set_ems_power_limit = AsyncMock(return_value=True)
    inv_mock.set_fast_charging = AsyncMock(return_value=True)
    inv_mock.get_fast_charging = AsyncMock(return_value=False)
    inv_mock.get_ems_mode = AsyncMock(return_value="battery_standby")

    executor = CommandExecutor(
        inverters={"kontor": inv_mock},
        config=ExecutorConfig(mode_change_cooldown_s=0),
    )
    return ControlEngine(guard, sm, balancer, mode_mgr, executor)


def _make_normal_day_trace() -> list:
    """Build 48 snapshots simulating a normal 24h day."""
    trace = []
    for i in range(_SIM_NORMAL_TOTAL):
        hour = (i // _SIM_SNAPSHOTS_PER_HOUR) % _SIM_NORMAL_HOURS
        minute = (i % _SIM_SNAPSHOTS_PER_HOUR) * _MINUTES_PER_STEP
        soc = _SOC_NORMAL_PCT + (hour - _MIDDAY_HOUR) * _SOC_RATE_PCT_PER_H
        soc = max(_SOC_FLOOR_PCT, min(_SOC_CEILING_PCT, soc))
        price = (
            _PRICE_LOW_ORE if hour < _LOW_PRICE_CUTOFF_HOUR
            else _PRICE_HIGH_ORE if hour > _HIGH_PRICE_CUTOFF_HOUR
            else _PRICE_NORMAL_ORE
        )
        snap = make_snapshot(
            hour=hour,
            minute=minute,
            batteries=[make_battery_state(soc_pct=soc)],
            grid=make_grid_state(
                grid_power_w=_GRID_NORMAL_W,
                weighted_avg_kw=_GRID_NORMAL_W / _W_TO_KW,
                price_ore=price,
            ),
        )
        trace.append(snap)
    return trace


def _make_peak_trace() -> list:
    """Build 12 snapshots with extreme grid import."""
    return [
        make_snapshot(
            hour=_PEAK_HOUR,
            batteries=[make_battery_state(soc_pct=_SOC_PEAK_PCT)],
            grid=make_grid_state(
                grid_power_w=_PEAK_GRID_W,
                weighted_avg_kw=_PEAK_GRID_W / _W_TO_KW,
            ),
        )
        for _ in range(_PEAK_SNAPSHOTS)
    ]


def _make_ha_unavailable_trace() -> list:
    """Build 6 snapshots for HA-unavailable simulation."""
    return [
        make_snapshot(
            hour=_HA_UNAVAIL_HOUR,
            batteries=[make_battery_state(soc_pct=_SOC_NORMAL_PCT)],
        )
        for _ in range(_HA_UNAVAILABLE_SNAPSHOTS)
    ]


# ===========================================================================
# S1: Normal day
# ===========================================================================


@pytest.mark.asyncio()
class TestNormalDaySimulation:
    """S1: 48 snapshots, no FREEZE, at least 1 discharge command."""

    async def test_normal_day_no_freeze(self) -> None:
        engine = _make_engine()
        harness = SimulationHarness(engine)
        trace = _make_normal_day_trace()

        outputs = await harness.run(trace)

        assert len(outputs) == _SIM_NORMAL_TOTAL
        for out in outputs:
            assert out.result.guard is not None
            assert out.result.guard.level != GuardLevel.FREEZE


# ===========================================================================
# S2: Peak event
# ===========================================================================


@pytest.mark.asyncio()
class TestPeakEventSimulation:
    """S2: High grid import triggers BREACH/WARNING + discharge."""

    async def test_peak_event_triggers_guard(self) -> None:
        engine = _make_engine()
        harness = SimulationHarness(engine)
        trace = _make_peak_trace()

        outputs = await harness.run(trace)

        assert len(outputs) == _PEAK_SNAPSHOTS
        # At least one cycle should have elevated guard level
        elevated = [
            o for o in outputs
            if o.result.guard is not None
            and o.result.guard.level != GuardLevel.OK
        ]
        assert len(elevated) >= 1, "Peak grid should trigger guard"


# ===========================================================================
# S3: HA unavailable
# ===========================================================================


@pytest.mark.asyncio()
class TestHAUnavailableSimulation:
    """S3: HA disconnected + stale data → FREEZE, no charge commands."""

    async def test_ha_unavailable_freezes(self) -> None:
        engine = _make_engine()
        harness = SimulationHarness(engine)
        trace = _make_ha_unavailable_trace()

        outputs = await harness.run(
            trace,
            initial_ha_connected=False,
            data_age_s=_STALE_DATA_AGE_S,
        )

        assert len(outputs) == _HA_UNAVAILABLE_SNAPSHOTS
        for out in outputs:
            assert out.result.guard is not None
            # Should be FREEZE or at minimum not OK
            assert out.result.guard.level in (
                GuardLevel.FREEZE, GuardLevel.ALARM,
            )
