"""Turn Home Assistant state objects into cloud events. Pure functions — no I/O.

Privacy: an event carries only entity ID, state, device class, unit, and timestamp.
Friendly names and every other attribute are dropped. Only entities from the home's own
sensor network (the configured integrations, ZHA by default) with an allowlisted device
class ever qualify.

Idempotency: an event's ID is derived from its content (entity, state, and HA's
last_changed timestamp). The same state change seen twice — live, then again in the
snapshot taken after a reconnect — gets the same ID, so the local outbox and the cloud
can both drop the duplicate. Attribute-only updates keep last_changed, so they map to
the same ID too and aren't re-sent.
"""

import uuid
from typing import Any

# Any fixed UUID works; it just has to never change.
EVENT_NAMESPACE = uuid.UUID("7d6f2f3c-58c1-4f7e-9a53-1e4c2b8d9a60")
FORWARDED_DOMAINS = frozenset({"binary_sensor", "sensor"})


def event_id_for(entity_id: str, state: str, last_changed: str) -> str:
    return str(uuid.uuid5(EVENT_NAMESPACE, f"{entity_id}|{state}|{last_changed}"))


def is_candidate(entity_id: str) -> bool:
    """Cheap pre-filter before an event is queued for processing."""
    return entity_id.partition(".")[0] in FORWARDED_DOMAINS


def build_event(
    state_obj: dict[str, Any],
    *,
    platform: str | None,
    platforms: frozenset[str],
    device_classes: frozenset[str],
) -> dict[str, Any] | None:
    """Return the cloud event for this state, or None if it must not leave the house."""
    entity_id = str(state_obj.get("entity_id", ""))
    if not is_candidate(entity_id) or platform not in platforms:
        return None

    attributes = state_obj.get("attributes") or {}
    device_class = attributes.get("device_class")
    if device_class not in device_classes:
        return None

    last_changed = state_obj.get("last_changed")
    if not last_changed:
        return None

    state = str(state_obj.get("state", ""))[:255]
    unit = attributes.get("unit_of_measurement")
    return {
        "event_id": event_id_for(entity_id, state, last_changed),
        "entity_id": entity_id,
        "state": state,
        "device_class": device_class,
        "unit": str(unit)[:32] if unit is not None else None,
        "occurred_at": last_changed,
    }
