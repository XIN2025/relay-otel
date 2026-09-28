from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from .types import Document

JOURNAL_SCHEMA_VERSION = 2


class JournalSchemaError(ValueError):
    """The database cannot satisfy the versioned relay journal contract."""


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_ABANDONED = "attempt_abandoned"
    ATTEMPT_FAILED = "attempt_failed"
    EFFECT_INTENT = "effect_intent"
    EFFECT_COMPLETED = "effect_completed"
    EFFECT_RESOLVED = "effect_resolved"
    ACTIVATION_FINISHED = "activation_finished"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"

    # Read compatibility only. Runtime v2 never appends these event types.
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    STEP_FAILED = "step_failed"
    EFFECT = "effect"
    EFFECT_REPLAYED = "effect_replayed"


@dataclass(frozen=True)
class Event:
    seq: int
    run_id: str
    type: EventType
    node: str | None
    payload: Document
    at: float
    activation_id: str | None = None
    attempt_id: str | None = None
    at_ns: int = 0

    @property
    def time_ns(self) -> int:

        return self.at_ns if self.at_ns > 0 else int(self.at * 1_000_000_000)


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT    PRIMARY KEY,
    schema_version  INTEGER NOT NULL,
    created_at_ns   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT    NOT NULL,
    type           TEXT    NOT NULL,
    node           TEXT,
    activation_id  TEXT,
    attempt_id     TEXT,
    payload        TEXT    NOT NULL,
    at             REAL    NOT NULL,
    at_ns          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_run ON events (run_id, seq);
"""

EVENT_COLUMNS = (
    "seq",
    "run_id",
    "type",
    "node",
    "activation_id",
    "attempt_id",
    "payload",
    "at",
    "at_ns",
)


def _document_json(payload: Document | None) -> tuple[Document, str]:
    document = {} if payload is None else payload
    if not isinstance(document, dict):
        raise TypeError("journal payload must be an object")
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return document, encoded


def _event_from_row(row: tuple[Any, ...]) -> Event:
    return Event(
        seq=int(row[0]),
        run_id=str(row[1]),
        type=EventType(str(row[2])),
        node=str(row[3]) if row[3] is not None else None,
        activation_id=str(row[4]) if row[4] is not None else None,
        attempt_id=str(row[5]) if row[5] is not None else None,
        payload=cast(Document, json.loads(str(row[6]))),
        at=float(row[7]),
        at_ns=int(row[8]),
    )


class Journal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)
        self._migrate_v1_columns()

    def _migrate_v1_columns(self) -> None:
        existing = {
            str(row[1]) for row in self._db.execute("PRAGMA table_info(events)")
        }
        additions = {
            "activation_id": "TEXT",
            "attempt_id": "TEXT",
            "at_ns": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in additions.items():
            if column not in existing:
                self._db.execute(
                    f"ALTER TABLE events ADD COLUMN {column} {declaration}"
                )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS events_by_activation "
            "ON events (run_id, activation_id, seq)"
        )

    def _insert(
        self,
        run_id: str,
        type: EventType,
        node: str | None,
        payload: Document | None,
        activation_id: str | None,
        attempt_id: str | None,
        *,
        at_ns: int | None = None,
    ) -> Event:
        recorded_at_ns = time.time_ns() if at_ns is None else at_ns
        if not run_id:
            raise ValueError("run_id must not be empty")
        if (
            isinstance(recorded_at_ns, bool)
            or not isinstance(recorded_at_ns, int)
            or recorded_at_ns < 1
        ):
            raise ValueError("journal timestamps must be positive integer nanoseconds")
        document, encoded = _document_json(payload)
        cur = self._db.execute(
            "INSERT INTO events "
            "(run_id, type, node, activation_id, attempt_id, payload, at, at_ns) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                str(type),
                node,
                activation_id,
                attempt_id,
                encoded,
                recorded_at_ns / 1_000_000_000,
                recorded_at_ns,
            ),
        )
        return Event(
            seq=int(cur.lastrowid or 0),
            run_id=run_id,
            type=type,
            node=node,
            payload=document,
            at=recorded_at_ns / 1_000_000_000,
            activation_id=activation_id,
            attempt_id=attempt_id,
            at_ns=recorded_at_ns,
        )

    def begin_run(
        self,
        run_id: str,
        *,
        payload: Document,
        start_node: str,
        activation_id: str,
        workflow_version: str,
    ) -> Event | None:

        self._db.execute("BEGIN IMMEDIATE")
        try:
            registered = self._db.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            has_events = self._db.execute(
                "SELECT 1 FROM events WHERE run_id = ? LIMIT 1", (run_id,)
            ).fetchone()
            if registered or has_events:
                self._db.execute("ROLLBACK")
                return None
            created_at_ns = time.time_ns()
            self._db.execute(
                "INSERT INTO runs (run_id, schema_version, created_at_ns) VALUES (?,?,?)",
                (run_id, JOURNAL_SCHEMA_VERSION, created_at_ns),
            )
            event = self._insert(
                run_id,
                EventType.RUN_STARTED,
                start_node,
                {
                    "input": payload,
                    "journal_schema_version": JOURNAL_SCHEMA_VERSION,
                    "workflow_version": workflow_version,
                    "start_node": start_node,
                    "activation_id": activation_id,
                },
                activation_id,
                None,
                at_ns=created_at_ns,
            )
            self._db.execute("COMMIT")
            return event
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def append(
        self,
        run_id: str,
        type: EventType,
        node: str | None = None,
        payload: Document | None = None,
        *,
        activation_id: str | None = None,
        attempt_id: str | None = None,
        at_ns: int | None = None,
    ) -> Event:
        row = self._db.execute(
            "SELECT schema_version FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise JournalSchemaError(
                f"cannot append event for unregistered run {run_id!r}"
            )
        if int(row[0]) != JOURNAL_SCHEMA_VERSION:
            raise JournalSchemaError(
                f"run {run_id!r} uses unsupported journal schema {row[0]!r}"
            )
        return self._insert(
            run_id,
            type,
            node,
            payload,
            activation_id,
            attempt_id,
            at_ns=at_ns,
        )

    def events(self, run_id: str, upto_seq: int | None = None) -> list[Event]:
        sql = (
            "SELECT seq, run_id, type, node, activation_id, attempt_id, "
            "payload, at, at_ns FROM events WHERE run_id = ?"
        )
        args: list[object] = [run_id]
        if upto_seq is not None:
            sql += " AND seq <= ?"
            args.append(upto_seq)
        rows = self._db.execute(sql + " ORDER BY seq", args).fetchall()
        return [_event_from_row(row) for row in rows]

    def runs(self) -> Iterator[str]:
        for (run_id,) in self._db.execute(
            "SELECT run_id FROM events GROUP BY run_id ORDER BY MIN(seq) DESC"
        ):
            yield str(run_id)

    def close(self) -> None:
        self._db.close()


class JournalReader:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"journal does not exist: {self.path}")
        uri = self.path.resolve().as_uri() + "?mode=ro"
        self._db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=30)
        self._db.execute("PRAGMA query_only=ON")
        existing = {
            str(row[1]) for row in self._db.execute("PRAGMA table_info(events)")
        }
        missing = set(EVENT_COLUMNS) - existing
        if missing:
            self._db.close()
            raise JournalSchemaError(
                "journal predates runtime schema v2; missing columns: "
                + ", ".join(sorted(missing))
            )

    def events(self, run_id: str, upto_seq: int | None = None) -> list[Event]:
        sql = (
            "SELECT seq, run_id, type, node, activation_id, attempt_id, "
            "payload, at, at_ns FROM events WHERE run_id = ?"
        )
        args: list[object] = [run_id]
        if upto_seq is not None:
            sql += " AND seq <= ?"
            args.append(upto_seq)
        rows = self._db.execute(sql + " ORDER BY seq", args).fetchall()
        return [_event_from_row(row) for row in rows]

    def runs(self) -> Iterator[str]:
        for (run_id,) in self._db.execute(
            "SELECT run_id FROM events GROUP BY run_id ORDER BY MIN(seq) DESC"
        ):
            yield str(run_id)

    def close(self) -> None:
        self._db.close()
