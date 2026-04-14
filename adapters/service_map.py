"""HA Service Domain Mapping — zero hardcoded domain strings.

PLAT-1462: Each adapter defines its service map. No raw "switch",
"select", "number" etc in adapter code — all from ServiceMap.

This enables swapping GoodWe for Huawei/Sigenergy or Easee for
Zaptec without changing adapter logic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceCall:
    """A single HA service call definition."""

    domain: str
    service: str


@dataclass(frozen=True)
class InverterServiceMap:
    """Service map for inverter adapters (GoodWe, Huawei, etc)."""

    set_mode: ServiceCall = ServiceCall("select", "select_option")
    set_power_limit: ServiceCall = ServiceCall("number", "set_value")
    set_export_limit: ServiceCall = ServiceCall("number", "set_value")
    set_fast_charging_on: ServiceCall = ServiceCall("switch", "turn_on")
    set_fast_charging_off: ServiceCall = ServiceCall("switch", "turn_off")


@dataclass(frozen=True)
class EVChargerServiceMap:
    """Service map for EV charger adapters (Easee, Zaptec, etc)."""

    enable: ServiceCall = ServiceCall("switch", "turn_on")
    disable: ServiceCall = ServiceCall("switch", "turn_off")
    set_current: ServiceCall = ServiceCall("easee", "set_charger_dynamic_limit")
    press_override: ServiceCall = ServiceCall("button", "press")


@dataclass(frozen=True)
class ConsumerServiceMap:
    """Service map for consumer load control."""

    turn_on: ServiceCall = ServiceCall("homeassistant", "turn_on")
    turn_off: ServiceCall = ServiceCall("homeassistant", "turn_off")


@dataclass(frozen=True)
class ClimateServiceMap:
    """Service map for climate control."""

    set_temperature: ServiceCall = ServiceCall("climate", "set_temperature")
    set_mode: ServiceCall = ServiceCall("climate", "set_hvac_mode")


@dataclass(frozen=True)
class HelperServiceMap:
    """Service map for HA helper entities."""

    set_input_text: ServiceCall = ServiceCall("input_text", "set_value")
    set_input_number: ServiceCall = ServiceCall("input_number", "set_value")
    turn_on_boolean: ServiceCall = ServiceCall("input_boolean", "turn_on")
    turn_off_boolean: ServiceCall = ServiceCall("input_boolean", "turn_off")
    set_number: ServiceCall = ServiceCall("number", "set_value")
