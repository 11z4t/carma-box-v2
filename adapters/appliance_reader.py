"""Appliance reader adapter for CARMA Box.

Reads Shelly power sensor entities from Home Assistant and returns
point-in-time ApplianceState snapshots.

All thresholds come from ApplianceMonitorConfig — zero hardcoding.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config.schema import ApplianceMonitorConfig
from core.models import ApplianceState

if TYPE_CHECKING:
    from adapters.ha_api import HAApiClient

logger = logging.getLogger(__name__)


async def read_appliances(
    ha: "HAApiClient",
    monitor_cfg: ApplianceMonitorConfig,
    prev_states: dict[str, bool] | None = None,
) -> list[ApplianceState]:
    """Read all configured appliance power sensors from HA.

    Uses hysteresis: start_threshold_w to activate, stop_threshold_w to
    deactivate. Falls back to active=False, power_w=0.0 on any error.

    Args:
        ha: Home Assistant API client.
        monitor_cfg: Appliance monitor configuration from site.yaml.
        prev_states: Map of entity_id → was_active from previous cycle,
                     used for hysteresis (optional).

    Returns:
        List of ApplianceState, one per configured appliance.
    """
    if not monitor_cfg.enabled:
        return []

    prev = prev_states or {}
    results: list[ApplianceState] = []

    for app_cfg in monitor_cfg.appliances:
        try:
            raw = await ha.get_state(app_cfg.entity_id)
            if raw is None or raw in ("unavailable", "unknown", ""):
                logger.debug(
                    "Appliance %s: entity %s unavailable, treating as off",
                    app_cfg.name, app_cfg.entity_id,
                )
                results.append(ApplianceState(
                    entity_id=app_cfg.entity_id,
                    name=app_cfg.name,
                    active=False,
                    power_w=0.0,
                ))
                continue

            power_w = float(raw)
            was_active = prev.get(app_cfg.entity_id, False)

            # Hysteresis: use start_threshold to activate, stop_threshold to deactivate
            if was_active:
                active = power_w >= app_cfg.stop_threshold_w
            else:
                active = power_w >= app_cfg.start_threshold_w

            results.append(ApplianceState(
                entity_id=app_cfg.entity_id,
                name=app_cfg.name,
                active=active,
                power_w=power_w,
            ))

        except (ValueError, TypeError) as exc:
            logger.warning(
                "Appliance %s: failed to parse state for %s: %s",
                app_cfg.name, app_cfg.entity_id, exc,
            )
            results.append(ApplianceState(
                entity_id=app_cfg.entity_id,
                name=app_cfg.name,
                active=False,
                power_w=0.0,
            ))

    return results
