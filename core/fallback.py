"""Explicit Fallback Policy for CARMA Box.

PLAT-1382: All fallback behaviors in one place. No silent defaults.
Each trigger has a defined action and is logged.

Triggers:
  F1: HA disconnected → standby all batteries
  F2: Stale data (>300s) → hold current mode, warn
  F3: Invalid SoC reading → use last known or 50%
  F4: Guard evaluation error → FREEZE
  F5: Scenario evaluation error → stay in current scenario
  F6: Executor error → log, retry next cycle
  F7: Config load failure → refuse to start
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, unique
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_SOC_FALLBACK_PCT: float = 50.0
DEFAULT_MAX_SOC_AGE_S: float = 300.0


@dataclass(frozen=True)
class FallbackConfig:
    """Configuration for fallback policy."""

    default_soc_pct: float = DEFAULT_SOC_FALLBACK_PCT
    max_soc_age_s: float = DEFAULT_MAX_SOC_AGE_S


@unique
class FallbackTrigger(Enum):
    """What caused the fallback."""

    HA_DISCONNECTED = "ha_disconnected"
    STALE_DATA = "stale_data"
    INVALID_SOC = "invalid_soc"
    GUARD_ERROR = "guard_error"
    SCENARIO_ERROR = "scenario_error"
    EXECUTOR_ERROR = "executor_error"
    CONFIG_ERROR = "config_error"


@unique
class FallbackAction(Enum):
    """What to do when a trigger fires."""

    STANDBY_ALL = "standby_all"
    HOLD_CURRENT = "hold_current"
    USE_LAST_KNOWN = "use_last_known"
    USE_DEFAULT = "use_default"
    FREEZE = "freeze"
    RETRY_NEXT = "retry_next"
    REFUSE_START = "refuse_start"


@dataclass(frozen=True)
class FallbackEvent:
    """A recorded fallback activation."""

    trigger: FallbackTrigger
    action: FallbackAction
    detail: str = ""


# Trigger → Action mapping (explicit, never silent)
FALLBACK_POLICY: dict[FallbackTrigger, FallbackAction] = {
    FallbackTrigger.HA_DISCONNECTED: FallbackAction.STANDBY_ALL,
    FallbackTrigger.STALE_DATA: FallbackAction.HOLD_CURRENT,
    FallbackTrigger.INVALID_SOC: FallbackAction.USE_LAST_KNOWN,
    FallbackTrigger.GUARD_ERROR: FallbackAction.FREEZE,
    FallbackTrigger.SCENARIO_ERROR: FallbackAction.HOLD_CURRENT,
    FallbackTrigger.EXECUTOR_ERROR: FallbackAction.RETRY_NEXT,
    FallbackTrigger.CONFIG_ERROR: FallbackAction.REFUSE_START,
}


def resolve_fallback(
    trigger: FallbackTrigger,
    detail: str = "",
) -> FallbackEvent:
    """Resolve a fallback trigger to its defined action.

    Always logs the event — no silent fallbacks.
    """
    action = FALLBACK_POLICY[trigger]
    event = FallbackEvent(trigger=trigger, action=action, detail=detail)
    logger.warning(
        "FALLBACK %s → %s: %s",
        trigger.value, action.value, detail or "(no detail)",
    )
    return event


def resolve_soc_fallback(
    raw_soc: float,
    last_known: float,
    max_age_s: float,
    age_s: float,
    config: FallbackConfig = FallbackConfig(),
) -> tuple[float, Optional[FallbackEvent]]:
    """Resolve SoC value with fallback for invalid readings.

    Args:
        raw_soc: Raw SoC from sensor (-1 = XPENG sleep, NaN = error).
        last_known: Last valid SoC value.
        max_age_s: Max age of last known before it's stale — legacy param.
        age_s: Time since last valid reading.
        config: FallbackConfig — controls default SoC and max age thresholds.

    Returns:
        Tuple of (resolved_soc, fallback_event_or_None).
    """
    effective_max_age = (
        config.max_soc_age_s if config.max_soc_age_s != DEFAULT_MAX_SOC_AGE_S else max_age_s
    )
    if raw_soc >= 0:
        return raw_soc, None

    # Invalid reading — try last known
    if last_known >= 0 and age_s < effective_max_age:
        event = resolve_fallback(
            FallbackTrigger.INVALID_SOC,
            f"raw={raw_soc}, using last_known={last_known} (age {age_s:.0f}s)",
        )
        return last_known, event

    # Last known too old — use safe default from config
    event = resolve_fallback(
        FallbackTrigger.INVALID_SOC,
        f"raw={raw_soc}, last_known stale ({age_s:.0f}s), defaulting to {config.default_soc_pct}%",
    )
    return config.default_soc_pct, event
