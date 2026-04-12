"""Tests for K/F Battery Balancer.

Covers:
- Proportional allocation at equal SoC (75%/25% by capacity)
- Convergence correction for diverged SoC
- Cold derating (50% at <4°C, blocked at <0°C)
- SoH derating tiers
- Charging mode: inverse allocation
- CT compensation
- Edge cases: single battery, all at floor, zero watts
- Regression B5: K/F divergence handling
"""

from __future__ import annotations

import pytest

from core.balancer import (
    BatteryBalancer,
    BatteryInfo,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _kontor(**overrides: object) -> BatteryInfo:
    """Create Kontor battery info with defaults."""
    defaults = {
        "battery_id": "kontor",
        "soc_pct": 60.0,
        "cap_kwh": 15.0,
        "cell_temp_c": 20.0,
        "soh_pct": 100.0,
        "max_discharge_w": 5000.0,
        "max_charge_w": 5000.0,
        "ct_placement": "local_load",
        "local_load_w": 500.0,
        "pv_power_w": 0.0,
    }
    defaults.update(overrides)
    return BatteryInfo(**defaults)  # type: ignore[arg-type]


def _forrad(**overrides: object) -> BatteryInfo:
    """Create Förråd battery info with defaults."""
    defaults = {
        "battery_id": "forrad",
        "soc_pct": 60.0,
        "cap_kwh": 5.0,
        "cell_temp_c": 20.0,
        "soh_pct": 100.0,
        "max_discharge_w": 5000.0,
        "max_charge_w": 5000.0,
        "ct_placement": "house_grid",
    }
    defaults.update(overrides)
    return BatteryInfo(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def balancer() -> BatteryBalancer:
    return BatteryBalancer()


# ===========================================================================
# Proportional allocation
# ===========================================================================


class TestProportionalAllocation:
    """At equal SoC, allocation should be proportional to capacity."""

    def test_75_25_split_at_equal_soc(self, balancer: BatteryBalancer) -> None:
        """Kontor (15kWh) = 75%, Förråd (5kWh) = 25% at equal SoC."""
        result = balancer.allocate(
            [_kontor(), _forrad()], total_watts=4000.0
        )
        alloc = result.allocation_map
        # 75% of 4000 = 3000, 25% = 1000
        assert alloc["kontor"].watts == pytest.approx(3000, abs=50)
        assert alloc["forrad"].watts == pytest.approx(1000, abs=50)

    def test_total_matches_requested(self, balancer: BatteryBalancer) -> None:
        result = balancer.allocate(
            [_kontor(), _forrad()], total_watts=2000.0
        )
        assert result.total_allocated_w == pytest.approx(2000, abs=50)

    def test_single_battery_gets_all(self, balancer: BatteryBalancer) -> None:
        result = balancer.allocate([_kontor()], total_watts=3000.0)
        assert len(result.allocations) == 1
        assert result.allocations[0].watts == 3000

    def test_empty_batteries_returns_empty(self, balancer: BatteryBalancer) -> None:
        result = balancer.allocate([], total_watts=1000.0)
        assert len(result.allocations) == 0
        assert result.total_allocated_w == 0.0

    def test_zero_watts_returns_zero(self, balancer: BatteryBalancer) -> None:
        result = balancer.allocate([_kontor(), _forrad()], total_watts=0.0)
        assert result.total_allocated_w == 0.0


# ===========================================================================
# Convergence correction (B5 regression)
# ===========================================================================


class TestConvergenceCorrection:
    """B5 regression: diverged K/F SoC should converge."""

    def test_diverged_soc_corrected(self, balancer: BatteryBalancer) -> None:
        """K=80%, F=40% — K should get MORE discharge (ahead of target)."""
        k = _kontor(soc_pct=80.0)
        f = _forrad(soc_pct=40.0)
        result = balancer.allocate([k, f], total_watts=4000.0)
        alloc = result.allocation_map

        # K is ahead (higher SoC) → correction > 1.0 → more discharge
        assert alloc["kontor"].correction_factor > 1.0
        # F is behind (lower SoC) → correction < 1.0 → less discharge
        assert alloc["forrad"].correction_factor < 1.0

    def test_equal_soc_no_correction(self, balancer: BatteryBalancer) -> None:
        """At equal SoC, no correction needed."""
        result = balancer.allocate(
            [_kontor(soc_pct=60.0), _forrad(soc_pct=60.0)],
            total_watts=4000.0,
        )
        for a in result.allocations:
            assert a.correction_factor == pytest.approx(1.0)

    def test_small_divergence_within_deadband(self, balancer: BatteryBalancer) -> None:
        """Within 2% deadband — no correction."""
        k = _kontor(soc_pct=61.0)
        f = _forrad(soc_pct=60.0)
        result = balancer.allocate([k, f], total_watts=4000.0)
        for a in result.allocations:
            assert a.correction_factor == pytest.approx(1.0)


# ===========================================================================
# Cold derating
# ===========================================================================


class TestColdDerating:
    """Cold weather discharge derating."""

    def test_cold_50pct_derating(self, balancer: BatteryBalancer) -> None:
        """Below 4°C but above 0°C → 50% derating."""
        k = _kontor(cell_temp_c=2.0)
        result = balancer.allocate([k], total_watts=4000.0)
        assert result.allocations[0].watts == pytest.approx(2000, abs=50)
        assert result.allocations[0].cold_derated

    def test_freeze_blocked(self, balancer: BatteryBalancer) -> None:
        """Below 0°C → blocked (0W)."""
        k = _kontor(cell_temp_c=-5.0)
        result = balancer.allocate([k], total_watts=4000.0)
        assert result.allocations[0].watts == 0
        assert result.allocations[0].cold_derated

    def test_warm_no_derating(self, balancer: BatteryBalancer) -> None:
        """Above 4°C → no derating."""
        k = _kontor(cell_temp_c=20.0)
        result = balancer.allocate([k], total_watts=4000.0)
        assert result.allocations[0].watts == 4000
        assert not result.allocations[0].cold_derated

    def test_one_cold_one_warm(self, balancer: BatteryBalancer) -> None:
        """Cold battery derated, warm gets remainder."""
        k = _kontor(cell_temp_c=2.0)  # 50% derating
        f = _forrad(cell_temp_c=20.0)  # Normal
        result = balancer.allocate([k, f], total_watts=4000.0)
        alloc = result.allocation_map
        # K: 75% * 4000 * 0.5 = 1500
        assert alloc["kontor"].cold_derated
        assert not alloc["forrad"].cold_derated


# ===========================================================================
# SoH derating
# ===========================================================================


class TestSoHDerating:
    """SoH-based floor raising."""

    def test_soh_80_raises_floor(self, balancer: BatteryBalancer) -> None:
        """SoH < 80% → floor +5%."""
        k = _kontor(soc_pct=18.0, soh_pct=75.0)
        # Floor = 15% + 5% = 20%, SoC 18% < 20% → 0 available
        result = balancer.allocate([k], total_watts=3000.0)
        assert result.allocations[0].watts == 0
        assert result.allocations[0].effective_floor_pct == 20.0

    def test_soh_70_raises_floor_more(self, balancer: BatteryBalancer) -> None:
        """SoH < 70% → floor +10%."""
        k = _kontor(soc_pct=23.0, soh_pct=65.0)
        # Floor = 15% + 10% = 25%, SoC 23% < 25% → 0 available
        result = balancer.allocate([k], total_watts=3000.0)
        assert result.allocations[0].watts == 0
        assert result.allocations[0].effective_floor_pct == 25.0

    def test_good_soh_normal_floor(self, balancer: BatteryBalancer) -> None:
        """SoH 100% → normal 15% floor."""
        k = _kontor(soc_pct=20.0, soh_pct=100.0)
        result = balancer.allocate([k], total_watts=3000.0)
        assert result.allocations[0].effective_floor_pct == 15.0
        assert result.allocations[0].watts > 0


# ===========================================================================
# Charging mode
# ===========================================================================


class TestChargingAllocation:
    """Charging: lower SoC gets more charge."""

    def test_lower_soc_gets_more_charge(self, balancer: BatteryBalancer) -> None:
        """F at 30% should get proportionally more charge than K at 80%."""
        k = _kontor(soc_pct=80.0)  # 20% empty = 3.0 kWh space
        f = _forrad(soc_pct=30.0)  # 70% empty = 3.5 kWh space
        result = balancer.allocate([k, f], total_watts=3000.0, is_charging=True)
        alloc = result.allocation_map
        # F has more empty space → gets more charge
        assert alloc["forrad"].watts > alloc["kontor"].watts

    def test_full_battery_gets_zero_charge(self, balancer: BatteryBalancer) -> None:
        """Battery at 100% should get 0 charge."""
        k = _kontor(soc_pct=100.0)
        f = _forrad(soc_pct=50.0)
        result = balancer.allocate([k, f], total_watts=3000.0, is_charging=True)
        alloc = result.allocation_map
        assert alloc["kontor"].watts == 0
        assert alloc["forrad"].watts > 0


# ===========================================================================
# CT compensation
# ===========================================================================


class TestCTCompensation:
    """CT placement compensation for Kontor local_load."""

    def test_pv_covers_local_load(self, balancer: BatteryBalancer) -> None:
        """When PV covers Kontor local load, Kontor needs explicit ems_power_limit."""
        k = _kontor(local_load_w=500.0, pv_power_w=3000.0)
        f = _forrad()
        kontor_w, forrad_w = balancer.ct_compensation(k, f, grid_import_w=2000.0)
        # Total need = max(0, 500-3000) + max(0, 2000) = 0 + 2000 = 2000
        # Kontor share: 15/(15+5) = 75% → 1500
        assert kontor_w == pytest.approx(1500, abs=50)
        assert forrad_w == pytest.approx(500, abs=50)

    def test_no_grid_import_no_need(self, balancer: BatteryBalancer) -> None:
        """No grid import → no discharge needed."""
        k = _kontor(local_load_w=500.0, pv_power_w=3000.0)
        f = _forrad()
        kontor_w, forrad_w = balancer.ct_compensation(k, f, grid_import_w=0.0)
        assert kontor_w == 0
        assert forrad_w == 0

    def test_no_pv_kontor_sees_load(self, balancer: BatteryBalancer) -> None:
        """Without PV, Kontor CT sees full local load."""
        k = _kontor(local_load_w=1000.0, pv_power_w=0.0)
        f = _forrad()
        kontor_w, forrad_w = balancer.ct_compensation(k, f, grid_import_w=1500.0)
        # Total = 1000 + 1500 = 2500, K=75%=1875, F=25%=625
        assert kontor_w == pytest.approx(1875, abs=50)
        assert forrad_w == pytest.approx(625, abs=50)


# ===========================================================================
# Clamping
# ===========================================================================


class TestClamping:
    """Test per-battery max limit clamping."""

    def test_clamped_to_max_discharge(self, balancer: BatteryBalancer) -> None:
        """Allocation should not exceed max_discharge_w."""
        k = _kontor(max_discharge_w=2000.0)
        result = balancer.allocate([k], total_watts=5000.0)
        assert result.allocations[0].watts == 2000
        assert result.allocations[0].clamped

    def test_within_max_not_clamped(self, balancer: BatteryBalancer) -> None:
        k = _kontor(max_discharge_w=5000.0)
        result = balancer.allocate([k], total_watts=3000.0)
        assert result.allocations[0].watts == 3000
        assert not result.allocations[0].clamped


# ===========================================================================
# All at floor
# ===========================================================================


class TestAllAtFloor:
    """When all batteries are at or below floor."""

    def test_all_at_floor_zero_allocation(self, balancer: BatteryBalancer) -> None:
        k = _kontor(soc_pct=14.0)
        f = _forrad(soc_pct=14.0)
        result = balancer.allocate([k, f], total_watts=4000.0)
        assert result.total_allocated_w == 0.0
        for a in result.allocations:
            assert a.watts == 0
