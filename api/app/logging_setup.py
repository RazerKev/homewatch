"""Structured JSON logs that carry no personal data.

Rules (enforced here and at every call site):
- never log request bodies, headers, query strings, or client IP addresses
- identify homes by opaque UUID only
- sensor values stay in the database, never in logs
- API keys are never logged (the CLI prints a new key once, to the terminal, not a log)

One JSON object per line is what CloudWatch Logs Insights queries natively in Phase 3.
"""

import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Any

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn's ANSI-coloured duplicate of msg
}

request_log = logging.getLogger("homewatch.request")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn's access log records client IPs and full URLs (query strings included).
    # RequestLogMiddleware below replaces it with a privacy-safe version.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True


class RequestLogMiddleware:
    """One log line per request: method, route template, status, duration, home UUID.

    Uses the matched route template ("/v1/events"), never the raw path, so scanners
    probing random URLs can't write arbitrary strings into the logs.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            state = scope.get("state") or {}
            request_log.info(
                "request",
                extra={
                    "method": scope.get("method"),
                    "route": getattr(route, "path", "<unmatched>"),
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "home_id": state.get("home_id"),
                },
            )
