"""Request and response bodies.

Models ignore unknown fields rather than rejecting them ("tolerant reader"): agents in
customers' homes update on their own schedule, so a newer agent sending an extra field
must not get every event rejected by an older server.
"""

import math
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

MAX_EVENTS_PER_BATCH = 500


class HeartbeatIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sent_at: AwareDatetime
    agent_version: str = Field(max_length=32, pattern=r"^[0-9A-Za-z.+_-]+$")
    ha_ok: bool
    outbox_depth: int = Field(ge=0)
    dead_letters: int = Field(default=0, ge=0)


class HeartbeatOut(BaseModel):
    status: Literal["ok"] = "ok"
    server_time: datetime  # lets the agent notice when the Pi's clock is wrong


class SensorEventIn(BaseModel):
    """One Home Assistant state change. The agent sends only these allowlisted fields."""

    model_config = ConfigDict(extra="ignore")

    event_id: UUID
    entity_id: str = Field(max_length=255, pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$")
    state: str = Field(max_length=255)
    device_class: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]+$")
    unit: str | None = Field(default=None, max_length=32)
    # Deliberately not range-checked: a Pi that booted without network time can report
    # 1970, and a leak with a wrong timestamp is still a leak. received_at is authoritative.
    occurred_at: AwareDatetime


class EventBatchIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Items are validated one by one in the endpoint (see main.py), so one malformed
    # event is rejected on its own instead of failing the whole batch.
    events: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


class RejectedEvent(BaseModel):
    index: int
    errors: list[dict[str, Any]]


class EventBatchOut(BaseModel):
    accepted: int
    duplicates: int
    rejected: list[RejectedEvent]


def numeric_value(state: str) -> float | None:
    """'21.5' -> 21.5; 'on', 'unavailable', 'nan', 'inf' -> None."""
    try:
        value = float(state)
    except ValueError:
        return None
    return value if math.isfinite(value) else None
