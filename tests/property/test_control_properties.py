"""Property-based tests for control logic thresholds (PLAT-1598).

Uses Hypothesis to verify invariants hold across the full input space,
not just hand-picked test values.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from core.guards import (
    ExportGuard,
    GridGuard,
    GuardConfig,
    GuardLevel,
    GuardPolicy,
)
from core.models import Scenario
from tests.conftest import make_battery_state, make_grid_state, make_snapshot


# ---------------------------------------------------------------------------
# Named test constants
# ---------------------------------------------------------------------------
_MAX_GRID_W_FOR_TEST: float = 20_000.0
_EXTREME_GRID_W_MIN: float = 15_000.0
_EXTREME_GRID_W_MAX: float = 50_000.0
_MAX_EV_AMPS: int = 10
_MAX_HOUR: int = 23
_MIN_HOUR: int = 0
_MAX_WEIGHTED_AVG_KW: float = 15.0
_NEUTRAL_SPOT_PRICE_ORE: float = 50.0
_PV_NONE_KW: float = 0.0
_SOC_MIN_NORMAL: float = 20.0
_SOC_MAX_NORMAL: float = 95.0
_SOC_MIDRANGE: float = 60.0
_ZERO_GRID_W: float = 0.0
_ZERO_KW: float = 0.0
_ZERO_DATA_AGE_S: float = 0.0
_MIDDAY_HOUR: int = 12
_SOC_FULL_MIN: float = 0.0
_SOC_FULL_MAX: float = 100.0
_BATTERY_CAP_KWH: float = 15.0
_PV_FORECAST_KWH: float = 10.0
_W_TO_KW: float = 1000.0
_HYPOTHESIS_MAX_EXAMPLES: int = 100


def _evaluate_guard(
    grid_w: float = _ZERO_GRID_W,
    weighted_avg_kw: float = _ZERO_KW,
    hour: int = _MIDDAY_HOUR,
    soc_pct: float = _SOC_MIDRANGE,
    ha_connected: bool = True,
    data_age_s: float = _ZERO_DATA_AGE_S,
) -> GuardLevel:
    """Evaluate guard with given parameters, return level."""
    guard = GridGuard(GuardConfig())
    policy = GuardPolicy(guard, ExportGuard())
    bat = make_battery_state(soc_pct=soc_pct)
    snap = make_snapshot(
        hour=hour,
        batteries=[bat],
        grid=make_grid_state(
            grid_power_w=grid_w,
            weighted_avg_kw=weighted_avg_kw,
        ),
    )
    result = policy.evaluate(
        batteries=snap.batteries,
        current_scenario=Scenario.MIDDAY_CHARGE,
        weighted_avg_kw=weighted_avg_kw,
        hour=snap.hour,
        ha_connected=ha_connected,
        pv_kw=_PV_NONE_KW,
        spot_price_ore=_NEUTRAL_SPOT_PRICE_ORE,
        data_age_s=data_age_s,
    )
    return result.level


# ===========================================================================
# P1: Guard output never None
# ===========================================================================


class TestGuardOutputNeverNone:
    """P1: GuardPolicy.evaluate() always returns a valid level."""

    @settings(max_examples=_HYPOTHESIS_MAX_EXAMPLES, deadline=None)
    @given(
        grid_w=st.floats(
            min_value=0.0, max_value=_MAX_GRID_W_FOR_TEST, allow_nan=False,
        ),
        hour=st.integers(min_value=_MIN_HOUR, max_value=_MAX_HOUR),
    )
    def test_guard_output_never_none(self, grid_w: float, hour: int) -> None:
        level = _evaluate_guard(grid_w=grid_w, hour=hour)
        assert level is not None


# ===========================================================================
# P2: Guard level is valid enum
# ===========================================================================


class TestGuardLevelIsValidEnum:
    """P2: Result level is always a valid GuardLevel member."""

    @settings(max_examples=_HYPOTHESIS_MAX_EXAMPLES, deadline=None)
    @given(
        weighted_avg=st.floats(
            min_value=0.0, max_value=_MAX_WEIGHTED_AVG_KW, allow_nan=False,
        ),
    )
    def test_guard_level_is_valid_enum(self, weighted_avg: float) -> None:
        level = _evaluate_guard(weighted_avg_kw=weighted_avg)
        assert isinstance(level, GuardLevel)


# ===========================================================================
# P3: EV amps never exceed max
# ===========================================================================


class TestEvAmpsNeverExceedMax:
    """P3: Planner never outputs amps > max_amps."""

    @settings(max_examples=_HYPOTHESIS_MAX_EXAMPLES, deadline=None)
    @given(
        soc=st.floats(
            min_value=_SOC_FULL_MIN, max_value=_SOC_FULL_MAX, allow_nan=False,
        ),
    )
    def test_ev_amps_never_exceed_max(self, soc: float) -> None:
        from core.planner import Planner, PlannerConfig

        planner = Planner(PlannerConfig())
        plan = planner.generate_night_plan(
            bat_soc_pct=soc,
            bat_cap_kwh=_BATTERY_CAP_KWH,
            ev_connected=True,
            ev_soc_pct=soc,
            pv_tomorrow_kwh=_PV_FORECAST_KWH,
            prices_by_hour={},
        )
        assert plan.ev_amps <= _MAX_EV_AMPS


# ===========================================================================
# P4: Guard elevated on extreme grid
# ===========================================================================


class TestGuardFreezeOnExtremeGrid:
    """P4: Extreme grid import always triggers at least WARNING."""

    @settings(max_examples=_HYPOTHESIS_MAX_EXAMPLES, deadline=None)
    @given(
        grid_w=st.floats(
            min_value=_EXTREME_GRID_W_MIN,
            max_value=_EXTREME_GRID_W_MAX,
            allow_nan=False,
        ),
    )
    def test_guard_freeze_on_extreme_grid(self, grid_w: float) -> None:
        level = _evaluate_guard(
            grid_w=grid_w,
            weighted_avg_kw=grid_w / _W_TO_KW,
        )
        assert level != GuardLevel.OK, (
            f"Expected guard >= WARNING at {grid_w}W, got OK"
        )


# ===========================================================================
# P5: Guard OK on zero grid + normal SoC
# ===========================================================================


class TestGuardOkOnZeroGrid:
    """P5: Zero grid import + normal SoC → never FREEZE/ALARM."""

    @settings(max_examples=_HYPOTHESIS_MAX_EXAMPLES, deadline=None)
    @given(
        soc=st.floats(
            min_value=_SOC_MIN_NORMAL, max_value=_SOC_MAX_NORMAL,
            allow_nan=False,
        ),
    )
    def test_guard_ok_on_zero_grid(self, soc: float) -> None:
        level = _evaluate_guard(
            grid_w=_ZERO_GRID_W,
            weighted_avg_kw=_ZERO_GRID_W,
            soc_pct=soc,
        )
        assert level not in (GuardLevel.FREEZE, GuardLevel.ALARM), (
            f"Zero grid + SoC {soc}% should not trigger FREEZE/ALARM"
        )
