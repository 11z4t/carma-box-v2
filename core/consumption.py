"""Consumption Profile Learning for CARMA Box.

Learns household consumption patterns from historical data.
Separate profiles for weekdays and weekends.
Uses EMA (Exponential Moving Average) for smooth adaptation.

Ported from v6 optimizer/consumption.py — pure Python, no HA imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Watts-to-kilowatts conversion factor.
_W_TO_KW: float = 1000.0

# Default 24h consumption profile (kW per hour)
# 00-05: 0.8 kW (night baseload)
# 06-08: 2.0 kW (morning activity)
# 09-16: 1.5 kW (daytime)
# 17-21: 2.5 kW (evening peak)
# 22-23: 1.0 kW (late evening)
DEFAULT_CONSUMPTION_PROFILE: list[float] = (
    [0.8] * 6 + [2.0] * 3 + [1.5] * 8 + [2.5] * 5 + [1.0] * 2
)

# EMA alpha: 10% new data, 90% history
EMA_ALPHA = 0.1
MIN_SAMPLES_FOR_LEARNED = 168  # 7 days x 24 hours
CONSUMPTION_MAX_KW: float = 20.0  # Clamp unreasonable readings


@dataclass(frozen=True)
class ConsumptionConfig:
    """Configuration for the consumption profile learner."""

    ema_alpha: float = EMA_ALPHA
    min_samples: int = MIN_SAMPLES_FOR_LEARNED
    max_kw: float = CONSUMPTION_MAX_KW


class ConsumptionProfile:
    """Learned consumption profile with weekday/weekend split.

    Stores 24 hourly values (kW) per day-type.
    Uses EMA to update smoothly as new data arrives.
    """

    def __init__(
        self,
        ema_alpha: float = EMA_ALPHA,
        min_samples: int = MIN_SAMPLES_FOR_LEARNED,
        config: ConsumptionConfig = ConsumptionConfig(),
    ) -> None:
        self.weekday: list[float] = list(DEFAULT_CONSUMPTION_PROFILE)
        self.weekend: list[float] = list(DEFAULT_CONSUMPTION_PROFILE)
        self.samples_weekday: int = 0
        self.samples_weekend: int = 0
        # config takes precedence; legacy params kept for backward compat
        self._ema_alpha = (
            config.ema_alpha if config.ema_alpha != EMA_ALPHA else ema_alpha
        )
        self._min_samples = (
            config.min_samples if config.min_samples != MIN_SAMPLES_FOR_LEARNED else min_samples
        )
        self._max_kw = config.max_kw

    def update(
        self, hour: int, consumption_kw: float, is_weekend: bool,
    ) -> None:
        """Update profile with a new measurement."""
        if hour < 0 or hour > 23:
            return
        consumption_kw = max(0.0, min(self._max_kw, consumption_kw))

        if is_weekend:
            self.weekend[hour] = (
                self._ema_alpha * consumption_kw
                + (1 - self._ema_alpha) * self.weekend[hour]
            )
            self.samples_weekend += 1
        else:
            self.weekday[hour] = (
                self._ema_alpha * consumption_kw
                + (1 - self._ema_alpha) * self.weekday[hour]
            )
            self.samples_weekday += 1

    def get_profile(self, is_weekend: bool) -> list[float]:
        """Get 24h profile for the given day type.

        Falls back to static default until MIN_SAMPLES_FOR_LEARNED samples.
        """
        if self.total_samples < self._min_samples:
            return list(DEFAULT_CONSUMPTION_PROFILE)
        if is_weekend:
            return [round(v, 2) for v in self.weekend]
        return [round(v, 2) for v in self.weekday]

    def get_profile_for_date(self, dt: datetime) -> list[float]:
        """Get profile based on date's weekday/weekend."""
        return self.get_profile(dt.weekday() >= 5)

    @property
    def is_learned(self) -> bool:
        """True if enough data for learned profile."""
        return self.total_samples >= MIN_SAMPLES_FOR_LEARNED

    @property
    def total_samples(self) -> int:
        return self.samples_weekday + self.samples_weekend

    def to_dict(self) -> dict[str, object]:
        """Serialize for storage."""
        return {
            "weekday": self.weekday,
            "weekend": self.weekend,
            "samples_weekday": self.samples_weekday,
            "samples_weekend": self.samples_weekend,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ConsumptionProfile:
        """Restore from stored dict."""
        profile = cls()
        weekday = data.get("weekday")
        weekend = data.get("weekend")
        if isinstance(weekday, list) and len(weekday) == 24:
            profile.weekday = [float(v) for v in weekday]
        if isinstance(weekend, list) and len(weekend) == 24:
            profile.weekend = [float(v) for v in weekend]
        sw = data.get("samples_weekday", 0)
        se = data.get("samples_weekend", 0)
        profile.samples_weekday = (
            int(sw) if isinstance(sw, (int, float, str)) else 0
        )
        profile.samples_weekend = (
            int(se) if isinstance(se, (int, float, str)) else 0
        )
        return profile


def calculate_house_consumption(
    grid_power_w: float,
    battery_power_1_w: float,
    battery_power_2_w: float,
    pv_power_w: float,
    ev_power_w: float,
) -> float:
    """Calculate actual house consumption from energy flows.

    House = grid_import + battery_discharge + pv_production - ev_charging
    Returns kW.
    """
    grid_import = max(0.0, grid_power_w)
    bat_discharge = abs(min(0.0, battery_power_1_w)) + abs(
        min(0.0, battery_power_2_w)
    )
    pv = max(0.0, pv_power_w)
    ev = max(0.0, ev_power_w)

    house_w = grid_import + bat_discharge + pv - ev
    return max(0.0, house_w) / _W_TO_KW
