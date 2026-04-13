"""Tests for Surplus Dispatch Engine.

Covers:
- Knapsack allocation with various surplus levels
- De-escalation in reverse priority order
- Rate limiter blocks rapid switching
- Consumer already active kept running
- Insufficient surplus skips consumer
"""

from __future__ import annotations

import pytest

from core.models import ConsumerState
from core.surplus_dispatch import SurplusConfig, SurplusDispatch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consumer(
    cid: str,
    priority: int,
    power_w: float = 400.0,
    active: bool = False,
    priority_shed: int = 0,
) -> ConsumerState:
    return ConsumerState(
        consumer_id=cid,
        name=cid,
        active=active,
        power_w=power_w,
        priority=priority,
        priority_shed=priority_shed or priority,
        load_type="on_off",
    )


@pytest.fixture()
def dispatch() -> SurplusDispatch:
    return SurplusDispatch(SurplusConfig(
        stop_threshold_w=-100.0,
        max_switches_per_window=10,  # High limit for tests
        switch_window_s=1800.0,
    ))


# ===========================================================================
# Knapsack allocation
# ===========================================================================


class TestKnapsackAllocation:
    """Fit consumers within available surplus."""

    def test_start_single_consumer(self, dispatch: SurplusDispatch) -> None:
        consumers = [_consumer("miner", priority=1, power_w=400)]
        result = dispatch.evaluate(800.0, consumers)
        alloc = {a.consumer_id: a for a in result.allocations}
        assert alloc["miner"].action == "start"

    def test_start_multiple_in_priority_order(self, dispatch: SurplusDispatch) -> None:
        consumers = [
            _consumer("miner", priority=1, power_w=400),
            _consumer("vp_kontor", priority=3, power_w=1500),
        ]
        result = dispatch.evaluate(2500.0, consumers)
        alloc = {a.consumer_id: a for a in result.allocations}
        assert alloc["miner"].action == "start"
        assert alloc["vp_kontor"].action == "start"

    def test_skip_consumer_that_doesnt_fit(self, dispatch: SurplusDispatch) -> None:
        consumers = [
            _consumer("miner", priority=1, power_w=400),
            _consumer("vp_kontor", priority=3, power_w=1500),
        ]
        result = dispatch.evaluate(600.0, consumers)
        alloc = {a.consumer_id: a for a in result.allocations}
        assert alloc["miner"].action == "start"
        assert alloc["vp_kontor"].action == "no_change"  # Doesn't fit

    def test_already_active_kept(self, dispatch: SurplusDispatch) -> None:
        consumers = [
            _consumer("miner", priority=1, power_w=400, active=True),
        ]
        result = dispatch.evaluate(500.0, consumers)
        alloc = {a.consumer_id: a for a in result.allocations}
        assert alloc["miner"].action == "no_change"

    def test_zero_surplus_no_starts(self, dispatch: SurplusDispatch) -> None:
        consumers = [_consumer("miner", priority=1, power_w=400)]
        result = dispatch.evaluate(0.0, consumers)
        alloc = {a.consumer_id: a for a in result.allocations}
        assert alloc["miner"].action == "no_change"


# ===========================================================================
# De-escalation
# ===========================================================================


class TestDeEscalation:
    """Stop consumers in reverse shed priority when surplus negative."""

    def test_de_escalate_stops_active(self, dispatch: SurplusDispatch) -> None:
        consumers = [
            _consumer("miner", priority=1, priority_shed=1, power_w=400, active=True),
            _consumer("vp", priority=3, priority_shed=3, power_w=1500, active=True),
        ]
        result = dispatch.evaluate(-200.0, consumers)
        alloc = {a.consumer_id: a for a in result.allocations}
        # VP (shed=3) stopped first (reverse order)
        assert alloc["vp"].action == "stop"

    def test_de_escalate_ignores_inactive(self, dispatch: SurplusDispatch) -> None:
        consumers = [
            _consumer("miner", priority=1, power_w=400, active=False),
        ]
        result = dispatch.evaluate(-200.0, consumers)
        alloc = {a.consumer_id: a for a in result.allocations}
        assert alloc["miner"].action == "no_change"


# ===========================================================================
# Rate limiting
# ===========================================================================


class TestRateLimiting:
    """Rate limiter blocks rapid switching."""

    def test_rate_limit_blocks_after_max(self) -> None:
        dispatch = SurplusDispatch(SurplusConfig(
            max_switches_per_window=2,
            switch_window_s=1800.0,
        ))
        consumers = [
            _consumer("a", priority=1, power_w=100),
            _consumer("b", priority=2, power_w=100),
            _consumer("c", priority=3, power_w=100),
        ]
        result = dispatch.evaluate(1000.0, consumers)
        actions = {a.consumer_id: a.action for a in result.allocations}
        # Only 2 should start (rate limit)
        started = sum(1 for a in actions.values() if a == "start")
        assert started == 2
        assert actions["c"] == "no_change"  # Blocked by rate limit


# ===========================================================================
# Coverage: uncovered branches
# ===========================================================================


class TestCoverageBranches:
    """Tests targeting specific uncovered branches."""

    def test_effective_deadband_doubled_when_doubled(self) -> None:
        """effective_deadband_w returns doubled value when deadband is doubled (line 111)."""
        import time
        cfg = SurplusConfig(deadband_w=50.0, doubled_deadband_w=100.0)
        dispatch = SurplusDispatch(cfg)
        # Set doubled deadband active by making it expire far in the future
        dispatch._rate_limiter._deadband_until = time.monotonic() + 3600
        assert dispatch._rate_limiter.is_deadband_doubled is True
        assert dispatch._rate_limiter.effective_deadband_w == 100.0

    def test_old_switches_purged_from_deque(self) -> None:
        """Old switch entries outside window are purged from deque (line 117)."""
        import time
        cfg = SurplusConfig(switch_window_s=300.0, max_switches_per_window=10)
        dispatch = SurplusDispatch(cfg)
        # Inject old timestamp (700s ago — outside window)
        dispatch._rate_limiter._switches.appendleft(time.monotonic() - 700)
        assert len(dispatch._rate_limiter._switches) == 1
        # Trigger purge via evaluate
        consumers = [_consumer("miner", priority=1, power_w=400)]
        dispatch.evaluate(800.0, consumers)
        assert len(dispatch._rate_limiter._switches) == 1  # Only new switch from start

    def test_de_escalation_rate_limited_emits_no_change(self) -> None:
        """Rate-limited de-escalation emits no_change with reason (line 228)."""
        cfg = SurplusConfig(
            max_switches_per_window=0,  # No switches allowed
            switch_window_s=1800.0,
        )
        dispatch = SurplusDispatch(cfg)
        consumers = [
            _consumer("miner", priority=1, priority_shed=1, power_w=400, active=True),
        ]
        result = dispatch.evaluate(-200.0, consumers)
        alloc = {a.consumer_id: a for a in result.allocations}
        # De-escalation wanted to stop, but rate limiter blocked it
        assert alloc["miner"].action == "no_change"
        assert "rate limited" in alloc["miner"].reason
