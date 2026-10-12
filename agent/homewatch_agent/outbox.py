"""Durable on-disk queue (SQLite) between Home Assistant and the cloud.

Every forwarded event is committed here before any upload is attempted, so an internet
outage, a cloud outage, or an agent restart doesn't lose events: they drain when the
connection comes back. Events the cloud rejects as invalid move to `dead_letter` instead
of blocking everything behind them ("poison message" isolation); the heartbeat reports
how many there are so a bug can't hide.
"""

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL UNIQUE,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS dead_letter (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    reason     TEXT NOT NULL,
    dead_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


class Outbox:
    def __init__(self, path: str, max_rows: int = 100_000) -> None:
        self._db = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit transactions below
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")  # a committed event survives power loss
        self._db.executescript(_SCHEMA)
        self._max_rows = max_rows

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def add(self, event: dict[str, Any]) -> bool:
        """Queue an event. Returns False if an event with the same ID is already queued."""
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO outbox (event_id, payload) VALUES (?, ?)",
            (event["event_id"], json.dumps(event, separators=(",", ":"))),
        )
        return cursor.rowcount == 1

    def peek(self, limit: int) -> list[tuple[int, dict[str, Any]]]:
        """Oldest `limit` events, without removing them."""
        rows = self._db.execute("SELECT id, payload FROM outbox ORDER BY id LIMIT ?", (limit,))
        return [(row_id, json.loads(payload)) for row_id, payload in rows]

    def ack(self, row_ids: Iterable[int]) -> None:
        """The cloud has these (stored or already had them): drop them."""
        with self._transaction():
            self._db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in row_ids])

    def dead_letter(self, row_id: int, reason: str) -> None:
        with self._transaction():
            self._db.execute(
                "INSERT INTO dead_letter (event_id, payload, reason) "
                "SELECT event_id, payload, ? FROM outbox WHERE id = ?",
                (reason[:500], row_id),
            )
            self._db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))

    def trim(self) -> int:
        """Enforce the size cap by dropping the oldest events. Returns how many were dropped."""
        excess = self.depth() - self._max_rows
        if excess <= 0:
            return 0
        self._db.execute(
            "DELETE FROM outbox WHERE id IN (SELECT id FROM outbox ORDER BY id LIMIT ?)", (excess,)
        )
        return excess

    def depth(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def dead_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0]

    def close(self) -> None:
        self._db.close()
