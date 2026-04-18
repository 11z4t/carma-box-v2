"""Battery arbitrage optimizer — greedy v1 (PLAT-1724).

Given a 24-hour-ish horizon of Nordpool prices, PV forecast, and house
baseline load, compute an HOURLY plan for the battery that maximises
saved öre. The plan can override zero-grid during specific windows:

  - ``discharge`` at hours where selling stored energy beats
    importing from grid
  - ``charge``    at hours where the cheap spot makes grid-charge
    profitable (even after round-trip losses)
  - ``hold``      otherwise; zero-grid takes over inside the hour

Greedy v1 ignores battery chemistry non-linearities and uses
deterministic allocation. An LP variant (cvxpy) can replace this with
the same public API for the v2 optimizer.

Pure module. No I/O. Deterministic given the same inputs. Consumed by
a future ``PlanExecutor`` layer that turns the plan into per-cycle
overrides for zero_grid.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArbitrageConfig:
    """Tunables — kund-agnostisk, all from site.yaml."""

    round_trip_efficiency: float = 0.9          # inverter + chem losses
    fees_ore_per_kwh: float = 65.0              # energiskatt + nät + påslag
    vat_rate: float = 0.25
    # Minimum arbitrage benefit per kWh before committing to a cycle.
    # Guards against cycling bat for a few öre (bat wear costs > gain).
    min_benefit_ore_per_kwh: float = 20.0
    # Allow grid-charge from a cheap hour into a peak hour. If disabled,
    # plan only discharges — charge always comes from PV surplus.
    allow_grid_charge: bool = True


@dataclass(frozen=True)
class HourPlan:
    """What to do at a single hour in the horizon."""

    hour_offset: int            # 0 = current hour, 1 = next, ...
    action: str                 # "discharge" | "charge" | "hold"
    power_kw: float
    price_ore: float
    reason: str


@dataclass(frozen=True)
class ArbitragePlan:
    """Output of one optimiser run."""

    hours: tuple[HourPlan, ...]
    total_saving_ore: float
    reason: str


def _effective_import_cost_ore(spot_ore: float, cfg: ArbitrageConfig) -> float:
    """Total paid per imported kWh (spot + fees + VAT)."""
    return (spot_ore + cfg.fees_ore_per_kwh) * (1.0 + cfg.vat_rate)


def plan_arbitrage(
    prices_ore: list[float],
    pv_forecast_kw_per_h: list[float],
    house_baseline_kw_per_h: list[float],
    bat_soc_now_pct: float,
    bat_capacity_kwh: float,
    soc_floor_pct: float,
    soc_ceiling_pct: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    cfg: ArbitrageConfig,
) -> ArbitragePlan:
    """Greedy arbitrage plan across the price horizon.

    Steps:
      1. Compute per-hour net import = max(0, house - pv).
      2. Rank hours by IMPORT COST descending; allocate bat discharge
         to the top hours until bat would hit floor.
      3. If ``allow_grid_charge``: rank hours by IMPORT COST ascending;
         if cheapest future cost + efficiency loss < best already-planned
         discharge price, schedule grid-charge at the cheap hour.
      4. Emit a HourPlan per slot — unscheduled hours are "hold".

    The plan speaks in kW per hour (energy per slot equals power × 1h).
    Caller enforces the plan per 15-s cycle by biasing zero-grid's
    target_grid_w toward the planned direction.
    """
    n = min(
        len(prices_ore),
        len(pv_forecast_kw_per_h),
        len(house_baseline_kw_per_h),
    )
    if n == 0:
        return ArbitragePlan(
            hours=(),
            total_saving_ore=0.0,
            reason="arbitrage: no forecast horizon",
        )

    bat_available_kwh = max(
        0.0, (bat_soc_now_pct - soc_floor_pct) / 100.0 * bat_capacity_kwh,
    )
    bat_headroom_kwh = max(
        0.0, (soc_ceiling_pct - bat_soc_now_pct) / 100.0 * bat_capacity_kwh,
    )

    net_import_kw = [
        max(0.0, house_baseline_kw_per_h[h] - pv_forecast_kw_per_h[h])
        for h in range(n)
    ]
    import_cost_ore = [
        _effective_import_cost_ore(prices_ore[h], cfg) for h in range(n)
    ]

    discharges_kw: list[float] = [0.0] * n
    charges_kw: list[float] = [0.0] * n

    # Greedy discharge: allocate to highest-cost hours first.
    sorted_by_cost_desc = sorted(range(n), key=lambda h: -import_cost_ore[h])
    remaining_energy_kwh = bat_available_kwh
    for h in sorted_by_cost_desc:
        if remaining_energy_kwh <= 0:
            break
        slot_demand_kw = min(max_discharge_kw, net_import_kw[h])
        slot_energy_kwh = min(slot_demand_kw, remaining_energy_kwh)
        if slot_energy_kwh <= 0:
            continue
        # Discharge only if saved import cost > wear threshold.
        if import_cost_ore[h] < cfg.min_benefit_ore_per_kwh:
            continue
        discharges_kw[h] = slot_energy_kwh
        remaining_energy_kwh -= slot_energy_kwh

    # Greedy grid-charge: cheapest hour → planned discharge hour, iff
    # the spread exceeds round-trip loss + wear threshold.
    if cfg.allow_grid_charge and bat_headroom_kwh > 0:
        remaining_headroom = bat_headroom_kwh
        sorted_by_cost_asc = sorted(range(n), key=lambda h: import_cost_ore[h])
        planned_discharge_best_ore = max(
            (import_cost_ore[h] for h in range(n) if discharges_kw[h] > 0),
            default=0.0,
        )
        for h in sorted_by_cost_asc:
            if remaining_headroom <= 0:
                break
            if discharges_kw[h] > 0:
                continue  # same hour — no same-slot bidirectional
            cheap_cost = import_cost_ore[h]
            break_even = cheap_cost / cfg.round_trip_efficiency
            if break_even + cfg.min_benefit_ore_per_kwh > planned_discharge_best_ore:
                break  # sorted asc → no cheaper slot will beat it
            slot_capacity_kw = min(max_charge_kw, remaining_headroom)
            if slot_capacity_kw <= 0:
                continue
            charges_kw[h] = slot_capacity_kw
            remaining_headroom -= slot_capacity_kw

    # Total saving estimate (discharge revenue − charge cost).
    saving_ore = 0.0
    for h in range(n):
        if discharges_kw[h] > 0:
            saving_ore += discharges_kw[h] * import_cost_ore[h]
        if charges_kw[h] > 0:
            # Cost to import extra for charge, minus recovered on later
            # discharge (already counted above). Subtract charging cost.
            saving_ore -= charges_kw[h] * import_cost_ore[h]

    hours = tuple(
        HourPlan(
            hour_offset=h,
            action=(
                "discharge" if discharges_kw[h] > 0
                else "charge" if charges_kw[h] > 0
                else "hold"
            ),
            power_kw=(
                discharges_kw[h] if discharges_kw[h] > 0
                else charges_kw[h]
            ),
            price_ore=prices_ore[h],
            reason=(
                f"discharge @ {import_cost_ore[h]:.0f} öre (top slot)"
                if discharges_kw[h] > 0
                else f"charge @ {import_cost_ore[h]:.0f} öre (cheap slot)"
                if charges_kw[h] > 0
                else "hold — no arbitrage"
            ),
        )
        for h in range(n)
    )
    return ArbitragePlan(
        hours=hours,
        total_saving_ore=saving_ore,
        reason=(
            f"arbitrage: {sum(1 for hp in hours if hp.action == 'discharge')} "
            f"discharge + {sum(1 for hp in hours if hp.action == 'charge')} "
            f"charge hours, estimated saving {saving_ore:.0f} öre"
        ),
    )
