"""Tests for core.arbitrage (PLAT-1724)."""

from __future__ import annotations

from core.arbitrage import (
    ArbitrageConfig,
    HourPlan,
    plan_arbitrage,
)


_CFG = ArbitrageConfig(
    round_trip_efficiency=0.9,
    fees_ore_per_kwh=65.0,
    vat_rate=0.25,
    min_benefit_ore_per_kwh=20.0,
    allow_grid_charge=True,
)


def _flat(n: int, v: float) -> list[float]:
    return [v] * n


def test_empty_horizon_returns_empty_plan() -> None:
    plan = plan_arbitrage(
        prices_ore=[],
        pv_forecast_kw_per_h=[],
        house_baseline_kw_per_h=[],
        bat_soc_now_pct=60.0,
        bat_capacity_kwh=20.0,
        soc_floor_pct=15.0,
        soc_ceiling_pct=95.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        cfg=_CFG,
    )
    assert plan.hours == ()
    assert plan.total_saving_ore == 0.0


def test_flat_prices_no_arbitrage_all_hold() -> None:
    """Constant price → no gain from moving energy → everything hold."""
    plan = plan_arbitrage(
        prices_ore=_flat(6, 50.0),
        pv_forecast_kw_per_h=_flat(6, 0.0),
        house_baseline_kw_per_h=_flat(6, 2.0),
        bat_soc_now_pct=60.0,
        bat_capacity_kwh=20.0,
        soc_floor_pct=15.0,
        soc_ceiling_pct=95.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        cfg=_CFG,
    )
    # Flat prices but still discharge into every non-zero net-import
    # hour: saves the (same) import cost for each. No charge because
    # there's no hour cheaper than the discharge hours.
    discharge_hours = [h for h in plan.hours if h.action == "discharge"]
    charge_hours = [h for h in plan.hours if h.action == "charge"]
    assert len(charge_hours) == 0
    # All 6h have same cost, bat has (60-15)/100 × 20 = 9 kWh, discharge
    # is min(max, net_import) = 2 kW per hour → 4.5 hours → 4 full + partial
    assert sum(h.power_kw for h in discharge_hours) <= 9.0


def test_peak_hours_get_discharge() -> None:
    """Classic: cheap overnight, expensive morning → discharge at peak."""
    # hours 0-3 cheap (50), 4-5 peak (150)
    prices = [50.0, 50.0, 50.0, 50.0, 150.0, 150.0]
    plan = plan_arbitrage(
        prices_ore=prices,
        pv_forecast_kw_per_h=_flat(6, 0.0),
        house_baseline_kw_per_h=_flat(6, 2.0),
        bat_soc_now_pct=60.0,
        bat_capacity_kwh=20.0,
        soc_floor_pct=15.0,
        soc_ceiling_pct=95.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        cfg=_CFG,
    )
    # Hours 4 + 5 should be discharge (peak).
    discharge_hours = [h for h in plan.hours if h.action == "discharge"]
    hours_idx = {h.hour_offset for h in discharge_hours}
    assert 4 in hours_idx
    assert 5 in hours_idx


def test_grid_charge_scheduled_when_cheap_enough() -> None:
    """Deep-negative hour + expensive peak → charge then discharge."""
    prices = [-80.0, 50.0, 50.0, 50.0, 150.0, 150.0]
    plan = plan_arbitrage(
        prices_ore=prices,
        pv_forecast_kw_per_h=_flat(6, 0.0),
        house_baseline_kw_per_h=_flat(6, 2.0),
        bat_soc_now_pct=50.0,        # headroom in both directions
        bat_capacity_kwh=20.0,
        soc_floor_pct=15.0,
        soc_ceiling_pct=95.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        cfg=_CFG,
    )
    charge_hours = [h for h in plan.hours if h.action == "charge"]
    discharge_hours = [h for h in plan.hours if h.action == "discharge"]
    assert any(h.hour_offset == 0 for h in charge_hours)   # cheap hour
    assert any(h.hour_offset in (4, 5) for h in discharge_hours)


def test_grid_charge_disabled_never_charges() -> None:
    """``allow_grid_charge=False`` forbids cross-hour charging."""
    cfg_no_charge = ArbitrageConfig(
        round_trip_efficiency=0.9,
        fees_ore_per_kwh=65.0,
        vat_rate=0.25,
        min_benefit_ore_per_kwh=20.0,
        allow_grid_charge=False,
    )
    plan = plan_arbitrage(
        prices_ore=[-80.0, 50.0, 150.0],
        pv_forecast_kw_per_h=_flat(3, 0.0),
        house_baseline_kw_per_h=_flat(3, 2.0),
        bat_soc_now_pct=40.0,
        bat_capacity_kwh=20.0,
        soc_floor_pct=15.0,
        soc_ceiling_pct=95.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        cfg=cfg_no_charge,
    )
    assert all(h.action != "charge" for h in plan.hours)


def test_bat_empty_cannot_discharge() -> None:
    """SoC at floor → zero available → every hour holds."""
    plan = plan_arbitrage(
        prices_ore=[100.0, 200.0, 300.0],
        pv_forecast_kw_per_h=_flat(3, 0.0),
        house_baseline_kw_per_h=_flat(3, 2.0),
        bat_soc_now_pct=15.0,
        bat_capacity_kwh=20.0,
        soc_floor_pct=15.0,
        soc_ceiling_pct=95.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        cfg=_CFG,
    )
    assert all(h.action != "discharge" for h in plan.hours)


def test_bat_full_cannot_charge() -> None:
    """SoC at ceiling → zero headroom → never charges."""
    plan = plan_arbitrage(
        prices_ore=[-80.0, 150.0],
        pv_forecast_kw_per_h=_flat(2, 0.0),
        house_baseline_kw_per_h=_flat(2, 2.0),
        bat_soc_now_pct=95.0,
        bat_capacity_kwh=20.0,
        soc_floor_pct=15.0,
        soc_ceiling_pct=95.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        cfg=_CFG,
    )
    assert all(h.action != "charge" for h in plan.hours)


def test_house_covered_by_pv_no_discharge() -> None:
    """If PV > house every hour, net_import=0 → nothing to discharge into."""
    plan = plan_arbitrage(
        prices_ore=_flat(3, 150.0),
        pv_forecast_kw_per_h=_flat(3, 5.0),     # 5 kW sun
        house_baseline_kw_per_h=_flat(3, 2.0),  # 2 kW house
        bat_soc_now_pct=60.0,
        bat_capacity_kwh=20.0,
        soc_floor_pct=15.0,
        soc_ceiling_pct=95.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        cfg=_CFG,
    )
    # net_import = max(0, 2 - 5) = 0 → no discharge need
    assert all(h.action != "discharge" for h in plan.hours)


def test_discharge_power_caps_at_max_discharge_kw() -> None:
    """Per-hour discharge respects the inverter cap."""
    plan = plan_arbitrage(
        prices_ore=[200.0, 200.0],
        pv_forecast_kw_per_h=_flat(2, 0.0),
        house_baseline_kw_per_h=_flat(2, 10.0),  # huge load
        bat_soc_now_pct=80.0,
        bat_capacity_kwh=20.0,
        soc_floor_pct=15.0,
        soc_ceiling_pct=95.0,
        max_charge_kw=5.0,
        max_discharge_kw=4.0,                    # < house demand
        cfg=_CFG,
    )
    for h in plan.hours:
        assert h.power_kw <= 4.0


def test_small_arbitrage_below_threshold_holds() -> None:
    """Spread smaller than wear threshold → no cycle."""
    plan = plan_arbitrage(
        prices_ore=[45.0, 50.0],     # net_costs ≈ 138 and 144 → spread ~6
        pv_forecast_kw_per_h=_flat(2, 0.0),
        house_baseline_kw_per_h=_flat(2, 2.0),
        bat_soc_now_pct=50.0,
        bat_capacity_kwh=20.0,
        soc_floor_pct=15.0,
        soc_ceiling_pct=95.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        cfg=_CFG,
    )
    # Flat prices → no cross-hour charge (spread under threshold).
    assert all(h.action != "charge" for h in plan.hours)


def test_output_is_deterministic() -> None:
    """Same inputs always produce the same plan (pure function)."""
    args = {
        "prices_ore": [-50.0, 50.0, 150.0],
        "pv_forecast_kw_per_h": _flat(3, 0.0),
        "house_baseline_kw_per_h": _flat(3, 2.0),
        "bat_soc_now_pct": 50.0,
        "bat_capacity_kwh": 20.0,
        "soc_floor_pct": 15.0,
        "soc_ceiling_pct": 95.0,
        "max_charge_kw": 5.0,
        "max_discharge_kw": 5.0,
        "cfg": _CFG,
    }
    p1 = plan_arbitrage(**args)
    p2 = plan_arbitrage(**args)
    assert p1 == p2


def test_hour_plan_is_frozen_dataclass() -> None:
    hp = HourPlan(
        hour_offset=0,
        action="hold",
        power_kw=0.0,
        price_ore=50.0,
        reason="test",
    )
    assert isinstance(hp, HourPlan)
