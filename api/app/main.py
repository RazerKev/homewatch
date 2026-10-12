"""HomeWatch cloud API — receives heartbeats and sensor events from each home's agent.

Endpoints
    POST /v1/heartbeat   proof of life from a home (plus whether Home Assistant answered)
    POST /v1/events      a batch of sensor state changes (partial-batch semantics)
    GET  /healthz        liveness + database check, no auth, no data

Every /v1 route requires `Authorization: Bearer <per-home API key>`.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import __version__
from .config import get_settings
from .db import get_db
from .logging_setup import RequestLogMiddleware, configure_logging
from .models import Event, Heartbeat, Home
from .schemas import (
    EventBatchIn,
    EventBatchOut,
    HeartbeatIn,
    HeartbeatOut,
    RejectedEvent,
    SensorEventIn,
    numeric_value,
)
from .security import hash_api_key, looks_like_api_key

log = logging.getLogger("homewatch.api")

router = APIRouter()
bearer = HTTPBearer(auto_error=False, description="Per-home API key (hwk_...)")
DbSession = Annotated[Session, Depends(get_db)]


def current_home(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Home:
    """Resolve the calling home from its API key, or answer 401."""
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="missing or invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or not looks_like_api_key(credentials.credentials):
        raise unauthorized
    home = db.scalar(
        select(Home).where(
            Home.api_key_hash == hash_api_key(credentials.credentials),
            Home.active.is_(True),
        )
    )
    if home is None:
        raise unauthorized
    request.state.home_id = str(home.id)  # picked up by the request log
    return home


CurrentHome = Annotated[Home, Depends(current_home)]


@router.get("/healthz", include_in_schema=False)
def healthz(db: DbSession) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        log.warning("health check: database unavailable", extra={"error": type(exc).__name__})
        return JSONResponse({"ok": False}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return JSONResponse({"ok": True, "version": __version__})


@router.post("/v1/heartbeat", response_model=HeartbeatOut)
def post_heartbeat(body: HeartbeatIn, home: CurrentHome, db: DbSession) -> HeartbeatOut:
    now = db.scalar(select(func.now()))  # database clock, never the Pi's
    db.add(
        Heartbeat(
            home_id=home.id,
            received_at=now,
            sent_at=body.sent_at,
            agent_version=body.agent_version,
            ha_ok=body.ha_ok,
            outbox_depth=body.outbox_depth,
            dead_letters=body.dead_letters,
        )
    )
    # Denormalised onto the home row so the watchdog reads one small table, not an
    # aggregate over every heartbeat ever received.
    home.last_heartbeat_at = now
    if body.ha_ok:
        home.last_healthy_at = now
    db.commit()
    return HeartbeatOut(server_time=now)


@router.post("/v1/events", response_model=EventBatchOut)
def post_events(body: EventBatchIn, home: CurrentHome, db: DbSession) -> EventBatchOut:
    """Store a batch of events.

    Partial-batch semantics (like SQS SendMessageBatch or Kinesis PutRecords): each
    event is validated on its own, valid ones are stored, invalid ones come back with
    their index so the agent can quarantine just those. Re-sending an event that is
    already stored is harmless — (home_id, event_id) is unique and duplicates are
    counted, not inserted.
    """
    rows: list[dict[str, Any]] = []
    rejected: list[RejectedEvent] = []
    for index, raw in enumerate(body.events):
        try:
            event = SensorEventIn.model_validate(raw)
        except ValidationError as exc:
            rejected.append(RejectedEvent(index=index, errors=_safe_errors(exc.errors())))
            continue
        rows.append(
            {
                "home_id": home.id,
                "event_id": event.event_id,
                "entity_id": event.entity_id,
                "state": event.state,
                "value": numeric_value(event.state),
                "device_class": event.device_class,
                "unit": event.unit,
                "occurred_at": event.occurred_at,
            }
        )

    accepted = 0
    if rows:
        stmt = (
            insert(Event)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_events_home_id_event_id")
            .returning(Event.id)
        )
        accepted = len(db.execute(stmt).all())
        db.commit()

    duplicates = len(rows) - accepted
    log.info(
        "events ingested",
        extra={
            "home_id": str(home.id),
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": len(rejected),
        },
    )
    return EventBatchOut(accepted=accepted, duplicates=duplicates, rejected=rejected)


def _safe_errors(errors: list[Any]) -> list[dict[str, Any]]:
    """Validation errors without the offending input values (they may be sensor data)."""
    return [
        {"loc": list(err.get("loc", ())), "type": err.get("type"), "msg": err.get("msg")}
        for err in errors
    ]


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # FastAPI's default 422 echoes the rejected input back; this version doesn't.
    assert isinstance(exc, RequestValidationError)
    return JSONResponse({"detail": _safe_errors(list(exc.errors()))}, status_code=422)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="HomeWatch API",
        version=__version__,
        docs_url="/docs" if settings.expose_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.expose_docs else None,
    )
    app.add_middleware(RequestLogMiddleware)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.include_router(router)
    return app


app = create_app()
