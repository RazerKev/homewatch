"""Alert wording and delivery channels.

Phase 1 channels: the log (always) and ntfy.sh push (optional, for your phone).
Phase 3 adds an SNS channel with the same `send()` signature.

Alert text names the home by its label ("home-007") and says what happened and when —
nothing about who lives there or what the sensors read.
"""

import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

log = logging.getLogger("homewatch.alerts")


class AlertSink(Protocol):
    def send(self, *, title: str, message: str, urgent: bool) -> None: ...


class LogSink:
    def send(self, *, title: str, message: str, urgent: bool) -> None:
        log.warning("ALERT", extra={"title": title, "alert_message": message, "urgent": urgent})


class NtfySink:
    """POST to an ntfy topic URL, e.g. https://ntfy.sh/<long-random-topic>."""

    def __init__(self, url: str, token: str | None = None, timeout_s: float = 10.0) -> None:
        self._url = url
        self._token = token
        self._timeout_s = timeout_s

    def send(self, *, title: str, message: str, urgent: bool) -> None:
        request = urllib.request.Request(self._url, data=message.encode("utf-8"), method="POST")
        request.add_header("Title", title)  # HTTP headers must be ASCII; titles are.
        request.add_header("Priority", "high" if urgent else "default")
        request.add_header("Tags", "rotating_light" if urgent else "white_check_mark")
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")
        with urllib.request.urlopen(request, timeout=self._timeout_s):  # noqa: S310 (URL is operator config)
            pass  # urlopen raises on 4xx/5xx


def describe_error(exc: BaseException) -> str:
    """A short, secret-free description of a delivery failure (no URLs, no tokens)."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    return type(exc).__name__


def format_duration(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


def format_time(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    hour = local.strftime("%I").lstrip("0")  # portable; %-I doesn't exist on Windows
    return f"{local:%b} {local.day}, {hour}:{local:%M %p %Z}"


def compose_alert(kind: str, *, label: str, now: datetime, problem_since: datetime, tz: ZoneInfo) -> tuple[str, str]:
    """Return (title, message). `problem_since` is the last good signal before the gap."""
    gap = f"{format_time(problem_since, tz)} ({format_duration(now - problem_since)} ago)"
    if kind == "offline":
        return (
            f"{label} OFFLINE",
            f"No heartbeat from {label} since {gap}. Likely power, internet, or hub failure - "
            f"this home is not being monitored.",
        )
    if kind == "degraded":
        return (
            f"{label} DEGRADED",
            f"The hub at {label} is online, but Home Assistant has not responded since {gap}. "
            f"Sensors are not being monitored.",
        )
    if kind == "recovered":
        return (
            f"{label} recovered",
            f"{label} is reporting normally again as of {format_time(now, tz)}. "
            f"Monitoring was interrupted for {format_duration(now - problem_since)}.",
        )
    raise ValueError(f"unknown alert kind: {kind}")
