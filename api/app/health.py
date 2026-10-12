"""Pure "is this home stale?" logic — no database, no clock, easy to unit test.

A heartbeat carries `ha_ok`: whether Home Assistant answered the agent's ping. That
splits "the home went dark" from "the hub is up but not monitoring anything":

    no heartbeat for offline_after          -> OFFLINE   (power, internet, or Pi failure)
    heartbeats arriving, but HA silent      -> DEGRADED  (HA crashed or is stuck)
    heartbeats arriving and HA answering    -> OK

Using the same window for DEGRADED debounces Home Assistant's own restarts (updates
take a minute or two), so routine maintenance doesn't page anyone.
"""

from datetime import datetime, timedelta
from enum import StrEnum


class HomeStatus(StrEnum):
    PENDING = "pending"  # never checked in (fresh install)
    OK = "ok"
    DEGRADED = "degraded"
    OFFLINE = "offline"


def evaluate(
    *,
    now: datetime,
    last_heartbeat_at: datetime | None,
    last_healthy_at: datetime | None,
    offline_after: timedelta,
) -> HomeStatus:
    if last_heartbeat_at is None:
        return HomeStatus.PENDING
    if now - last_heartbeat_at > offline_after:
        return HomeStatus.OFFLINE
    if last_healthy_at is None or now - last_healthy_at > offline_after:
        return HomeStatus.DEGRADED
    return HomeStatus.OK


PROBLEM_STATUSES = (HomeStatus.OFFLINE, HomeStatus.DEGRADED)


def gap_start(
    old: HomeStatus,
    new: HomeStatus,
    *,
    current_gap_start: datetime | None,
    last_heartbeat_at: datetime | None,
    last_healthy_at: datetime | None,
    now: datetime,
) -> datetime | None:
    """When the current monitoring gap began (None when the home is fine).

    Moving between OFFLINE and DEGRADED doesn't restart the gap: sensors were
    unmonitored the whole time.
    """
    if new not in PROBLEM_STATUSES:
        return None
    if old in PROBLEM_STATUSES and current_gap_start is not None:
        return current_gap_start
    if new == HomeStatus.OFFLINE:
        return last_heartbeat_at or now
    return last_healthy_at or last_heartbeat_at or now


def alert_kind(old: HomeStatus, new: HomeStatus) -> str | None:
    """Which alert a status change should raise, if any.

    Alerts fire on transitions, not on every check, so a home that stays offline
    produces one alert rather than one every 30 seconds.
    """
    if old == new:
        return None
    if new in PROBLEM_STATUSES:
        return new.value
    if new == HomeStatus.OK and old in PROBLEM_STATUSES:
        return "recovered"
    return None  # pending -> ok is a first check-in, not news
