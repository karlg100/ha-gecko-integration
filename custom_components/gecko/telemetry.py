"""Shared telemetry helpers for Gecko entities."""

from __future__ import annotations

from enum import Enum
from typing import Any

from gecko_iot_client.models.flow_zone import FlowZoneInitiator

FLOW_SPEED_MODE_OPTIONS: tuple[str, ...] = ("off", "low", "medium", "high", "max")


def normalize_initiators(initiators: Any) -> set[str]:
    """Normalize flow initiators into comparable string values."""
    if not initiators:
        return set()

    normalized: set[str] = set()
    for initiator in initiators:
        if isinstance(initiator, Enum):
            normalized.add(str(initiator.name))
            normalized.add(str(initiator.value))
            continue

        initiator_text = str(initiator)
        normalized.add(initiator_text)
        normalized.add(initiator_text.upper())

    return normalized


def is_manual_flow_demand(zone: Any) -> bool:
    """Return True when the active flow zone was manually started by the user."""
    if not getattr(zone, "active", False):
        return False

    initiators = normalize_initiators(getattr(zone, "initiators_", None))
    return bool(
        initiators
        and (
            FlowZoneInitiator.USER_DEMAND.value in initiators
            or FlowZoneInitiator.USER_DEMAND.name in initiators
        )
    )


def derive_flow_speed_mode(zone: Any) -> str | None:
    """Convert Gecko flow speed telemetry into an HA-friendly mode."""
    if not getattr(zone, "active", False):
        return "off"

    speed = getattr(zone, "speed", None)
    if speed is None:
        return None

    if not isinstance(speed, (int, float)):
        return None

    if speed <= 0:
        return "off"

    # Some spas report discrete preset indexes instead of percentages.
    if float(speed).is_integer() and 0 <= speed <= 4:
        return {
            0: "off",
            1: "low",
            2: "medium",
            3: "high",
            4: "max",
        }.get(int(speed))

    if speed < 34:
        return "low"
    if speed < 67:
        return "medium"
    return "high"


def derive_flow_percentage(zone: Any) -> int:
    """Convert Gecko flow telemetry into a stable HA percentage."""
    mode = derive_flow_speed_mode(zone)
    if mode == "off":
        return 0
    if mode == "low":
        return 33
    if mode == "medium":
        return 67
    if mode in {"high", "max"}:
        return 100

    speed = getattr(zone, "speed", None)
    if isinstance(speed, (int, float)):
        return max(0, min(100, int(speed)))
    return 0
