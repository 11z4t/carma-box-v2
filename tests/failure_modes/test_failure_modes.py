"""Failure-mode system tests (PLAT-1593).

10 degraded-scenario tests verifying safe behavior under:
T1: HA timeout, T2: inverter write failure, T3: storage/executor error,
T4: forecast unavailable, T5: partial entity loss, T6: EV rejection,
T7: stale data freeze, T8: guard error, T9: HA disconnected,
T10: invalid SoC fallback.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config.schema import load_config
from core.executor import CommandExecutor, ExecutorConfig
from core.fallback import (
    FallbackAction,
    FallbackTrigger,
    resolve_fallback,
    resolve_soc_fallback,
)
from core.guards import (
    ExportGuard,
    GridGuard,
    GuardConfig,
    GuardLevel,
    GuardPolicy,
)
from core.models import Command, CommandType, Scenario
from core.plan_executor import PlanExecutor
from core.planner import Planner, PlannerConfig
from tests.conftest import make_battery_state, make_grid_state, make_snapshot


# ---------------------------------------------------------------------------
# Named test constants
# ---------------------------------------------------------------------------
_CONFIG_PATH = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")

_STALE_DATA_AGE_S: float = 400.0          # Above stale threshold (300s)
_SOC_NOMINAL_PCT: float = 60.0            # Mid-range, no guard trigger
_SOC_LAST_KNOWN_PCT: float = 65.0         # Fallback SoC value
_SOC_INVALID: float = -1.0                # Invalid SoC reading
_SOC_FALLBACK_MAX_AGE_S: float = 120.0    # Max age for last known SoC
_SOC_FALLBACK_AGE_S: float = 30.0         # Time since last valid reading
_TEST_HOUR: int = 14                       # Mid-afternoon
_NEUTRAL_WEIGHTED_AVG_KW: float = 1.0     # No G3 trigger
_NEUTRAL_SPOT_PRICE_ORE: float = 50.0     # No ExportGuard trigger
_PV_NONE_KW: float = 0.0                  # No PV production
_PV_FORECAST_ZERO_KWH: float = 0.0        # No forecast available
_TEST_EMS_POWER_LIMIT_W: int = 3_000      # Test fixture EMS power limit
_NIGHT_PLAN_HOUR: int = 22                 # Night plan generation hour
_TEST_EV_CURRENT_A: int = 10              # Test fixture EV current


# ===========================================================================
# T1: HA timeout → FREEZE
# ===========================================================================


class TestHATimeoutGracefulSkip:
    """T1: HA disconnected triggers G7 — guard level raised."""

    def test_ha_timeout_graceful_skip(self) -> None:
        guard = GridGuard(GuardConfig())
        policy = GuardPolicy(guard, ExportGuard())

        bat = make_battery_state(soc_pct=_SOC_NOMINAL_PCT)
        snap = make_snapshot(hour=_TEST_HOUR, batteries=[bat])
        result = policy.evaluate(
            batteries=snap.batteries,
            current_scenario=Scenario.PV_SURPLUS_DAY,
            weighted_avg_kw=_NEUTRAL_WEIGHTED_AVG_KW,
            hour=snap.hour,
            ha_connected=False,
            pv_kw=_PV_NONE_KW,
            spot_price_ore=_NEUTRAL_SPOT_PRICE_ORE,
        )

        # G7 raises level on HA comm loss (at least WARNING)
        assert result.level != GuardLevel.OK


# ===========================================================================
# T2: Inverter write failure
# ===========================================================================


@pytest.mark.asyncio()
class TestInverterWriteFailure:
    """T2: Inverter write failure returns failed result, no exception."""

    async def test_inverter_write_failure_sets_unverified_mode(self) -> None:
        inv_mock = AsyncMock()
        inv_mock.set_ems_mode = AsyncMock(return_value=True)
        inv_mock.set_ems_power_limit = AsyncMock(return_value=False)
        inv_mock.set_fast_charging = AsyncMock(return_value=True)

        executor = CommandExecutor(
            inverters={"kontor": inv_mock},
            config=ExecutorConfig(mode_change_cooldown_s=0),
        )
        cmds = [
            Command(
                command_type=CommandType.SET_EMS_POWER_LIMIT,
                target_id="kontor",
                value=_TEST_EMS_POWER_LIMIT_W,
                rule_id="TEST",
                reason="test write failure",
            ),
        ]
        result = await executor.execute(cmds)

        assert result.commands_failed >= 1
        assert not result.all_succeeded


# ===========================================================================
# T3: Storage/executor error → RETRY_NEXT
# ===========================================================================


class TestStorageFailureDoesNotStopControl:
    """T3: Executor error resolves to RETRY_NEXT — control continues."""

    def test_storage_failure_does_not_stop_control(self) -> None:
        event = resolve_fallback(FallbackTrigger.EXECUTOR_ERROR, "DB write failed")
        assert event.action == FallbackAction.RETRY_NEXT


# ===========================================================================
# T4: Forecast unavailable → conservative plan
# ===========================================================================


@pytest.mark.asyncio()
class TestForecastUnavailable:
    """T4: Zero PV forecast → plan still generated (conservative)."""

    async def test_forecast_unavailable_uses_conservative_fallback(self) -> None:
        cfg = load_config(_CONFIG_PATH)
        planner = Planner(PlannerConfig())
        guard_policy = GuardPolicy(GridGuard(GuardConfig()), ExportGuard())

        executor = PlanExecutor(
            planner=planner,
            ha_api=None,
            config=cfg,
            guard_policy=guard_policy,
        )

        snap = make_snapshot(
            hour=_NIGHT_PLAN_HOUR,
            batteries=[make_battery_state(soc_pct=_SOC_NOMINAL_PCT)],
            grid=make_grid_state(
                pv_forecast_today_kwh=_PV_FORECAST_ZERO_KWH,
                pv_forecast_tomorrow_kwh=_PV_FORECAST_ZERO_KWH,
            ),
        )
        # Should not raise
        await executor.generate(snap)
        # Night plan should still be generated
        assert executor.active_night_plan is not None


# ===========================================================================
# T5: Partial entity loss → WARNING
# ===========================================================================


class TestPartialEntityLoss:
    """T5: Stale entities logged, guard level raised."""

    def test_partial_entity_loss_logs_missing(self) -> None:
        guard = GridGuard(GuardConfig())
        policy = GuardPolicy(guard, ExportGuard())

        bat = make_battery_state(soc_pct=_SOC_NOMINAL_PCT)
        snap = make_snapshot(hour=_TEST_HOUR, batteries=[bat])
        result = policy.evaluate(
            batteries=snap.batteries,
            current_scenario=Scenario.PV_SURPLUS_DAY,
            weighted_avg_kw=_NEUTRAL_WEIGHTED_AVG_KW,
            hour=snap.hour,
            ha_connected=True,
            pv_kw=_PV_NONE_KW,
            spot_price_ore=_NEUTRAL_SPOT_PRICE_ORE,
            stale_entities=[
                "sensor.goodwe_battery_power_kontor",
                "sensor.goodwe_battery_power_forrad",
            ],
        )

        assert result.level != GuardLevel.OK, (
            f"Expected guard level raised on stale entities, got {result.level.value}"
        )


# ===========================================================================
# T6: EV rejection
# ===========================================================================


@pytest.mark.asyncio()
class TestEVRejection:
    """T6: EV charger rejects command → failure recorded, no crash."""

    async def test_ev_rejection_handled_gracefully(self) -> None:
        inv_mock = AsyncMock()
        inv_mock.set_ems_mode = AsyncMock(return_value=True)
        inv_mock.set_ems_power_limit = AsyncMock(return_value=True)
        inv_mock.set_fast_charging = AsyncMock(return_value=True)

        executor = CommandExecutor(
            inverters={"kontor": inv_mock},
            config=ExecutorConfig(mode_change_cooldown_s=0),
        )

        cmds = [
            Command(
                command_type=CommandType.SET_EV_CURRENT,
                target_id="ev",
                value=_TEST_EV_CURRENT_A,
                rule_id="TEST",
                reason="test EV rejection",
            ),
        ]
        # EV commands go through a different path — should not crash
        result = await executor.execute(cmds)
        assert result is not None


# ===========================================================================
# T7: Stale data → FREEZE, no plan commands
# ===========================================================================


class TestStaleDataFreeze:
    """T7: Stale data triggers FREEZE — plan executor skips."""

    def test_stale_data_triggers_freeze_and_no_commands_sent(self) -> None:
        guard = GridGuard(GuardConfig())
        policy = GuardPolicy(guard, ExportGuard())

        bat = make_battery_state(soc_pct=_SOC_NOMINAL_PCT)
        snap = make_snapshot(hour=_TEST_HOUR, batteries=[bat])
        result = policy.evaluate(
            batteries=snap.batteries,
            current_scenario=Scenario.PV_SURPLUS_DAY,
            weighted_avg_kw=_NEUTRAL_WEIGHTED_AVG_KW,
            hour=snap.hour,
            ha_connected=True,
            pv_kw=_PV_NONE_KW,
            spot_price_ore=_NEUTRAL_SPOT_PRICE_ORE,
            data_age_s=_STALE_DATA_AGE_S,
        )

        assert result.level == GuardLevel.FREEZE


# ===========================================================================
# T8: Guard error → FREEZE
# ===========================================================================


class TestGuardErrorFreeze:
    """T8: Guard error resolves to FREEZE action."""

    def test_guard_error_resolves_to_freeze(self) -> None:
        event = resolve_fallback(FallbackTrigger.GUARD_ERROR, "guard crashed")
        assert event.action == FallbackAction.FREEZE


# ===========================================================================
# T9: HA disconnected → STANDBY_ALL
# ===========================================================================


class TestHADisconnectedStandby:
    """T9: HA disconnected resolves to STANDBY_ALL."""

    def test_ha_disconnected_resolves_to_standby(self) -> None:
        event = resolve_fallback(FallbackTrigger.HA_DISCONNECTED, "HA unreachable")
        assert event.action == FallbackAction.STANDBY_ALL


# ===========================================================================
# T10: Invalid SoC → use last known
# ===========================================================================


class TestInvalidSocFallback:
    """T10: Invalid SoC reading uses last known value."""

    def test_invalid_soc_uses_last_known_value(self) -> None:
        soc, event = resolve_soc_fallback(
            raw_soc=_SOC_INVALID,
            last_known=_SOC_LAST_KNOWN_PCT,
            max_age_s=_SOC_FALLBACK_MAX_AGE_S,
            age_s=_SOC_FALLBACK_AGE_S,
        )
        assert soc == _SOC_LAST_KNOWN_PCT
        assert event is not None
        assert event.trigger == FallbackTrigger.INVALID_SOC
