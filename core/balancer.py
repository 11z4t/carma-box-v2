"""K/F Battery Balancer for CARMA Box.

Distributes discharge/charge power proportionally to available energy
so both batteries (Kontor 15 kWh + Förråd 5 kWh) reach min_soc
simultaneously. Handles:

- Proportional allocation by available_kwh (above effective_min_soc)
- Convergence correction when SoC has diverged
- Cold derating (50% at <4°C, blocked at <0°C)
- SoH derating (+5%/+10% floor raise)
- CT placement compensation (Kontor local_load vs Förråd house_grid)
- Charging mode: inverse allocation (lower SoC → more charge)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.models import CTPlacement, MAX_SOC_PCT, effective_min_soc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BalancerConfig:
    """Balancer thresholds — all from site.yaml, zero hardcoding."""

    # SoC floor thresholds (shared with GuardConfig)
    normal_floor_pct: float = 15.0
    cold_floor_pct: float = 20.0
    freeze_floor_pct: float = 25.0
    cold_temp_c: float = 4.0
    freeze_temp_c: float = 0.0

    # SoH derating
    soh_warn_pct: float = 80.0
    soh_crit_pct: float = 70.0
    soh_warn_raise_pct: float = 5.0
    soh_crit_raise_pct: float = 10.0

    # Convergence correction
    correction_deadband_pct: float = 2.0   # No correction within this
    correction_scale: float = 20.0         # delta/scale = correction factor
    correction_max: float = 1.5            # Max correction multiplier
    correction_min: float = 0.5            # Min correction multiplier

    # Cold derating factors
    cold_derating_factor: float = 0.5      # 50% at cold
    freeze_derating_factor: float = 0.0    # 0% at freeze (blocked)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatteryInfo:
    """Input to the balancer — one per battery."""

    battery_id: str
    soc_pct: float
    cap_kwh: float
    cell_temp_c: float
    soh_pct: float
    max_discharge_w: float
    max_charge_w: float
    ct_placement: CTPlacement
    local_load_w: float = 0.0   # Only meaningful for local_load CT
    pv_power_w: float = 0.0     # PV production on this inverter


@dataclass(frozen=True)
class BatteryAllocation:
    """Output per battery from the balancer."""

    battery_id: str
    watts: int               # Target discharge/charge watts
    share_pct: float         # Percentage of total allocated
    effective_floor_pct: float
    available_kwh: float
    correction_factor: float
    cold_derated: bool
    clamped: bool            # True if hit max limit


@dataclass(frozen=True)
class BalanceResult:
    """Complete balancer output."""

    allocations: list[BatteryAllocation]
    total_requested_w: float
    total_allocated_w: float
    is_charging: bool

    @property
    def allocation_map(self) -> dict[str, BatteryAllocation]:
        """Map battery_id → allocation for quick lookup."""
        return {a.battery_id: a for a in self.allocations}


# ---------------------------------------------------------------------------
# Balancer
# ---------------------------------------------------------------------------


class BatteryBalancer:
    """Proportional K/F battery balancer.

    Pure function design: allocate() takes inputs, returns result.
    No side effects, no I/O, no state between calls.
    """

    def __init__(self, config: BalancerConfig | None = None) -> None:
        self._config = config or BalancerConfig()

    def allocate(
        self,
        batteries: list[BatteryInfo],
        total_watts: float,
        is_charging: bool = False,
    ) -> BalanceResult:
        """Allocate discharge/charge watts across batteries.

        Args:
            batteries: Current state of each battery.
            total_watts: Total watts to distribute (always positive).
            is_charging: True for charging allocation (lower SoC → more).

        Returns:
            BalanceResult with per-battery allocations.
        """
        if not batteries or total_watts <= 0:
            return BalanceResult(
                allocations=[],
                total_requested_w=total_watts,
                total_allocated_w=0.0,
                is_charging=is_charging,
            )

        # Step 1: Calculate effective floor and available energy per battery
        floors: dict[str, float] = {}
        available: dict[str, float] = {}

        for bat in batteries:
            floor = self._effective_min_soc(bat)
            floors[bat.battery_id] = floor

            if is_charging:
                # Charging: available = empty space (100% - soc)
                available[bat.battery_id] = max(
                    0.0, (MAX_SOC_PCT - bat.soc_pct) / MAX_SOC_PCT * bat.cap_kwh
                )
            else:
                # Discharging: available = energy above floor
                available[bat.battery_id] = max(
                    0.0, (bat.soc_pct - floor) / MAX_SOC_PCT * bat.cap_kwh
                )

        # Step 2: Calculate total available
        total_available = sum(available.values())

        if total_available <= 0:
            # All batteries at floor (discharge) or full (charge)
            return BalanceResult(
                allocations=[
                    BatteryAllocation(
                        battery_id=b.battery_id,
                        watts=0,
                        share_pct=0.0,
                        effective_floor_pct=floors[b.battery_id],
                        available_kwh=0.0,
                        correction_factor=1.0,
                        cold_derated=False,
                        clamped=False,
                    )
                    for b in batteries
                ],
                total_requested_w=total_watts,
                total_allocated_w=0.0,
                is_charging=is_charging,
            )

        # Step 3: Calculate base share per battery
        shares: dict[str, float] = {
            bid: avail / total_available
            for bid, avail in available.items()
        }

        # Step 4: Apply convergence correction
        corrections = self._correction_factors(batteries, is_charging)

        # Step 5: Allocate, derate, and clamp
        allocations: list[BatteryAllocation] = []
        total_allocated = 0.0

        for bat in batteries:
            bid = bat.battery_id
            base_w = total_watts * shares[bid] * corrections[bid]

            # Cold derating
            cold_derated = False
            if bat.cell_temp_c < self._config.freeze_temp_c:
                base_w = base_w * self._config.freeze_derating_factor
                cold_derated = True
            elif bat.cell_temp_c < self._config.cold_temp_c:
                base_w = base_w * self._config.cold_derating_factor
                cold_derated = True

            # Clamp to per-battery max
            max_w = bat.max_charge_w if is_charging else bat.max_discharge_w
            clamped = base_w > max_w
            final_w = int(min(base_w, max_w))

            total_allocated += final_w
            allocations.append(BatteryAllocation(
                battery_id=bid,
                watts=final_w,
                share_pct=shares[bid] * 100.0,
                effective_floor_pct=floors[bid],
                available_kwh=available[bid],
                correction_factor=corrections[bid],
                cold_derated=cold_derated,
                clamped=clamped,
            ))

        return BalanceResult(
            allocations=allocations,
            total_requested_w=total_watts,
            total_allocated_w=total_allocated,
            is_charging=is_charging,
        )

    def ct_compensation(
        self,
        kontor: BatteryInfo,
        forrad: BatteryInfo,
        grid_import_w: float,
    ) -> tuple[int, int]:
        """Compensate for CT placement asymmetry.

        Kontor CT on local_load: sees only local demand (miner, VP).
        Förråd CT on house_grid: sees total house import.

        When PV covers Kontor local load, Kontor's CT sees 0W demand
        and won't discharge. We calculate Kontor's share of total need
        and return (kontor_ems_limit, forrad_ems_limit).
        """
        # What Kontor's CT sees
        kontor_ct_demand = max(0.0, kontor.local_load_w - kontor.pv_power_w)

        # What the house needs (Förråd's CT sees this)
        house_need = max(0.0, grid_import_w)

        total_need = kontor_ct_demand + house_need
        if total_need <= 0:
            return (0, 0)

        # Proportional by capacity
        total_cap = kontor.cap_kwh + forrad.cap_kwh
        kontor_share = kontor.cap_kwh / total_cap
        forrad_share = forrad.cap_kwh / total_cap

        kontor_target = int(total_need * kontor_share)
        forrad_target = int(total_need * forrad_share)

        return (kontor_target, forrad_target)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _effective_min_soc(self, bat: BatteryInfo) -> float:
        """Calculate effective minimum SoC considering temperature and SoH.

        H3: Delegates to the shared pure function in core.models to avoid
        logic duplication between BatteryBalancer and GridGuard.
        """
        return effective_min_soc(bat.cell_temp_c, bat.soh_pct, self._config)

    def _correction_factors(
        self,
        batteries: list[BatteryInfo],
        is_charging: bool,
    ) -> dict[str, float]:
        """Calculate convergence correction factors when SoC has diverged.

        For discharge: lagging battery (higher SoC) gets more discharge.
        For charging: lagging battery (lower SoC) gets more charge.

        Returns multiplier per battery: >1.0 for more, <1.0 for less.
        """
        if len(batteries) < 2:
            return {b.battery_id: 1.0 for b in batteries}

        cfg = self._config

        # Capacity-weighted target SoC
        total_cap = sum(b.cap_kwh for b in batteries)
        target_soc = sum(b.soc_pct * b.cap_kwh for b in batteries) / total_cap

        factors: dict[str, float] = {}
        for b in batteries:
            delta = target_soc - b.soc_pct
            # delta > 0: battery is behind target (lower SoC)
            # delta < 0: battery is ahead of target (higher SoC)

            if abs(delta) < cfg.correction_deadband_pct:
                factors[b.battery_id] = 1.0
            else:
                if is_charging:
                    # Charging: behind (delta>0) should get MORE charge
                    raw = 1.0 + (delta / cfg.correction_scale)
                else:
                    # Discharge: ahead (delta<0, so -delta>0) should get MORE discharge
                    raw = 1.0 + (-delta / cfg.correction_scale)

                factors[b.battery_id] = max(
                    cfg.correction_min,
                    min(cfg.correction_max, raw),
                )

        return factors
