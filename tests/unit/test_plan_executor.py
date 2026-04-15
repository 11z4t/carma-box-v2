"""Tests for core/plan_executor.py (PLAT-1568).

Covers:
- GuardPolicy ALARM → plan skipped
- GuardPolicy FREEZE → plan skipped
- Normal cycle → active_night_plan updated
- Normal cycle → active_evening_plan updated
- generate_48h returns correct structure
- Specific exception handling (PLAT-1563): exc_info=True
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.schema import load_config
from core.guards import GuardEvaluation, GuardLevel, GuardPolicy
from core.models import (
    BatteryState,
    CTPlacement,
    EMSMode,
    EVState,
    GridState,
    Scenario,
    SystemSnapshot,
)
from core.plan_executor import PlanExecutor
from core.planner import EveningPlan, NightPlan, Planner, PlannerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONFIG_PATH = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")

# Named test constants
_NIGHT_PLAN_HOUR: int = 22
_EVENING_PLAN_HOUR: int = 17
_MORNING_PLAN_HOUR: int = 6
_NON_PLAN_HOUR: int = 10
_SOC_NOMINAL_PCT: float = 60.0
_SOC_EV_PCT: float = 50.0
_TEST_PV_W: float = 3000.0
_TEST_GRID_W: float = 500.0
_TEST_LOAD_W: float = 1000.0
_TEST_PRICE_ORE: float = 45.0
_TEST_WEIGHTED_AVG_KW: float = 1.2
_TEST_PEAK_KW: float = 1.5
_TEST_TAK_KW: float = 2.0
_TEST_PV_TODAY_KWH: float = 20.0
_TEST_PV_TOMORROW_KWH: float = 25.0
_TEST_BAT_CAP_KWH: float = 15.0
_TEST_HOUR_AFTER_MIDNIGHT: int = 23
_MIDNIGHT_HOUR: int = 0
_TEST_HOUR_EARLY_1: int = 1
_TEST_HOUR_EARLY_2: int = 2
_TEST_PRICE_MID_ORE: float = 50.0
_TEST_PRICE_CHEAP_ORE: float = 30.0
_TEST_PRICE_EXP_ORE: float = 80.0


def _make_snapshot(hour: int, bat_soc: float = _SOC_NOMINAL_PCT) -> SystemSnapshot:
    """Build a minimal SystemSnapshot for a given hour."""
    bat = BatteryState(
        battery_id="kontor",
        soc_pct=bat_soc,
        power_w=0.0,
        cell_temp_c=25.0,
        pv_power_w=0.0,
        grid_power_w=500.0,
        load_power_w=1000.0,
        ems_mode=EMSMode.CHARGE_PV,
        ems_power_limit_w=0,
        fast_charging=False,
        soh_pct=98.0,
        cap_kwh=15.0,
        ct_placement=CTPlacement.LOCAL_LOAD,
    )
    ev = EVState(
        connected=True,
        soc_pct=_SOC_EV_PCT,
        charging=False,
        power_w=0.0,
        current_a=0.0,
        charger_status="awaiting_start",
    )
    grid = GridState(
        grid_power_w=500.0,
        pv_total_w=3000.0,
        price_ore=45.0,
        weighted_avg_kw=1.2,
        current_peak_kw=1.5,
        dynamic_tak_kw=2.0,
        pv_forecast_today_kwh=20.0,
        pv_forecast_tomorrow_kwh=25.0,
    )
    return SystemSnapshot(
        timestamp=datetime(2026, 4, 15, hour, 5, 0, tzinfo=timezone.utc),
        hour=hour,
        minute=5,
        batteries=[bat],
        ev=ev,
        grid=grid,
        consumers=[],
        current_scenario=Scenario.PV_SURPLUS_DAY,
    )


def _make_executor(
    guard_level: GuardLevel = GuardLevel.OK,
) -> tuple[PlanExecutor, MagicMock]:
    """Create PlanExecutor with mocked dependencies."""
    cfg = load_config(_CONFIG_PATH)
    planner = Planner(PlannerConfig())

    mock_guard = MagicMock(spec=GuardPolicy)
    mock_guard.evaluate.return_value = GuardEvaluation(level=guard_level)

    mock_ha = AsyncMock()
    mock_ha.set_input_text = AsyncMock()

    executor = PlanExecutor(
        planner=planner,
        ha_api=mock_ha,
        config=cfg,
        guard_policy=mock_guard,
    )
    return executor, mock_guard


# ---------------------------------------------------------------------------
# Guard blocks plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestGuardBlocking:
    """GuardPolicy ALARM/FREEZE → plan generation skipped."""

    async def test_alarm_blocks_plan(self) -> None:
        """ALARM level should prevent plan generation."""
        executor, guard = _make_executor(GuardLevel.ALARM)
        snap = _make_snapshot(hour=_NIGHT_PLAN_HOUR)
        await executor.generate(snap)
        assert executor.active_night_plan is None

    async def test_freeze_blocks_plan(self) -> None:
        """FREEZE level should prevent plan generation."""
        executor, guard = _make_executor(GuardLevel.FREEZE)
        snap = _make_snapshot(hour=_NIGHT_PLAN_HOUR)
        await executor.generate(snap)
        assert executor.active_night_plan is None

    async def test_ok_allows_plan(self) -> None:
        """OK level should allow plan generation."""
        executor, guard = _make_executor(GuardLevel.OK)
        snap = _make_snapshot(hour=_NIGHT_PLAN_HOUR)
        await executor.generate(snap)
        assert executor.active_night_plan is not None

    async def test_warning_allows_plan(self) -> None:
        """WARNING level should allow plan generation."""
        executor, guard = _make_executor(GuardLevel.WARNING)
        snap = _make_snapshot(hour=_EVENING_PLAN_HOUR)
        await executor.generate(snap)
        assert executor.active_evening_plan is not None


# ---------------------------------------------------------------------------
# Normal cycle → plan updated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestNormalCycle:
    """Plan generation at configured hours updates active plans."""

    async def test_night_plan_at_22(self) -> None:
        """Hour 22 should generate and store a NightPlan."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=_NIGHT_PLAN_HOUR)
        await executor.generate(snap)
        plan = executor.active_night_plan
        assert isinstance(plan, NightPlan)
        assert plan.ev_charge_need_kwh >= 0.0

    async def test_evening_plan_at_17(self) -> None:
        """Hour 17 should generate and store an EveningPlan."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=_EVENING_PLAN_HOUR)
        await executor.generate(snap)
        plan = executor.active_evening_plan
        assert isinstance(plan, EveningPlan)
        assert plan.bat_available_kwh >= 0.0

    async def test_morning_no_plan_stored(self) -> None:
        """Hour 6 logs snapshot but doesn't create night/evening plan."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=_MORNING_PLAN_HOUR)
        await executor.generate(snap)
        assert executor.active_night_plan is None
        assert executor.active_evening_plan is None

    async def test_non_plan_hour_noop(self) -> None:
        """Hour 10 is not a plan hour — no plan generated."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=_NON_PLAN_HOUR)
        await executor.generate(snap)
        assert executor.active_night_plan is None
        assert executor.active_evening_plan is None


# ---------------------------------------------------------------------------
# generate_48h
# ---------------------------------------------------------------------------


class TestGenerate48h:
    """generate_48h returns correct pipe-separated structure."""

    def test_returns_two_strings(self) -> None:
        """Should return (today_plan, tomorrow_plan) tuple."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=_NIGHT_PLAN_HOUR)
        today, tomorrow = executor.generate_48h(snap, current_hour=_NIGHT_PLAN_HOUR)
        assert isinstance(today, str)
        assert isinstance(tomorrow, str)

    def test_entries_have_correct_format(self) -> None:
        """Each entry should be HH:ACTION:SoC%."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=_NIGHT_PLAN_HOUR)
        today, tomorrow = executor.generate_48h(snap, current_hour=_NIGHT_PLAN_HOUR)

        for plan_str in (today, tomorrow):
            if not plan_str:
                continue
            entries = plan_str.split("|")
            for entry in entries:
                parts = entry.split(":")
                assert len(parts) == 3, f"Bad entry format: {entry}"
                hh, action, soc = parts
                assert len(hh) == 2 and hh.isdigit(), f"Bad hour: {hh}"
                assert action in ("EV", "GRD", "CHG", "DIS", "STB"), (
                    f"Unknown action: {action}"
                )
                assert soc.endswith("%"), f"SoC missing %: {soc}"

    def test_covers_48_hours(self) -> None:
        """Combined entries should cover 48 hours."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=_NIGHT_PLAN_HOUR)
        today, tomorrow = executor.generate_48h(snap, current_hour=_NIGHT_PLAN_HOUR)
        today_count = len(today.split("|")) if today else 0
        tomorrow_count = len(tomorrow.split("|")) if tomorrow else 0
        assert today_count + tomorrow_count == 48


# ---------------------------------------------------------------------------
# PLAT-1563: Specific exceptions with exc_info=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestExceptionHandling:
    """Specific exceptions are caught with exc_info=True."""

    async def test_value_error_logged_with_exc_info(self) -> None:
        """ValueError should be caught and logged with exc_info=True."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=_NIGHT_PLAN_HOUR)

        # Force ValueError in planner
        with patch.object(
            executor._planner,
            "generate_night_plan",
            side_effect=ValueError("bad data"),
        ):
            with patch("core.plan_executor.logger") as mock_logger:
                await executor.generate(snap)
                mock_logger.error.assert_called_once()
                call_kwargs = mock_logger.error.call_args
                assert call_kwargs[1].get("exc_info") is True

    async def test_key_error_logged_with_exc_info(self) -> None:
        """KeyError should be caught and logged with exc_info=True."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=_EVENING_PLAN_HOUR)

        with patch.object(
            executor._planner,
            "generate_evening_plan",
            side_effect=KeyError("missing_key"),
        ):
            with patch("core.plan_executor.logger") as mock_logger:
                await executor.generate(snap)
                mock_logger.error.assert_called_once()
                call_kwargs = mock_logger.error.call_args
                assert call_kwargs[1].get("exc_info") is True

    async def test_timeout_error_logged_with_exc_info(self) -> None:
        """asyncio.TimeoutError should be caught and logged with exc_info."""
        import asyncio

        executor, _ = _make_executor()
        snap = _make_snapshot(hour=_NIGHT_PLAN_HOUR)

        with patch.object(
            executor._planner,
            "generate_night_plan",
            side_effect=asyncio.TimeoutError(),
        ):
            with patch("core.plan_executor.logger") as mock_logger:
                await executor.generate(snap)
                mock_logger.error.assert_called_once()
                call_kwargs = mock_logger.error.call_args
                assert call_kwargs[1].get("exc_info") is True


# ---------------------------------------------------------------------------
# PLAT-1581: C6+C7+C8 — FREEZE skip + sentinel price + skip logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestFreezeSkipsPlan:
    """C6: Engine skips planner call when FREEZE guard active."""

    async def test_engine_skips_plan_on_freeze(self) -> None:
        """FREEZE guard → planner NOT called."""
        executor, _ = _make_executor(GuardLevel.FREEZE)
        snap = _make_snapshot(hour=_NIGHT_PLAN_HOUR)

        with patch.object(
            executor._planner, "generate_night_plan",
        ) as mock_plan:
            await executor.generate(snap)
            mock_plan.assert_not_called()

    async def test_engine_logs_skip_reason(self) -> None:
        """C8: Skip reason logged with guard level and violations."""
        executor, _ = _make_executor(GuardLevel.FREEZE)
        snap = _make_snapshot(hour=_NIGHT_PLAN_HOUR)

        with patch("core.plan_executor.logger") as mock_logger:
            await executor.generate(snap)
            mock_logger.warning.assert_called_once()
            log_msg = mock_logger.warning.call_args[0][0]
            assert "SKIP" in log_msg
            assert "FREEZE" in log_msg


class TestPlannerFallbackPrice:
    """C7: Missing price data uses _PRICE_SORT_SENTINEL_ORE."""

    def test_missing_hours_sorted_last(self) -> None:
        """Hours without price data should sort last (highest sentinel)."""
        from core.planner import Planner

        planner = Planner()
        hours = [
            _NIGHT_PLAN_HOUR, _TEST_HOUR_AFTER_MIDNIGHT,
            _MIDNIGHT_HOUR, _TEST_HOUR_EARLY_1, _TEST_HOUR_EARLY_2,
        ]
        prices = {
            _NIGHT_PLAN_HOUR: _TEST_PRICE_MID_ORE,
            _MIDNIGHT_HOUR: _TEST_PRICE_CHEAP_ORE,
            _TEST_HOUR_EARLY_2: _TEST_PRICE_EXP_ORE,
        }  # _TEST_HOUR_AFTER_MIDNIGHT and _TEST_HOUR_EARLY_1 missing
        result = planner._sort_by_cheapest(hours, prices)
        # Cheapest first, missing hours last (sentinel price)
        assert result[0] == _MIDNIGHT_HOUR
        assert result[1] == _NIGHT_PLAN_HOUR
        assert result[2] == _TEST_HOUR_EARLY_2
        assert set(result[3:]) == {_TEST_HOUR_AFTER_MIDNIGHT, _TEST_HOUR_EARLY_1}
