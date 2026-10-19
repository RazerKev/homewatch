from homewatch_agent.config import DEFAULT_DEVICE_CLASSES
from homewatch_agent.events import build_event


def reading(device_class, state="1.0"):
    """A Home Assistant state, shaped like the ones the agent receives."""
    return {
        "entity_id": f"sensor.basement_plug_{device_class}",
        "state": state,
        "attributes": {"device_class": device_class},
        "last_changed": "2026-10-18T19:00:00+00:00",
    }


def forwarded(state_obj):
    """What the agent would send to the cloud for this state, or None if it stays home."""
    return build_event(
        state_obj,
        platform="zha",
        platforms=frozenset({"zha"}),
        device_classes=DEFAULT_DEVICE_CLASSES,
    )


def test_voltage_reading_stays_in_the_house():
    assert forwarded(reading("voltage", "121.4")) is None
