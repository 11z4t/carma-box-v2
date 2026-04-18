"""Multi-night EV charging planner for CARMA Box (PLAT-1662).

Pure functions — no I/O, no side effects. Takes energy state and per-night
price schedules and returns a night-by-night charging plan that:
  - Spreads charging over multiple nights when the SoC gap exceeds one night's capacity.
  - Prioritises the cheapest nights (lowest average spot price).
  - Never plans beyond MAX_NIGHTS.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Named constants — zero magic numbers
# ---------------------------------------------------------------------------

MAX_NIGHTS: int = 3
"""Maximum number of nights to spread EV charging across."""

_PCT_TO_FRACTION: float = 100.0
"""Divisor to convert percentage → fraction (e.g. 80 / 100 = 0.80)."""

_PRICE_SENTINEL_ORE: float = 9_999.0
"""Fallback price when a night has no price data (sorts to last)."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NightChargeTarget:
    """Charging target for one specific night.

    Attributes:
        night_index: 0 = tonight, 1 = tomorrow night, etc.
        kwh_to_charge: Energy to add this night (net, before efficiency loss).
        avg_price_ore: Average spot price for this night (öre/kWh).
        soc_after_pct: Estimated EV SoC at end of this night's charge.
    """

    night_index: int
    kwh_to_charge: float
    avg_price_ore: float
    soc_after_pct: float


@dataclass(frozen=True)
class MultinightEVPlan:
    """Result of calculate_ev_multinight_plan().

    Attributes:
        nights: Ordered list of NightChargeTarget (night_index ascending).
        total_kwh: Sum of all kwh_to_charge across nights.
        nights_needed: Number of nights that carry non-zero charge.
        reached_target: True if target_soc_pct is reachable within MAX_NIGHTS.
        final_soc_pct: Projected SoC after all planned nights.
    """

    nights: list[NightChargeTarget]
    total_kwh: float
    nights_needed: int
    reached_target: bool
    final_soc_pct: float


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def calculate_ev_multinight_plan(
    ev_soc_pct: float,
    ev_capacity_kwh: float,
    target_soc_pct: float,
    ev_charge_kw: float,
    ev_efficiency: float,
    charge_hours_per_night: float,
    prices_by_night: list[dict[int, float]],
) -> MultinightEVPlan:
    """Calculate a multi-night EV charging plan.

    If the energy gap fits in a single night the plan has one entry.
    If not, charging is spread across nights in ascending price order
    (cheapest night first) up to MAX_NIGHTS.

    Args:
        ev_soc_pct:             Current EV state-of-charge (%).
        ev_capacity_kwh:        Usable battery capacity (kWh).
        target_soc_pct:         Desired SoC at completion (%).
        ev_charge_kw:           Continuous charge rate (kW, e.g. 6.9 for 3×10A).
        ev_efficiency:          Charge efficiency, 0 < η ≤ 1 (e.g. 0.92).
        charge_hours_per_night: Available charging window each night (hours).
        prices_by_night:        List of {hour: öre/kWh} dicts, index = night offset.
                                Index 0 = tonight, 1 = tomorrow night, etc.
                                Must contain at least 1 entry.

    Returns:
        MultinightEVPlan with per-night targets.
    """
    energy_gap_kwh = _energy_gap_kwh(
        ev_soc_pct, target_soc_pct, ev_capacity_kwh
    )

    if energy_gap_kwh <= 0.0:
        # Already at or above target — empty plan.
        return MultinightEVPlan(
            nights=[],
            total_kwh=0.0,
            nights_needed=0,
            reached_target=True,
            final_soc_pct=ev_soc_pct,
        )

    max_kwh_per_night = _max_kwh_one_night(
        ev_charge_kw, charge_hours_per_night, ev_efficiency
    )

    night_slots = _build_night_slots(prices_by_night, max_kwh_per_night)

    raw_nights, total_kwh = _allocate(energy_gap_kwh, night_slots)

    # Fill in soc_after_pct for each night in chronological order.
    resolved_nights = _resolve_soc_per_night(raw_nights, ev_soc_pct, ev_capacity_kwh)

    reached = total_kwh >= energy_gap_kwh - _ALLOCATION_TOLERANCE
    final_soc = resolved_nights[-1].soc_after_pct if resolved_nights else ev_soc_pct

    return MultinightEVPlan(
        nights=resolved_nights,
        total_kwh=round(total_kwh, _ROUND_KWH),
        nights_needed=sum(1 for n in resolved_nights if n.kwh_to_charge > 0.0),
        reached_target=reached,
        final_soc_pct=round(final_soc, _ROUND_SOC),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ALLOCATION_TOLERANCE: float = 0.01
"""kWh tolerance for "reached target" check (floating-point guard)."""

_ROUND_KWH: int = 2
"""Decimal places for kWh values in results."""

_ROUND_SOC: int = 1
"""Decimal places for SoC values in results."""


def _energy_gap_kwh(
    current_soc_pct: float,
    target_soc_pct: float,
    capacity_kwh: float,
) -> float:
    """Energy needed to reach target_soc from current_soc (kWh, >= 0)."""
    gap_pct = max(0.0, target_soc_pct - current_soc_pct)
    return (gap_pct / _PCT_TO_FRACTION) * capacity_kwh


def _max_kwh_one_night(
    charge_kw: float,
    charge_hours: float,
    efficiency: float,
) -> float:
    """Maximum usable energy deliverable in one night window (kWh)."""
    return charge_kw * charge_hours * efficiency


def _avg_price(prices: dict[int, float]) -> float:
    """Average öre/kWh for a night's price dict, or sentinel if empty."""
    if not prices:
        return _PRICE_SENTINEL_ORE
    return sum(prices.values()) / len(prices)


@dataclass
class _NightSlot:
    """Mutable scratch record used during allocation."""

    night_index: int
    max_kwh: float
    avg_price_ore: float


def _build_night_slots(
    prices_by_night: list[dict[int, float]],
    max_kwh_per_night: float,
) -> list[_NightSlot]:
    """Build price-sorted night slots up to MAX_NIGHTS."""
    slots = [
        _NightSlot(
            night_index=i,
            max_kwh=max_kwh_per_night,
            avg_price_ore=_avg_price(prices_by_night[i]),
        )
        for i in range(min(len(prices_by_night), MAX_NIGHTS))
    ]
    # Sort cheapest-first; stable sort preserves night_index order on ties.
    slots.sort(key=lambda s: s.avg_price_ore)
    return slots


def _soc_after_charge(
    start_soc_pct: float,
    added_kwh: float,
    capacity_kwh: float,
) -> float:
    """SoC (%) after adding added_kwh to a battery of capacity_kwh."""
    return start_soc_pct + (added_kwh / capacity_kwh) * _PCT_TO_FRACTION


def _allocate(
    energy_gap_kwh: float,
    slots: list[_NightSlot],
) -> tuple[list[NightChargeTarget], float]:
    """Greedily fill cheapest-first slots until gap is covered or slots exhausted."""
    remaining = energy_gap_kwh
    targets: list[NightChargeTarget] = []
    total_charged = 0.0

    # We track a running SoC so each night's soc_after_pct is meaningful.
    # We don't have direct access to ev_soc_pct here, so soc_after_pct is
    # expressed as "cumulative kWh charged so far" — the caller resolves
    # absolute SoC. We use a placeholder that the public function fills in.
    cumulative_kwh = 0.0

    for slot in slots:
        if remaining <= 0.0:
            break
        charge_this_night = min(slot.max_kwh, remaining)
        cumulative_kwh += charge_this_night
        targets.append(
            NightChargeTarget(
                night_index=slot.night_index,
                kwh_to_charge=round(charge_this_night, _ROUND_KWH),
                avg_price_ore=round(slot.avg_price_ore, _ROUND_SOC),
                # soc_after_pct placeholder — resolved in calculate_ev_multinight_plan
                soc_after_pct=0.0,
            )
        )
        remaining -= charge_this_night
        total_charged += charge_this_night

    return targets, total_charged


def _resolve_soc_per_night(
    nights: list[NightChargeTarget],
    start_soc_pct: float,
    ev_capacity_kwh: float,
) -> list[NightChargeTarget]:
    """Return new list with soc_after_pct filled in for each night in index order."""
    sorted_nights = sorted(nights, key=lambda n: n.night_index)
    resolved: list[NightChargeTarget] = []
    running_soc = start_soc_pct
    for night in sorted_nights:
        running_soc = min(
            _soc_after_charge(running_soc, night.kwh_to_charge, ev_capacity_kwh),
            _PCT_TO_FRACTION,
        )
        resolved.append(
            NightChargeTarget(
                night_index=night.night_index,
                kwh_to_charge=night.kwh_to_charge,
                avg_price_ore=night.avg_price_ore,
                soc_after_pct=round(running_soc, _ROUND_SOC),
            )
        )
    return resolved
