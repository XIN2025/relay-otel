from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

from opentelemetry import trace

from relay.graph import Graph, NodeContext
from relay.types import Document, State

EFFECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL,
    at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE TABLE IF NOT EXISTS refunds (
    idempotency_key TEXT PRIMARY KEY,
    receipt TEXT NOT NULL,
    amount_cents INTEGER NOT NULL
);
"""


class RefundStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(EFFECT_SCHEMA)

    def refund(self, idempotency_key: str, amount_cents: int) -> dict[str, Any]:
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        if amount_cents < 1:
            raise ValueError("amount_cents must be positive")
        self.db.execute(
            "INSERT INTO attempts (idempotency_key) VALUES (?)", (idempotency_key,)
        )
        receipt = (
            "rf_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:10]
        )
        self.db.execute(
            "INSERT OR IGNORE INTO refunds (idempotency_key, receipt, amount_cents) VALUES (?,?,?)",
            (idempotency_key, receipt, amount_cents),
        )
        row = self.db.execute(
            "SELECT receipt, amount_cents FROM refunds WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("refund disappeared after insert")
        if int(row[1]) != amount_cents:
            raise ValueError(
                "idempotency key was reused with a different refund amount"
            )
        return {"receipt": row[0], "amount_cents": row[1]}

    def summary(self) -> dict[str, Any]:
        attempts = int(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
        executions = int(self.db.execute("SELECT COUNT(*) FROM refunds").fetchone()[0])
        rows = self.db.execute(
            "SELECT idempotency_key, receipt, amount_cents FROM refunds ORDER BY idempotency_key"
        ).fetchall()
        return {
            "attempts": attempts,
            "business_executions": executions,
            "refunds": [
                {"idempotency_key": row[0], "receipt": row[1], "amount_cents": row[2]}
                for row in rows
            ],
        }

    def close(self) -> None:
        self.db.close()


def refund_graph(
    effect_store: Path | str,
    *,
    crash_after_effect: bool,
    tracer: trace.Tracer | None = None,
) -> Graph:
    graph = Graph(START="issue_refund", version="refund-v2")

    @graph.node("issue_refund", description="Refund one deterministic support ticket")
    def issue_refund(state: State, ctx: NodeContext) -> Document:
        amount_cents = int(state["amount_cents"])
        key = f"{ctx.run_id}:{ctx.activation_id}:refund"

        def call_refund() -> dict[str, Any]:
            store = RefundStore(effect_store)
            try:
                return store.refund(key, amount_cents)
            finally:
                store.close()

        if tracer is None:
            result = ctx.call(
                "payments:refund",
                call_refund,
                request={"amount_cents": amount_cents},
                adapter_version="refund-store/v1",
                effect_id="refund",
                idempotency_key=key,
                retry_safe=True,
            )
        else:
            with tracer.start_as_current_span(
                "relay.effect.execute payments:refund",
                attributes={
                    "relay.operation.name": "effect.execute",
                    "relay.run.id": ctx.run_id,
                    "relay.activation.id": ctx.activation_id,
                    "relay.attempt.id": ctx.attempt_id,
                    "relay.effect.id": "refund",
                    "relay.effect.label": "payments:refund",
                    "relay.effect.adapter.version": "refund-store/v1",
                    "relay.effect.outcome": "executed",
                    "relay.effect.source": "external",
                },
            ):
                result = ctx.call(
                    "payments:refund",
                    call_refund,
                    request={"amount_cents": amount_cents},
                    adapter_version="refund-store/v1",
                    effect_id="refund",
                    idempotency_key=key,
                    retry_safe=True,
                )
        if crash_after_effect:
            os._exit(9)
        return {"refund_receipt": result["receipt"]}

    graph.edge("issue_refund", Graph.END)
    return graph
