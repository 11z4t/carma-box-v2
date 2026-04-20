"""Tests for Mode Change Protocol (5-Step).

Covers:
- Full state machine progression (IDLE→CLEARING→STANDBY→SET→VERIFY→COMPLETE)
- Emergency bypass (skips standby)
- Transition matrix (PLAT-1750): per-target-mode skip policies
  - charge_battery skips both clear_wait and standby (direct to SET TARGET)
  - skip_standby-only transitions: CLEARING → SET TARGET (no STANDBY_WAIT)
  - discharge_pv still uses full 5-step (B1/B2 regression guard)
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
    TransitionPolicy,
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
        accepted = manager.request_change("kontor", "discharge_pv", reason="test")
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
    async def test_is_in_progress_during_change(self, manager: ModeChangeManager) -> None:
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
    async def test_emergency_skips_clear_and_standby(self, manager: ModeChangeManager) -> None:
        """PLAT-1360: emergency skips both clear_wait AND standby (goes straight to
        SETTING_TARGET on the first cycle — no 60s clear_wait delay)."""
        executor = _make_executor()
        executor.get_ems_mode.return_value = "battery_standby"

        manager.emergency_mode_change("kontor", "battery_standby", reason="G0 grid charging")

        # Cycle 1: IDLE → SETTING_TARGET (skips CLEARING and STANDBY)
        await manager.process(executor)
        assert manager.get_state("kontor") == ModeChangeState.SETTING_TARGET

    @pytest.mark.asyncio
    async def test_emergency_overrides_existing(self, manager: ModeChangeManager) -> None:
        """Emergency override replaces in-progress normal change."""
        executor = _make_executor()

        manager.request_change("kontor", "discharge_pv")
        await manager.process(executor)  # Start normal

        # Emergency overrides
        manager.emergency_mode_change("kontor", "battery_standby", "G0")
        assert manager.get_state("kontor") == ModeChangeState.IDLE

    @pytest.mark.asyncio
    async def test_emergency_with_fast_charging_sets_fast_charging_on(
        self, manager: ModeChangeManager
    ) -> None:
        """PLAT-1751: emergency_mode_change(target_fast_charging=True) must call
        set_fast_charging(True) on the executor during _execute_set_target.

        This is the fast path for SoC-floor recovery: budget signals emergency_recovery
        → engine calls emergency_mode_change with fast_charging=True → hardware gets
        charge_battery + fast_charging ON in a single cycle (< 30 s).
        """
        executor = _make_executor()
        executor.get_ems_mode.return_value = "charge_battery"

        manager.emergency_mode_change(
            "kontor",
            "charge_battery",
            reason="SoC < floor",
            target_fast_charging=True,
        )

        # Cycle 1: IDLE → SETTING_TARGET (emergency skips clear+standby)
        await manager.process(executor)

        assert manager.get_state("kontor") == ModeChangeState.SETTING_TARGET
        # set_fast_charging must have been called with True
        calls = [
            call.args
            for call in executor.set_fast_charging.call_args_list
            if call.args[0] == "kontor"
        ]
        assert any(args[1] is True for args in calls), (
            "emergency_mode_change(target_fast_charging=True) must call "
            f"set_fast_charging('kontor', True); calls: {calls}"
        )

    @pytest.mark.asyncio
    async def test_emergency_without_fast_charging_does_not_force_on(
        self, manager: ModeChangeManager
    ) -> None:
        """PLAT-1751: emergency without target_fast_charging=True must NOT
        call set_fast_charging(True) — default is False (existing behaviour).
        """
        executor = _make_executor()
        executor.get_ems_mode.return_value = "battery_standby"

        manager.emergency_mode_change(
            "kontor",
            "battery_standby",
            reason="G0 guard",
            # target_fast_charging defaults to False
        )

        await manager.process(executor)

        calls = [
            call.args
            for call in executor.set_fast_charging.call_args_list
            if call.args[0] == "kontor"
        ]
        assert not any(args[1] is True for args in calls), (
            "Default emergency (no fast_charging flag) must NOT call "
            f"set_fast_charging True; calls: {calls}"
        )


# ---------------------------------------------------------------------------
# Concurrent batteries
# ---------------------------------------------------------------------------


class TestConcurrentBatteries:
    """Multiple batteries can change simultaneously."""

    @pytest.mark.asyncio
    async def test_two_batteries_change_independently(self, manager: ModeChangeManager) -> None:
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
    async def test_same_target_rejected(self, manager: ModeChangeManager) -> None:
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
    async def test_retry_on_mismatch(self, manager: ModeChangeManager) -> None:
        executor = _make_executor()
        # Mode read returns wrong mode first time
        executor.get_ems_mode.side_effect = [
            "charge_pv",  # verify mismatch #1
            "discharge_pv",  # set target call reads
            "discharge_pv",  # verify success
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
    async def test_fails_after_max_retries(self, manager: ModeChangeManager) -> None:
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
    async def test_cancel_removes_request(self, manager: ModeChangeManager) -> None:
        executor = _make_executor()
        manager.request_change("kontor", "discharge_pv")
        await manager.process(executor)

        manager.cancel("kontor")
        assert manager.is_in_progress("kontor") is False

    def test_cancel_nonexistent_is_noop(self, manager: ModeChangeManager) -> None:
        manager.cancel("nonexistent")  # Should not raise


# ---------------------------------------------------------------------------
# REGRESSION: B1 — standby intermediate
# ---------------------------------------------------------------------------


class TestB1StandbyIntermediate:
    """B1 regression: battery_standby MUST be set as intermediate step."""

    @pytest.mark.asyncio
    async def test_standby_set_during_transition(self, manager: ModeChangeManager) -> None:
        executor = _make_executor()
        executor.get_ems_mode.return_value = "discharge_pv"

        manager.request_change("kontor", "discharge_pv")

        # Run through CLEARING
        await manager.process(executor)  # IDLE → CLEARING
        await manager.process(executor)  # CLEARING → STANDBY_WAIT

        # Verify standby was set
        set_mode_calls = executor.set_ems_mode.call_args_list
        assert any(
            call[0] == ("kontor", "battery_standby") for call in set_mode_calls
        ), "battery_standby MUST be set as intermediate (B1)"


# ---------------------------------------------------------------------------
# REGRESSION: B7 — fast_charging OFF before discharge_pv
# ---------------------------------------------------------------------------


class TestB7FastChargingBeforeDischarge:
    """B7 regression: fast_charging MUST be OFF before discharge_pv."""

    @pytest.mark.asyncio
    async def test_fast_charging_cleared_in_step2(self, manager: ModeChangeManager) -> None:
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
            c for c in executor.set_fast_charging.call_args_list if c[0] == ("kontor", False)
        ]
        assert len(fc_calls) >= 1, "fast_charging OFF must be enforced (B7)"


# ---------------------------------------------------------------------------
# REGRESSION: B15 — ems_power_limit = 0 in CLEARING
# ---------------------------------------------------------------------------


class TestB15ClearLimits:
    """B15 regression: ems_power_limit MUST be set to 0 in clearing step."""

    @pytest.mark.asyncio
    async def test_limit_zeroed_in_clearing(self, manager: ModeChangeManager) -> None:
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

    async def test_get_state_returns_idle_when_no_request(self, manager: ModeChangeManager) -> None:
        """get_state returns IDLE when battery has no pending request (line 205)."""
        state = manager.get_state("nonexistent_battery")
        assert state == ModeChangeState.IDLE

    async def test_target_limit_w_set_when_nonzero(self, manager: ModeChangeManager) -> None:
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
            c for c in executor.set_ems_power_limit.await_args_list if c.args == ("kontor", 2000)
        ]
        assert len(calls) >= 1


# ---------------------------------------------------------------------------
# PLAT-1750: Transition matrix — skip STANDBY for charge_battery
# ---------------------------------------------------------------------------


class TestTransitionMatrix:
    """Transition matrix routes * → charge_battery directly to SET TARGET."""

    @pytest.mark.asyncio
    async def test_charge_battery_skips_clearing_and_standby(
        self, manager: ModeChangeManager
    ) -> None:
        """charge_battery must skip CLEARING wait AND STANDBY (direct to SETTING_TARGET).

        GoodWe firmware accepts direct write to charge_battery from any mode.
        """
        executor = _make_executor()
        executor.get_ems_mode.return_value = "charge_battery"

        accepted = manager.request_change(
            "kontor", "charge_battery", target_limit_w=3000, reason="PLAT-1750"
        )
        assert accepted is True

        # Cycle 1: IDLE → SETTING_TARGET (skip CLEARING + STANDBY)
        await manager.process(executor)
        assert manager.get_state("kontor") == ModeChangeState.SETTING_TARGET

        # battery_standby must NOT have been set as intermediate
        set_mode_calls = [c[0] for c in executor.set_ems_mode.call_args_list]
        assert (
            "kontor",
            "battery_standby",
        ) not in set_mode_calls, "charge_battery must NOT go through battery_standby"

    @pytest.mark.asyncio
    async def test_charge_battery_completes_without_standby(
        self, manager: ModeChangeManager
    ) -> None:
        """Full progression: charge_battery reaches COMPLETE skipping standby steps."""
        executor = _make_executor()
        executor.get_ems_mode.return_value = "charge_battery"

        manager.request_change("kontor", "charge_battery", target_limit_w=2500)

        # Run until COMPLETE (should need fewer cycles than full 5-step)
        for _ in range(5):
            await manager.process(executor)
            if manager.get_state("kontor") == ModeChangeState.COMPLETE:
                break

        assert manager.get_state("kontor") == ModeChangeState.COMPLETE

        # Verify SETTING_TARGET was called with correct limit
        power_calls = [c.args for c in executor.set_ems_power_limit.await_args_list]
        assert ("kontor", 2500) in power_calls

    @pytest.mark.asyncio
    async def test_charge_battery_clear_limits_still_executed(
        self, manager: ModeChangeManager
    ) -> None:
        """Even when skipping standby, clear limits (step 2) must still execute.

        B9/B15: ems_power_limit=0 and fast_charging=OFF must be written before
        setting the target mode.
        """
        executor = _make_executor()
        executor.get_ems_mode.return_value = "charge_battery"

        manager.request_change("kontor", "charge_battery")
        await manager.process(executor)  # IDLE → SETTING_TARGET

        # Clear limits executed even when skipping standby
        executor.set_ems_power_limit.assert_awaited_with("kontor", 0)
        executor.set_fast_charging.assert_awaited_with("kontor", False)

    @pytest.mark.asyncio
    async def test_charge_battery_still_rejects_if_in_progress(
        self, manager: ModeChangeManager
    ) -> None:
        """Transition matrix skip does NOT override in-progress changes (unlike emergency)."""
        executor = _make_executor()
        executor.get_ems_mode.return_value = "charge_battery"

        # First request starts (IDLE → SETTING_TARGET on cycle 1)
        accepted_first = manager.request_change("kontor", "charge_battery")
        assert accepted_first is True
        await manager.process(executor)  # → SETTING_TARGET

        # Second request for same target must be rejected (still in progress)
        accepted_second = manager.request_change("kontor", "charge_battery")
        assert accepted_second is False

    @pytest.mark.asyncio
    async def test_discharge_pv_still_uses_full_five_step(self, manager: ModeChangeManager) -> None:
        """B1/B2 regression: discharge_pv must still go through battery_standby.

        The transition matrix only skips standby for modes that GoodWe
        accepts directly. discharge_pv requires the full 5-step protocol.
        """
        executor = _make_executor()
        executor.get_ems_mode.return_value = "discharge_pv"

        manager.request_change("kontor", "discharge_pv", reason="B1-regression")
        await manager.process(executor)  # IDLE → CLEARING
        assert manager.get_state("kontor") == ModeChangeState.CLEARING

        await manager.process(executor)  # CLEARING → STANDBY_WAIT
        assert manager.get_state("kontor") == ModeChangeState.STANDBY_WAIT

        # battery_standby MUST be set as intermediate
        set_mode_calls = [c[0] for c in executor.set_ems_mode.call_args_list]
        assert (
            "kontor",
            "battery_standby",
        ) in set_mode_calls, "B1: battery_standby MUST be set as intermediate for discharge_pv"

    @pytest.mark.asyncio
    async def test_charge_pv_not_in_default_matrix_uses_full_five_step(
        self, manager: ModeChangeManager
    ) -> None:
        """charge_pv is not in the default skip matrix — uses full 5-step."""
        executor = _make_executor()
        executor.get_ems_mode.return_value = "charge_pv"

        manager.request_change("kontor", "charge_pv")
        await manager.process(executor)  # IDLE → CLEARING
        assert manager.get_state("kontor") == ModeChangeState.CLEARING

        await manager.process(executor)  # CLEARING → STANDBY_WAIT
        assert manager.get_state("kontor") == ModeChangeState.STANDBY_WAIT

    def test_default_matrix_includes_charge_battery(self) -> None:
        """Default transition matrix must include charge_battery with skip policy."""
        cfg = ModeChangeConfig()
        assert "charge_battery" in cfg.transition_matrix
        policy = cfg.transition_matrix["charge_battery"]
        assert policy.skip_clear_wait is True
        assert policy.skip_standby is True

    def test_transition_policy_is_immutable(self) -> None:
        """TransitionPolicy must be frozen (immutable config contract)."""
        policy = TransitionPolicy(skip_clear_wait=True, skip_standby=True)
        import dataclasses

        assert dataclasses.is_dataclass(policy)
        # frozen=True means FrozenInstanceError on mutation
        import pytest as _pytest

        with _pytest.raises(Exception):
            policy.skip_clear_wait = False  # type: ignore[misc]

    def test_custom_matrix_via_config(self) -> None:
        """Caller can pass a custom transition_matrix in ModeChangeConfig."""
        custom_matrix = {
            "charge_pv": TransitionPolicy(skip_clear_wait=False, skip_standby=True),
        }
        cfg = ModeChangeConfig(transition_matrix=custom_matrix)
        assert "charge_pv" in cfg.transition_matrix
        policy = cfg.transition_matrix["charge_pv"]
        assert policy.skip_clear_wait is False
        assert policy.skip_standby is True
        # charge_battery NOT in custom matrix
        assert "charge_battery" not in cfg.transition_matrix

    @pytest.mark.asyncio
    async def test_skip_standby_only_clears_then_sets_target(self) -> None:
        """When only skip_standby=True (not skip_clear_wait), waits CLEARING then skips STANDBY."""
        custom_matrix = {
            "charge_pv": TransitionPolicy(skip_clear_wait=False, skip_standby=True),
        }
        cfg = ModeChangeConfig(
            clear_wait_s=0.0,
            standby_wait_s=0.0,
            set_wait_s=0.0,
            verify_wait_s=0.0,
            transition_matrix=custom_matrix,
        )
        mgr = ModeChangeManager(cfg)
        executor = _make_executor()
        executor.get_ems_mode.return_value = "charge_pv"

        mgr.request_change("kontor", "charge_pv")

        # Cycle 1: IDLE → CLEARING (skip_clear_wait=False, so still enters CLEARING)
        await mgr.process(executor)
        assert mgr.get_state("kontor") == ModeChangeState.CLEARING

        # Cycle 2: CLEARING → SETTING_TARGET (skip_standby=True, no STANDBY_WAIT)
        await mgr.process(executor)
        assert mgr.get_state("kontor") == ModeChangeState.SETTING_TARGET

        # battery_standby must NOT have been set
        set_mode_calls = [c[0] for c in executor.set_ems_mode.call_args_list]
        assert ("kontor", "battery_standby") not in set_mode_calls


# ---------------------------------------------------------------------------
# PLAT-1750: Timing — charge_battery completes in ≤ 3 cycles (fast_config)
# ---------------------------------------------------------------------------


class TestChargeBatteryTiming:
    """charge_battery must reach SETTING_TARGET on the first process() call."""

    @pytest.mark.asyncio
    async def test_charge_battery_reaches_set_target_on_cycle_1(
        self, manager: ModeChangeManager
    ) -> None:
        """With zero-wait config, SETTING_TARGET is reached in 1 cycle (skipped standby).

        In production with real timings, this eliminates the 60s clearing wait
        + 300s standby, reducing * → charge_battery to set_wait + verify_wait
        (target ≤ 15 s per PLAT-1750 AC).
        """
        executor = _make_executor()
        executor.get_ems_mode.return_value = "charge_battery"

        manager.request_change("kontor", "charge_battery")
        await manager.process(executor)
        assert (
            manager.get_state("kontor") == ModeChangeState.SETTING_TARGET
        ), "charge_battery MUST reach SETTING_TARGET on cycle 1 (no clear_wait, no standby)"

    @pytest.mark.asyncio
    async def test_full_five_step_reaches_standby_on_cycle_2(
        self, manager: ModeChangeManager
    ) -> None:
        """Baseline: discharge_pv reaches STANDBY_WAIT on cycle 2 (not optimised)."""
        executor = _make_executor()
        executor.get_ems_mode.return_value = "discharge_pv"

        manager.request_change("kontor", "discharge_pv")
        await manager.process(executor)  # Cycle 1: IDLE → CLEARING
        await manager.process(executor)  # Cycle 2: CLEARING → STANDBY_WAIT
        assert manager.get_state("kontor") == ModeChangeState.STANDBY_WAIT


# ---------------------------------------------------------------------------
# Coverage: pre-existing uncovered branches (clear_pending, prune)
# ---------------------------------------------------------------------------


class TestCoverageGaps:
    """Close pre-existing coverage gaps to reach 100% on mode_change.py."""

    def test_clear_pending_removes_pending_request(self, manager: ModeChangeManager) -> None:
        """clear_pending() removes a pending (IDLE) request."""
        manager.request_change("kontor", "charge_pv")
        assert manager.get_state("kontor") == ModeChangeState.IDLE

        manager.clear_pending("kontor")
        assert manager.get_state("kontor") == ModeChangeState.IDLE  # no request = IDLE

    def test_clear_pending_nonexistent_is_noop(self, manager: ModeChangeManager) -> None:
        """clear_pending() on unknown battery does nothing."""
        manager.clear_pending("unknown")  # must not raise

    @pytest.mark.asyncio
    async def test_prune_removes_stale_completed_request(self, manager: ModeChangeManager) -> None:
        """Completed requests older than _PRUNE_AGE_S are pruned by process()."""
        import time

        executor = _make_executor()
        executor.get_ems_mode.return_value = "charge_battery"

        manager.request_change("kontor", "charge_battery")
        # Run to COMPLETE
        for _ in range(5):
            await manager.process(executor)
            if manager.get_state("kontor") == ModeChangeState.COMPLETE:
                break

        assert manager.get_state("kontor") == ModeChangeState.COMPLETE

        # Back-date step_started_at past prune threshold
        req = manager._requests["kontor"]  # noqa: SLF001
        req.step_started_at = time.monotonic() - (ModeChangeManager._PRUNE_AGE_S + 1)

        # One more process() cycle — pruner should remove the stale entry
        await manager.process(executor)
        assert "kontor" not in manager._requests  # noqa: SLF001
