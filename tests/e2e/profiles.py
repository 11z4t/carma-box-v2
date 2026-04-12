"""Synthetic data profiles for E2E simulation.

Summer: high PV (0-10 kW bell curve), mild temperature.
Winter: low PV (0-2 kW short day), cold temperature.
"""

from __future__ import annotations

import math


def summer_pv_kw(hour: int) -> float:
    """Summer PV profile: bell curve peaking at 12:00, 10kW max."""
    if hour < 5 or hour > 21:
        return 0.0
    # Bell curve centered at 13
    x = (hour - 13) / 4.0
    return 10.0 * math.exp(-x * x)


def winter_pv_kw(hour: int) -> float:
    """Winter PV profile: short day, 2kW max."""
    if hour < 9 or hour > 15:
        return 0.0
    x = (hour - 12) / 2.0
    return 2.0 * math.exp(-x * x)


def house_consumption_kw(hour: int) -> float:
    """House consumption profile: baseload 2kW, peaks morning/evening."""
    base = 2.0
    if 7 <= hour <= 9:
        return base + 1.0  # Morning peak
    if 17 <= hour <= 21:
        return base + 1.5  # Evening peak
    if 0 <= hour <= 5:
        return base - 0.5  # Night low
    return base


def nordpool_price_ore(hour: int) -> float:
    """Nordpool price profile: cheap night, expensive day."""
    if 0 <= hour <= 5:
        return 15.0  # Night cheap
    if 6 <= hour <= 8:
        return 60.0  # Morning peak
    if 9 <= hour <= 16:
        return 40.0  # Daytime
    if 17 <= hour <= 21:
        return 80.0  # Evening peak
    return 25.0  # Late night
