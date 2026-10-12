"""Heartbeat watchdog — runs OFF the Pi.

A home that loses power or internet can't report its own failure, so something outside
the home has to notice the silence. Locally this runs as its own container; in Phase 3
`run_check()` becomes the body of a scheduled (EventBridge) Lambda.

Safe to run more than once at a time — two containers, or overlapping Lambda invocations
(EventBridge delivers at least once):
  * Status changes are compare-and-set: the UPDATE only applies if the home still has the
    status *and the exact heartbeat timestamps* the decision was based on. If a heartbeat
    lands mid-check, the update matches nothing and the home is re-evaluated next run —
    no false "offline" alert.
  * The alert row is inserted in the same transaction as the status change (transactional
    outbox), so an alert can't be lost between "decided" and "sent".
  * Delivery locks one alert row at a time with SELECT ... FOR UPDATE SKIP LOCKED.
    Delivery is at-least-once: a crash after sending but before recording it can repeat
    an alert; nothing can drop one.

Run locally:  python -m app.watchdog
"""

import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .alerts import AlertSink, LogSink, NtfySink, compose_alert, describe_error
from .config import Settings, get_settings
from .db import get_sessionmaker
from .health import HomeStatus, alert_kind, evaluate, gap_start
from .logging_setup import configure_logging
from .models import Alert, Home

log = logging.getLogger("homewatch.watchdog")

URGENT_KINDS = {"offline", "degraded"}


@dataclass(frozen=True)
class CheckResult:
    transitions: int
    alerts_delivered: int
    alerts_failed: int


def run_check(session: Session, sinks: list[AlertSink], tz: ZoneInfo) -> CheckResult:
    """One full pass: detect status changes, then deliver any pending alerts."""
    transitions = detect_transitions(session, tz)
    delivered, failed = deliver_pending_alerts(session, sinks)
    return CheckResult(transitions, delivered, failed)


def detect_transitions(session: Session, tz: ZoneInfo) -> int:
    now = session.scalar(select(func.now()))
    homes = session.execute(
        select(
            Home.id,
            Home.label,
            Home.status,
            Home.status_changed_at,
            Home.problem_since,
            Home.last_heartbeat_at,
            Home.last_healthy_at,
            Home.offline_after_s,
        ).where(Home.active.is_(True))
    ).all()

    changed = 0
    for home in homes:
        old = HomeStatus(home.status)
        new = evaluate(
            now=now,
            last_heartbeat_at=home.last_heartbeat_at,
            last_healthy_at=home.last_healthy_at,
            offline_after=timedelta(seconds=home.offline_after_s),
        )
        if new == old:
            continue
        problem_since = gap_start(
            old,
            new,
            current_gap_start=home.problem_since,
            last_heartbeat_at=home.last_heartbeat_at,
            last_healthy_at=home.last_healthy_at,
            now=now,
        )

        result = session.execute(
            update(Home)
            .where(
                Home.id == home.id,
                Home.status == old.value,
                Home.last_heartbeat_at.is_not_distinct_from(home.last_heartbeat_at),
                Home.last_healthy_at.is_not_distinct_from(home.last_healthy_at),
            )
            .values(status=new.value, status_changed_at=now, problem_since=problem_since)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            continue  # another runner won, or a heartbeat just arrived; re-check next run

        kind = alert_kind(old, new)
        if kind:
            gap_began = problem_since if kind != "recovered" else (home.problem_since or home.status_changed_at)
            title, message = compose_alert(kind, label=home.label, now=now, problem_since=gap_began, tz=tz)
            session.add(Alert(home_id=home.id, kind=kind, title=title, message=message, created_at=now))
        log.info(
            "home status changed",
            extra={"home_id": str(home.id), "from": old.value, "to": new.value, "alert": kind},
        )
        changed += 1

    session.commit()
    return changed


def deliver_pending_alerts(session: Session, sinks: list[AlertSink], limit: int = 50) -> tuple[int, int]:
    pending_ids = session.scalars(
        select(Alert.id).where(Alert.delivered_at.is_(None)).order_by(Alert.id).limit(limit)
    ).all()
    session.commit()  # end the read-only transaction before doing network I/O

    delivered = failed = 0
    for alert_id in pending_ids:
        alert = session.scalars(
            select(Alert)
            .where(Alert.id == alert_id, Alert.delivered_at.is_(None))
            .with_for_update(skip_locked=True)
        ).first()
        if alert is None:  # delivered meanwhile, or another runner holds it
            session.rollback()
            continue

        alert.attempts += 1
        try:
            for sink in sinks:
                sink.send(title=alert.title, message=alert.message, urgent=alert.kind in URGENT_KINDS)
        except Exception as exc:  # noqa: BLE001 — any channel failure means "retry next run"
            alert.last_error = describe_error(exc)
            session.commit()
            failed += 1
            log.warning(
                "alert delivery failed; will retry",
                extra={"alert_id": alert.id, "attempts": alert.attempts, "error": alert.last_error},
            )
            continue

        alert.delivered_at = session.scalar(select(func.clock_timestamp()))
        alert.last_error = None
        session.commit()
        delivered += 1
        log.info("alert delivered", extra={"alert_id": alert.id, "kind": alert.kind, "home_id": str(alert.home_id)})
    return delivered, failed


def build_sinks(settings: Settings) -> list[AlertSink]:
    sinks: list[AlertSink] = [LogSink()]
    if settings.ntfy_url is not None:
        token = settings.ntfy_token.get_secret_value() if settings.ntfy_token else None
        sinks.append(NtfySink(settings.ntfy_url.get_secret_value(), token))
    return sinks


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    sinks = build_sinks(settings)
    tz = ZoneInfo(settings.alert_timezone)
    make_session = get_sessionmaker()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    log.info(
        "watchdog started",
        extra={"interval_s": settings.watchdog_interval_s, "channels": [type(s).__name__ for s in sinks]},
    )
    while not stop.is_set():
        started = time.monotonic()
        try:
            with make_session() as session:
                result = run_check(session, sinks, tz)
            if result.transitions or result.alerts_delivered or result.alerts_failed:
                log.info("check complete", extra=vars(result))
        except Exception:  # noqa: BLE001 — database down etc.; try again next tick
            log.exception("watchdog check failed")
        stop.wait(max(0.0, settings.watchdog_interval_s - (time.monotonic() - started)))
    log.info("watchdog stopped")


if __name__ == "__main__":
    main()
