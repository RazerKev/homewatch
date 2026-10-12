"""Operator CLI for managing homes.

Inside Docker Compose:
    docker compose run --rm api python -m app.cli create-home home-001
    docker compose run --rm api python -m app.cli list-homes
    docker compose run --rm api python -m app.cli rotate-key home-001
    docker compose run --rm api python -m app.cli set-offline-after home-001 600
    docker compose run --rm api python -m app.cli deactivate home-001
    docker compose run --rm api python -m app.cli alerts

Labels are short codes (home-001), never a name or address: the telemetry database
must not be able to say who lives where. The database enforces the format.
"""

import argparse
import re
import sys
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .alerts import format_duration
from .db import get_sessionmaker
from .models import Alert, Home
from .security import api_key_hint, generate_api_key, hash_api_key

LABEL_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]")


def _label(value: str) -> str:
    if not LABEL_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "use a short code like home-001 (lowercase letters, digits, dashes; 3-40 chars) "
            "— never a name or address"
        )
    return value


def _print_key(label: str, key: str) -> None:
    print(f"\nAPI key for {label} (shown once - put it in the agent's configuration, never in git):\n")
    print(f"    {key}\n")


def _get_home(session: Session, label: str) -> Home:
    home = session.scalar(select(Home).where(Home.label == label))
    if home is None:
        sys.exit(f"No home labelled {label!r}.")
    return home


def create_home(session: Session, args: argparse.Namespace) -> None:
    if session.scalar(select(Home.id).where(Home.label == args.label)):
        sys.exit(f"A home labelled {args.label!r} already exists.")
    key = generate_api_key()
    home = Home(
        label=args.label,
        api_key_hash=hash_api_key(key),
        api_key_hint=api_key_hint(key),
        offline_after_s=args.offline_after,
    )
    session.add(home)
    session.commit()
    print(f"Created {home.label} (id {home.id}); alerts after {args.offline_after} s without a heartbeat.")
    _print_key(home.label, key)


def rotate_key(session: Session, args: argparse.Namespace) -> None:
    home = _get_home(session, args.label)
    key = generate_api_key()
    home.api_key_hash = hash_api_key(key)
    home.api_key_hint = api_key_hint(key)
    session.commit()
    print(f"Rotated the key for {home.label}. The old key stopped working just now.")
    _print_key(home.label, key)


def set_offline_after(session: Session, args: argparse.Namespace) -> None:
    home = _get_home(session, args.label)
    home.offline_after_s = args.seconds
    session.commit()
    print(f"{home.label}: offline after {args.seconds} s without a heartbeat.")


def deactivate(session: Session, args: argparse.Namespace) -> None:
    home = _get_home(session, args.label)
    home.active = False
    session.commit()
    print(f"{home.label} deactivated: its key is rejected and the watchdog ignores it. Data is kept.")


def _ago(now: datetime, moment: datetime | None) -> str:
    return "never" if moment is None else f"{format_duration(now - moment)} ago"


def list_homes(session: Session, args: argparse.Namespace) -> None:
    now = session.scalar(select(func.now()))
    homes = session.scalars(select(Home).order_by(Home.label)).all()
    if not homes:
        print("No homes yet. Create one with: python -m app.cli create-home home-001")
        return
    print(f"{'LABEL':<14}{'STATUS':<10}{'LAST HEARTBEAT':<18}{'OFFLINE AFTER':<15}{'KEY':<18}ACTIVE")
    for h in homes:
        print(
            f"{h.label:<14}{h.status:<10}{_ago(now, h.last_heartbeat_at):<18}"
            f"{str(h.offline_after_s) + ' s':<15}{h.api_key_hint + '...':<18}{'yes' if h.active else 'no'}"
        )


def list_alerts(session: Session, args: argparse.Namespace) -> None:
    rows = session.execute(
        select(Alert, Home.label).join(Home, Home.id == Alert.home_id).order_by(Alert.id.desc()).limit(args.limit)
    ).all()
    if not rows:
        print("No alerts yet.")
        return
    for alert, label in rows:
        if alert.delivered_at:
            delivery = f"delivered after {(alert.delivered_at - alert.created_at).total_seconds():.1f} s"
        else:
            delivery = f"PENDING ({alert.attempts} attempts, last error: {alert.last_error})"
        print(f"#{alert.id:<5} {alert.created_at:%Y-%m-%d %H:%M:%S%z}  {label:<12} {alert.kind:<10} {delivery}")
        print(f"        {alert.message}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-home", help="register a home and print its API key once")
    p.add_argument("label", type=_label)
    p.add_argument("--offline-after", type=int, default=300, metavar="SECONDS",
                   help="alert after this long without a heartbeat (default 300)")
    p.set_defaults(func=create_home)

    p = sub.add_parser("rotate-key", help="issue a new key; the old one stops working immediately")
    p.add_argument("label", type=_label)
    p.set_defaults(func=rotate_key)

    p = sub.add_parser("set-offline-after", help="change a home's alert threshold")
    p.add_argument("label", type=_label)
    p.add_argument("seconds", type=int)
    p.set_defaults(func=set_offline_after)

    p = sub.add_parser("deactivate", help="stop accepting a home's key and stop watching it")
    p.add_argument("label", type=_label)
    p.set_defaults(func=deactivate)

    p = sub.add_parser("list-homes", help="status of every home")
    p.set_defaults(func=list_homes)

    p = sub.add_parser("alerts", help="recent alerts and whether they were delivered")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=list_alerts)

    args = parser.parse_args(argv)
    if hasattr(args, "offline_after") and not 5 <= args.offline_after <= 86400:
        parser.error("--offline-after must be between 5 and 86400 seconds")
    if hasattr(args, "seconds") and not 5 <= args.seconds <= 86400:
        parser.error("seconds must be between 5 and 86400")
    with get_sessionmaker()() as session:
        args.func(session, args)


if __name__ == "__main__":
    main()
