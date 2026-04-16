"""Unit tests for core.bat_support_controller (PLAT-1674).

Verifies proportional bat support — both batteries reach min_soc simultaneously,
total discharge bounded by Ellevio tak, gracefully reduces when bat low.
"""

from __future__ import annotations

import pytest

from core.bat_support_controller import (
    BatInfo,
    BatSupportConfig,
    BatSupportInput,
    _available_kwh,
    _effective_min_soc,
    _proportional_shares,
    evaluate,
)
from core.models import CommandType, EMSMode


def _bat(
    *,
    bid: str = "kontor",
    soc: float = 50.0,
    cap: float = 15.0,
    temp: float = 15.0,
    max_w: float = 5000.0,
    mode: EMSMode = EMSMode.BATTERY_STANDBY,
) -> BatInfo:
    return BatInfo(
        battery_id=bid, soc_pct=soc, cap_kwh=cap,
        cell_temp_c=temp, max_discharge_w=max_w,
        current_mode=mode,
    )


@pytest.fixture
def cfg() -> BatSupportConfig:
    return BatSupportConfig()


# -------------------------------------------------------------------
# _effective_min_soc — temperature-aware
# -------------------------------------------------------------------


def test_effective_min_soc_warm(cfg: BatSupportConfig) -> None:
    bat = _bat(temp=15.0)
    assert _effective_min_soc(bat, cfg) == 15.0


def test_effective_min_soc_cold(cfg: BatSupportConfig) -> None:
    bat = _bat(temp=2.0)  # under cold_temp_c=4
    assert _effective_min_soc(bat, cfg) == 20.0


# -------------------------------------------------------------------
# _available_kwh
# -------------------------------------------------------------------


def test_available_kwh_basic(cfg: BatSupportConfig) -> None:
    bat = _bat(soc=50, cap=15)  # min=15 → headroom 35pp
    # 35/100 * 15 = 5.25 kWh
    assert _available_kwh(bat, cfg) == pytest.approx(5.25, abs=0.01)


def test_available_kwh_at_min(cfg: BatSupportConfig) -> None:
    bat = _bat(soc=15, cap=15)  # at min
    assert _available_kwh(bat, cfg) == 0


def test_available_kwh_below_min(cfg: BatSupportConfig) -> None:
    bat = _bat(soc=10, cap=15)  # below min — clamped 0
    assert _available_kwh(bat, cfg) == 0


# -------------------------------------------------------------------
# _proportional_shares — same-time-min-reached
# -------------------------------------------------------------------


def test_proportional_shares_simultaneous_min(cfg: BatSupportConfig) -> None:
    """Live 2026-04-16: kontor 52% / forrad 20%, caps 15/5.
    avail_kontor = 5.55, avail_forrad = 0.25 → shares ~96% / 4%
    """
    bats = [_bat(bid="kontor", soc=52, cap=15), _bat(bid="forrad", soc=20, cap=5)]
    shares = _proportional_shares(bats, cfg)
    assert shares["kontor"] == pytest.approx(0.957, abs=0.01)
    assert shares["forrad"] == pytest.approx(0.043, abs=0.01)
    assert shares["kontor"] + shares["forrad"] == pytest.approx(1.0)


def test_proportional_shares_equal(cfg: BatSupportConfig) -> None:
    """Equal SoC + same cap → 50/50 share."""
    bats = [_bat(bid="a", soc=50, cap=10), _bat(bid="b", soc=50, cap=10)]
    shares = _proportional_shares(bats, cfg)
    assert shares["a"] == pytest.approx(0.5)
    assert shares["b"] == pytest.approx(0.5)


def test_proportional_shares_one_at_min(cfg: BatSupportConfig) -> None:
    """One at min → other gets 100%."""
    bats = [_bat(bid="a", soc=50, cap=10), _bat(bid="b", soc=15, cap=10)]
    shares = _proportional_shares(bats, cfg)
    assert shares["a"] == 1.0
    assert shares["b"] == 0


def test_proportional_shares_all_at_min(cfg: BatSupportConfig) -> None:
    """All at min → 0 share."""
    bats = [_bat(bid="a", soc=15, cap=10), _bat(bid="b", soc=15, cap=10)]
    shares = _proportional_shares(bats, cfg)
    assert shares["a"] == 0
    assert shares["b"] == 0


# -------------------------------------------------------------------
# evaluate
# -------------------------------------------------------------------


def test_evaluate_disabled(cfg: BatSupportConfig) -> None:
    cfg2 = BatSupportConfig(enabled=False)
    inp = BatSupportInput(
        batteries=[_bat()], total_load_kw=8.0, grid_weighted_kw=4.0,
    )
    res = evaluate(inp, cfg2)
    assert res.commands == []
    assert "DISABLED" in res.reason


def test_evaluate_no_support_when_load_under_cap(cfg: BatSupportConfig) -> None:
    """total_load < tak_raw * margin → no bat discharge needed."""
    inp = BatSupportInput(
        batteries=[_bat(soc=50)], total_load_kw=4.0, grid_weighted_kw=2.0,
    )
    # cap_raw = (3.0/0.5) * 0.95 = 5.7 kW
    res = evaluate(inp, cfg)
    assert res.total_discharge_w == 0
    assert "NO_SUPPORT_NEEDED" in res.reason


def test_evaluate_supports_when_load_over_cap(cfg: BatSupportConfig) -> None:
    """total_load > tak_raw * margin → bat discharges to cover gap."""
    bats = [_bat(bid="kontor", soc=50, cap=15), _bat(bid="forrad", soc=30, cap=5)]
    inp = BatSupportInput(
        batteries=bats, total_load_kw=8.0, grid_weighted_kw=4.0,
    )
    # gap = 8.0 - 5.7 = 2.3 kW = 2300 W
    res = evaluate(inp, cfg)
    assert res.total_discharge_w > 0
    assert "SUPPORT" in res.reason


def test_evaluate_clamps_to_max_discharge(cfg: BatSupportConfig) -> None:
    """Per-battery clamp respected."""
    bats = [_bat(bid="kontor", soc=80, cap=15, max_w=2000)]  # cap 2 kW
    inp = BatSupportInput(
        batteries=bats, total_load_kw=20.0, grid_weighted_kw=10.0,  # huge load
    )
    res = evaluate(inp, cfg)
    assert res.per_battery_w["kontor"] <= 2000


def test_evaluate_emits_mode_change_if_needed(cfg: BatSupportConfig) -> None:
    """If bat in standby and discharge needed → SET_EMS_MODE issued."""
    bats = [_bat(bid="kontor", soc=50, cap=15, mode=EMSMode.BATTERY_STANDBY)]
    inp = BatSupportInput(
        batteries=bats, total_load_kw=8.0, grid_weighted_kw=4.0,
    )
    res = evaluate(inp, cfg)
    assert any(c.command_type == CommandType.SET_EMS_MODE for c in res.commands)


def test_evaluate_skips_mode_change_already_discharge(cfg: BatSupportConfig) -> None:
    """If bat already in discharge_pv → no mode change."""
    bats = [_bat(bid="kontor", soc=50, cap=15, mode=EMSMode.DISCHARGE_PV)]
    inp = BatSupportInput(
        batteries=bats, total_load_kw=8.0, grid_weighted_kw=4.0,
    )
    res = evaluate(inp, cfg)
    mode_cmds = [c for c in res.commands if c.command_type == CommandType.SET_EMS_MODE]
    assert mode_cmds == []
    # but power_limit should still be issued
    assert any(c.command_type == CommandType.SET_EMS_POWER_LIMIT for c in res.commands)


def test_evaluate_2026_04_16_live_scenario(cfg: BatSupportConfig) -> None:
    """Regression: replikera live disk-vågor situation kl 22:25.
    EV @ 9A 3-fas = 6.3 kW + disk 1.7 kW + baseload 0.5 kW = 8.5 kW total
    bats: kontor 50%, forrad 19% (live)
    """
    bats = [
        _bat(bid="kontor", soc=50, cap=15, mode=EMSMode.DISCHARGE_PV),
        _bat(bid="forrad", soc=19, cap=5, mode=EMSMode.DISCHARGE_PV),
    ]
    inp = BatSupportInput(
        batteries=bats, total_load_kw=8.5, grid_weighted_kw=4.0,
    )
    res = evaluate(inp, cfg)
    # cap_raw = 5.7 kW, gap = 2.8 kW = 2800W needed
    assert 2500 <= res.total_discharge_w <= 3000
    # forrad gets tiny share since avail = (19-15)*5/100 = 0.2 kWh
    # kontor = (50-15)*15/100 = 5.25 kWh → forrad share ~3.7%
    assert res.per_battery_w["forrad"] < 200
    assert res.per_battery_w["kontor"] > 2500


def test_evaluate_reason_always_set(cfg: BatSupportConfig) -> None:
    inp = BatSupportInput(
        batteries=[_bat()], total_load_kw=4.0, grid_weighted_kw=2.0,
    )
    res = evaluate(inp, cfg)
    assert res.reason != ""
