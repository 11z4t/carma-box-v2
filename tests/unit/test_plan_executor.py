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


def _make_snapshot(hour: int, bat_soc: float = 60.0) -> SystemSnapshot:
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
        soc_pct=50.0,
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
        current_scenario=Scenario.MIDDAY_CHARGE,
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
        snap = _make_snapshot(hour=22)
        await executor.generate(snap)
        assert executor.active_night_plan is None

    async def test_freeze_blocks_plan(self) -> None:
        """FREEZE level should prevent plan generation."""
        executor, guard = _make_executor(GuardLevel.FREEZE)
        snap = _make_snapshot(hour=22)
        await executor.generate(snap)
        assert executor.active_night_plan is None

    async def test_ok_allows_plan(self) -> None:
        """OK level should allow plan generation."""
        executor, guard = _make_executor(GuardLevel.OK)
        snap = _make_snapshot(hour=22)
        await executor.generate(snap)
        assert executor.active_night_plan is not None

    async def test_warning_allows_plan(self) -> None:
        """WARNING level should allow plan generation."""
        executor, guard = _make_executor(GuardLevel.WARNING)
        snap = _make_snapshot(hour=17)
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
        snap = _make_snapshot(hour=22)
        await executor.generate(snap)
        plan = executor.active_night_plan
        assert isinstance(plan, NightPlan)
        assert plan.ev_charge_need_kwh >= 0.0

    async def test_evening_plan_at_17(self) -> None:
        """Hour 17 should generate and store an EveningPlan."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=17)
        await executor.generate(snap)
        plan = executor.active_evening_plan
        assert isinstance(plan, EveningPlan)
        assert plan.bat_available_kwh >= 0.0

    async def test_morning_no_plan_stored(self) -> None:
        """Hour 6 logs snapshot but doesn't create night/evening plan."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=6)
        await executor.generate(snap)
        assert executor.active_night_plan is None
        assert executor.active_evening_plan is None

    async def test_non_plan_hour_noop(self) -> None:
        """Hour 10 is not a plan hour — no plan generated."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=10)
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
        snap = _make_snapshot(hour=22)
        today, tomorrow = executor.generate_48h(snap, current_hour=22)
        assert isinstance(today, str)
        assert isinstance(tomorrow, str)

    def test_entries_have_correct_format(self) -> None:
        """Each entry should be HH:ACTION:SoC%."""
        executor, _ = _make_executor()
        snap = _make_snapshot(hour=22)
        today, tomorrow = executor.generate_48h(snap, current_hour=22)

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
        snap = _make_snapshot(hour=22)
        today, tomorrow = executor.generate_48h(snap, current_hour=22)
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
        snap = _make_snapshot(hour=22)

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
        snap = _make_snapshot(hour=17)

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
        snap = _make_snapshot(hour=22)

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
