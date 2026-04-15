"""Tests for ConsumptionProfile — EMA learning + house consumption calc."""

from __future__ import annotations

from datetime import datetime

from core.consumption import (
    DEFAULT_CONSUMPTION_PROFILE,
    EMA_ALPHA,
    MIN_SAMPLES_FOR_LEARNED,
    ConsumptionProfile,
    calculate_house_consumption,
)


class TestConsumptionProfile:
    """ConsumptionProfile EMA learning."""

    def test_initial_profile_is_default(self) -> None:
        p = ConsumptionProfile()
        assert p.get_profile(is_weekend=False) == DEFAULT_CONSUMPTION_PROFILE
        assert p.get_profile(is_weekend=True) == DEFAULT_CONSUMPTION_PROFILE

    def test_not_learned_until_min_samples(self) -> None:
        p = ConsumptionProfile()
        assert not p.is_learned
        for i in range(MIN_SAMPLES_FOR_LEARNED - 1):
            p.update(i % 24, 3.0, is_weekend=False)
        assert not p.is_learned
        p.update(0, 3.0, is_weekend=False)
        assert p.is_learned

    def test_ema_update_weekday(self) -> None:
        p = ConsumptionProfile()
        old = p.weekday[10]
        p.update(10, 5.0, is_weekend=False)
        expected = EMA_ALPHA * 5.0 + (1 - EMA_ALPHA) * old
        assert abs(p.weekday[10] - expected) < 0.001

    def test_ema_update_weekend(self) -> None:
        p = ConsumptionProfile()
        old = p.weekend[10]
        p.update(10, 5.0, is_weekend=True)
        expected = EMA_ALPHA * 5.0 + (1 - EMA_ALPHA) * old
        assert abs(p.weekend[10] - expected) < 0.001

    def test_invalid_hour_ignored(self) -> None:
        p = ConsumptionProfile()
        p.update(-1, 5.0, is_weekend=False)
        p.update(24, 5.0, is_weekend=False)
        assert p.total_samples == 0

    def test_clamp_consumption(self) -> None:
        p = ConsumptionProfile()
        p.update(0, 100.0, is_weekend=False)
        # Should be clamped to 20.0 kW
        expected = EMA_ALPHA * 20.0 + (1 - EMA_ALPHA) * DEFAULT_CONSUMPTION_PROFILE[0]
        assert abs(p.weekday[0] - expected) < 0.001

    def test_serialization_roundtrip(self) -> None:
        p = ConsumptionProfile()
        for i in range(200):
            p.update(i % 24, 2.0 + (i % 5) * 0.1, is_weekend=i % 7 >= 5)

        d = p.to_dict()
        p2 = ConsumptionProfile.from_dict(d)
        assert p2.weekday == p.weekday
        assert p2.weekend == p.weekend
        assert p2.samples_weekday == p.samples_weekday
        assert p2.samples_weekend == p.samples_weekend

    def test_get_profile_for_date(self) -> None:
        p = ConsumptionProfile()
        # Monday = weekday (weekday() == 0)
        monday = datetime(2026, 4, 13)  # Monday
        saturday = datetime(2026, 4, 18)  # Saturday
        assert p.get_profile_for_date(monday) == p.get_profile(False)
        assert p.get_profile_for_date(saturday) == p.get_profile(True)


class TestCalculateHouseConsumption:
    """House consumption calculation from energy flows."""

    def test_grid_import_only(self) -> None:
        result = calculate_house_consumption(
            grid_power_w=3000, battery_power_1_w=0,
            battery_power_2_w=0, pv_power_w=0, ev_power_w=0,
        )
        assert abs(result - 3.0) < 0.001

    def test_battery_discharge_adds(self) -> None:
        result = calculate_house_consumption(
            grid_power_w=0, battery_power_1_w=-2000,
            battery_power_2_w=-1000, pv_power_w=0, ev_power_w=0,
        )
        assert abs(result - 3.0) < 0.001

    def test_ev_subtracted(self) -> None:
        result = calculate_house_consumption(
            grid_power_w=5000, battery_power_1_w=0,
            battery_power_2_w=0, pv_power_w=0, ev_power_w=3000,
        )
        assert abs(result - 2.0) < 0.001

    def test_never_negative(self) -> None:
        result = calculate_house_consumption(
            grid_power_w=0, battery_power_1_w=0,
            battery_power_2_w=0, pv_power_w=0, ev_power_w=5000,
        )
        assert result == 0.0

    def test_export_not_counted(self) -> None:
        result = calculate_house_consumption(
            grid_power_w=-2000, battery_power_1_w=0,
            battery_power_2_w=0, pv_power_w=3000, ev_power_w=0,
        )
        assert result == 3.0  # PV producing, export ignored

    def test_clamp_uses_constant(self) -> None:
        """PLAT-1554: Clamp uses CONSUMPTION_MAX_KW constant."""
        from core.consumption import CONSUMPTION_MAX_KW
        p = ConsumptionProfile()
        p.update(0, CONSUMPTION_MAX_KW + 50, is_weekend=False)
        expected = EMA_ALPHA * CONSUMPTION_MAX_KW + (1 - EMA_ALPHA) * DEFAULT_CONSUMPTION_PROFILE[0]
        assert abs(p.weekday[0] - expected) < 0.001


# ---------------------------------------------------------------------------
# PLAT-1578: ConsumptionConfig
# ---------------------------------------------------------------------------

_CUSTOM_EMA_ALPHA: float = 0.2
_CUSTOM_MIN_SAMPLES: int = 48
_CUSTOM_MAX_KW: float = 10.0


class TestConsumptionConfig:
    """PLAT-1578 C3: ConsumptionConfig dataclass defaults and overrides."""

    def test_consumption_config_defaults(self) -> None:
        from core.consumption import (
            CONSUMPTION_MAX_KW,
            EMA_ALPHA,
            MIN_SAMPLES_FOR_LEARNED,
            ConsumptionConfig,
        )
        cfg = ConsumptionConfig()
        assert cfg.ema_alpha == EMA_ALPHA
        assert cfg.min_samples == MIN_SAMPLES_FOR_LEARNED
        assert cfg.max_kw == CONSUMPTION_MAX_KW

    def test_consumption_profile_uses_config(self) -> None:
        from core.consumption import ConsumptionConfig, ConsumptionProfile

        cfg = ConsumptionConfig(
            ema_alpha=_CUSTOM_EMA_ALPHA,
            min_samples=_CUSTOM_MIN_SAMPLES,
            max_kw=_CUSTOM_MAX_KW,
        )
        profile = ConsumptionProfile(config=cfg)
        assert profile._ema_alpha == _CUSTOM_EMA_ALPHA
        assert profile._min_samples == _CUSTOM_MIN_SAMPLES
        assert profile._max_kw == _CUSTOM_MAX_KW
