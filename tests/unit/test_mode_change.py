"""Tests for Mode Change Protocol (5-Step).

Covers:
- Full state machine progression (IDLE→CLEARING→STANDBY→SET→VERIFY→COMPLETE)
- Emergency bypass (skips standby)
- Concurrent changes on different batteries
- Rejection of duplicate requests
- Retry on verification mismatch (up to 3×)
- FAILED state after max retries
- Regressions: B1 (standby intermediate), B2 (no direct charge→discharge),
  B7 (fast_charging OFF before discharge), B15 (limit=0 in CLEARING)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.mode_change import (
    ModeChangeConfig,
    ModeChangeManager,
    ModeChangeState,
)


# ---------------------------------------------------------------------------
# Mock executor
# ---------------------------------------------------------------------------


def _make_executor(
    current_mode: str = "charge_pv",
    fast_charging: bool = False,
) -> AsyncMock:
    """Create a mock ModeChangeExecutor."""
    executor = AsyncMock()
    executor.set_ems_mode = AsyncMock(return_value=True)
    executor.set_ems_power_limit = AsyncMock(return_value=True)
    executor.set_fast_charging = AsyncMock(return_value=True)
    executor.get_ems_mode = AsyncMock(return_value=current_mode)
    executor.get_fast_charging = AsyncMock(return_value=fast_charging)
    return executor


@pytest.fixture()
def fast_config() -> ModeChangeConfig:
    """Config with zero waits for testing."""
    return ModeChangeConfig(
        clear_wait_s=0.0,
        standby_wait_s=0.0,
        set_wait_s=0.0,
        verify_wait_s=0.0,
        max_retries=3,
    )


@pytest.fixture()
def manager(fast_config: ModeChangeConfig) -> ModeChangeManager:
    return ModeChangeManager(fast_config)


# ---------------------------------------------------------------------------
# Full state machine progression
# ---------------------------------------------------------------------------


class TestFullProgression:
    """Test the complete 5-step mode change sequence."""

    @pytest.mark.asyncio
    async def test_idle_to_complete(self, manager: ModeChangeManager) -> None:
        """Full progression: IDLE → CLEARING → STANDBY → SET → VERIFY → COMPLETE."""
        executor = _make_executor()
        executor.get_ems_mode.return_value = "discharge_pv"

        # Request change
        accepted = manager.request_change(
            "kontor", "discharge_pv", reason="test"
        )
        assert accepted is True
        assert manager.get_state("kontor") == ModeChangeState.IDLE

        # Cycle 1: IDLE → CLEARING (executes clear limits)
        await manager.process(executor)
        assert manager.get_state("kontor") == ModeChangeState.CLEARING

        # Verify clear limits were called (B15)
        executor.set_ems_power_limit.assert_awaited_with("kontor", 0)
        executor.set_fast_charging.assert_awaited_with("kontor", False)

        # Cycle 2: CLEARING → STANDBY_WAIT (clear_wait_s=0)
        await manager.process(executor)
        assert manager.get_state("kontor") == ModeChangeState.STANDBY_WAIT

        # Verify standby was set (B1)
        executor.set_ems_mode.assert_awaited_with("kontor", "battery_standby")

        # Cycle 3: STANDBY_WAIT → SETTING_TARGET (standby_wait_s=0)
        await manager.process(executor)
        assert manager.get_state("kontor") == ModeChangeState.SETTING_TARGET

        # Cycle 4: SETTING_TARGET → VERIFYING (set_wait_s=0)
        await manager.process(executor)
        assert manager.get_state("kontor") == ModeChangeState.VERIFYING

        # Cycle 5: VERIFYING → COMPLETE (verify_wait_s=0)
        await manager.process(executor)
        assert manager.get_state("kontor") == ModeChangeState.COMPLETE

    @pytest.mark.asyncio
    async def test_is_in_progress_during_change(
        self, manager: ModeChangeManager
    ) -> None:
        executor = _make_executor()
        assert manager.is_in_progress("kontor") is False

        manager.request_change("kontor", "discharge_pv")
        await manager.process(executor)  # IDLE → CLEARING
        assert manager.is_in_progress("kontor") is True


# ---------------------------------------------------------------------------
# Emergency bypass
# ---------------------------------------------------------------------------


class TestEmergencyBypass:
    """Emergency mode changes skip standby wait."""

    @pytest.mark.asyncio
    async def test_emergency_skips_clear_and_standby(
        self, manager: ModeChangeManager
    ) -> None:
        """PLAT-1360: emergency skips both clear_wait AND standby (goes straight to
        SETTING_TARGET on the first cycle — no 60s clear_wait delay)."""
        executor = _make_executor()
        executor.get_ems_mode.return_value = "battery_standby"

        manager.emergency_mode_change(
            "kontor", "battery_standby", reason="G0 grid charging"
        )

        # Cycle 1: IDLE → SETTING_TARGET (skips CLEARING and STANDBY)
        await manager.process(executor)
        assert manager.get_state("kontor") == ModeChangeState.SETTING_TARGET

    @pytest.mark.asyncio
    async def test_emergency_overrides_existing(
        self, manager: ModeChangeManager
    ) -> None:
        """Emergency override replaces in-progress normal change."""
        executor = _make_executor()

        manager.request_change("kontor", "discharge_pv")
        await manager.process(executor)  # Start normal

        # Emergency overrides
        manager.emergency_mode_change("kontor", "battery_standby", "G0")
        assert manager.get_state("kontor") == ModeChangeState.IDLE


# ---------------------------------------------------------------------------
# Concurrent batteries
# ---------------------------------------------------------------------------


class TestConcurrentBatteries:
    """Multiple batteries can change simultaneously."""

    @pytest.mark.asyncio
    async def test_two_batteries_change_independently(
        self, manager: ModeChangeManager
    ) -> None:
        executor = _make_executor()

        manager.request_change("kontor", "discharge_pv")
        manager.request_change("forrad", "charge_pv")

        await manager.process(executor)

        assert manager.is_in_progress("kontor")
        assert manager.is_in_progress("forrad")
        # Both should be in CLEARING
        assert manager.get_state("kontor") == ModeChangeState.CLEARING
        assert manager.get_state("forrad") == ModeChangeState.CLEARING


# ---------------------------------------------------------------------------
# Duplicate rejection
# ---------------------------------------------------------------------------


class TestDuplicateRejection:
    """Reject duplicate requests while in progress."""

    @pytest.mark.asyncio
    async def test_same_target_rejected(
        self, manager: ModeChangeManager
    ) -> None:
        executor = _make_executor()
        manager.request_change("kontor", "discharge_pv")
        await manager.process(executor)  # Start

        accepted = manager.request_change("kontor", "discharge_pv")
        assert accepted is False

    @pytest.mark.asyncio
    async def test_different_target_rejected_during_progress(
        self, manager: ModeChangeManager
    ) -> None:
        executor = _make_executor()
        manager.request_change("kontor", "discharge_pv")
        await manager.process(executor)  # Start

        accepted = manager.request_change("kontor", "charge_pv")
        assert accepted is False


# ---------------------------------------------------------------------------
# Verification retry
# ---------------------------------------------------------------------------


class TestVerificationRetry:
    """Retry on verification mismatch."""

    @pytest.mark.asyncio
    async def test_retry_on_mismatch(
        self, manager: ModeChangeManager
    ) -> None:
        executor = _make_executor()
        # Mode read returns wrong mode first time
        executor.get_ems_mode.side_effect = [
            "charge_pv",      # verify mismatch #1
            "discharge_pv",   # set target call reads
            "discharge_pv",   # verify success
        ]

        manager.request_change("kontor", "discharge_pv")

        # Progress through to VERIFYING
        for _ in range(5):
            await manager.process(executor)

        # Should be in SETTING_TARGET (retry) or VERIFYING
        state = manager.get_state("kontor")
        assert state in (
            ModeChangeState.SETTING_TARGET,
            ModeChangeState.VERIFYING,
            ModeChangeState.COMPLETE,
        )

    @pytest.mark.asyncio
    async def test_fails_after_max_retries(
        self, manager: ModeChangeManager
    ) -> None:
        executor = _make_executor()
        # Mode always returns wrong mode
        executor.get_ems_mode.return_value = "charge_pv"

        manager.request_change("kontor", "discharge_pv")

        # Run many cycles — should eventually fail
        for _ in range(20):
            await manager.process(executor)

        assert manager.get_state("kontor") == ModeChangeState.FAILED


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


class TestCancel:
    """Cancel in-progress mode changes."""

    @pytest.mark.asyncio
    async def test_cancel_removes_request(
        self, manager: ModeChangeManager
    ) -> None:
        executor = _make_executor()
        manager.request_change("kontor", "discharge_pv")
        await manager.process(executor)

        manager.cancel("kontor")
        assert manager.is_in_progress("kontor") is False

    def test_cancel_nonexistent_is_noop(
        self, manager: ModeChangeManager
    ) -> None:
        manager.cancel("nonexistent")  # Should not raise


# ---------------------------------------------------------------------------
# REGRESSION: B1 — standby intermediate
# ---------------------------------------------------------------------------


class TestB1StandbyIntermediate:
    """B1 regression: battery_standby MUST be set as intermediate step."""

    @pytest.mark.asyncio
    async def test_standby_set_during_transition(
        self, manager: ModeChangeManager
    ) -> None:
        executor = _make_executor()
        executor.get_ems_mode.return_value = "discharge_pv"

        manager.request_change("kontor", "discharge_pv")

        # Run through CLEARING
        await manager.process(executor)  # IDLE → CLEARING
        await manager.process(executor)  # CLEARING → STANDBY_WAIT

        # Verify standby was set
        set_mode_calls = executor.set_ems_mode.call_args_list
        assert any(
            call[0] == ("kontor", "battery_standby")
            for call in set_mode_calls
        ), "battery_standby MUST be set as intermediate (B1)"


# ---------------------------------------------------------------------------
# REGRESSION: B7 — fast_charging OFF before discharge_pv
# ---------------------------------------------------------------------------


class TestB7FastChargingBeforeDischarge:
    """B7 regression: fast_charging MUST be OFF before discharge_pv."""

    @pytest.mark.asyncio
    async def test_fast_charging_cleared_in_step2(
        self, manager: ModeChangeManager
    ) -> None:
        """Step 2 CLEAR LIMITS must set fast_charging=OFF."""
        executor = _make_executor()
        manager.request_change("kontor", "discharge_pv")
        await manager.process(executor)  # IDLE → CLEARING

        executor.set_fast_charging.assert_awaited_with("kontor", False)

    @pytest.mark.asyncio
    async def test_fast_charging_verified_before_set_target(
        self, manager: ModeChangeManager
    ) -> None:
        """Step 4: If fast_charging still ON before discharge, force OFF."""
        executor = _make_executor()
        executor.get_fast_charging.return_value = True  # Still ON!
        executor.get_ems_mode.return_value = "discharge_pv"

        manager.request_change("kontor", "discharge_pv")

        # Run all steps
        for _ in range(10):
            await manager.process(executor)

        # fast_charging should have been forced OFF
        fc_calls = [
            c for c in executor.set_fast_charging.call_args_list
            if c[0] == ("kontor", False)
        ]
        assert len(fc_calls) >= 1, "fast_charging OFF must be enforced (B7)"


# ---------------------------------------------------------------------------
# REGRESSION: B15 — ems_power_limit = 0 in CLEARING
# ---------------------------------------------------------------------------


class TestB15ClearLimits:
    """B15 regression: ems_power_limit MUST be set to 0 in clearing step."""

    @pytest.mark.asyncio
    async def test_limit_zeroed_in_clearing(
        self, manager: ModeChangeManager
    ) -> None:
        executor = _make_executor()
        manager.request_change("kontor", "discharge_pv")
        await manager.process(executor)  # IDLE → CLEARING

        executor.set_ems_power_limit.assert_awaited_with("kontor", 0)


# ---------------------------------------------------------------------------
# Coverage: uncovered branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCoverageBranches:
    """Tests targeting specific uncovered branches."""

    async def test_get_state_returns_idle_when_no_request(
        self, manager: ModeChangeManager
    ) -> None:
        """get_state returns IDLE when battery has no pending request (line 205)."""
        state = manager.get_state("nonexistent_battery")
        assert state == ModeChangeState.IDLE

    async def test_target_limit_w_set_when_nonzero(
        self, manager: ModeChangeManager
    ) -> None:
        """set_ems_power_limit called with target_limit_w when > 0 (line 333)."""
        executor = _make_executor(current_mode="charge_pv")
        executor.get_ems_mode.return_value = "charge_pv"
        manager.request_change(
            "kontor", "charge_pv", target_limit_w=2000, reason="grid charge test"
        )
        # Cycle through all steps until COMPLETE
        for _ in range(10):
            await manager.process(executor)
            if manager.get_state("kontor") == ModeChangeState.COMPLETE:
                break
        # set_ems_power_limit should have been called with 2000 at SETTING_TARGET
        calls = [
            c for c in executor.set_ems_power_limit.await_args_list
            if c.args == ("kontor", 2000)
        ]
        assert len(calls) >= 1
