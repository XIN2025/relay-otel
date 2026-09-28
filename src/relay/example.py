from __future__ import annotations

import os
import random

from .graph import Graph, NodeContext, State
from .types import Document

graph = Graph(START="classify", version="refund-approval-example/v2")

# RELAY_CRASH_AT=<node> hard-exits inside that node with os._exit.
CRASH_AT = os.environ.get("RELAY_CRASH_AT")


def _maybe_crash(node: str) -> None:
    if CRASH_AT == node:
        print(f"  [process killed inside {node!r}]", flush=True)
        os._exit(9)


@graph.node("classify", description="Ask the model what kind of ticket this is")
def classify(state: State, ctx: NodeContext) -> Document:
    def model_call() -> Document:
        # Non-deterministic on purpose: a matching replay proves the recording.
        return {
            "category": "refund_request",
            "confidence": round(random.uniform(0.80, 0.99), 4),
            "amount_usd": 240,
        }

    verdict = ctx.call(
        "model:classify",
        model_call,
        request={"ticket": state.get("ticket")},
        adapter_version="example-model/v1",
        effect_id="classify-ticket",
    )
    _maybe_crash("classify")
    return {
        "category": verdict["category"],
        "confidence": verdict["confidence"],
        "amount_usd": verdict["amount_usd"],
    }


@graph.node("lookup_order", description="Fetch the order the ticket refers to")
def lookup_order(state: State, ctx: NodeContext) -> Document:
    order = ctx.call(
        "db:lookup_order",
        lambda: {"order_id": "A-88213", "paid": True},
        request={"ticket": state.get("ticket")},
        adapter_version="example-order-store/v1",
        effect_id="lookup-order",
    )
    _maybe_crash("lookup_order")
    return {"order": order}


@graph.node(
    "issue_refund",
    requires_approval=True,
    description="Irreversible. Held until a human approves.",
)
def issue_refund(state: State, ctx: NodeContext) -> Document:
    receipt = ctx.call(
        "payments:refund",
        lambda: {"receipt": f"rf_{random.randint(10**6, 10**7)}", "status": "settled"},
        request={
            "order_id": state.get("order", {}).get("order_id"),
            "amount_usd": state.get("amount_usd"),
        },
        adapter_version="example-payments/v1",
        effect_id="issue-refund",
    )
    _maybe_crash("issue_refund")
    return {"refund": receipt}


@graph.node("notify", description="Tell the customer what happened")
def notify(state: State, ctx: NodeContext) -> Document:
    sent = ctx.call(
        "email:send",
        lambda: {"to": "customer@example.com", "sent": True},
        request={"refund": state.get("refund")},
        adapter_version="example-email/v1",
        effect_id="notify-customer",
    )
    _maybe_crash("notify")
    return {"notified": sent["sent"]}


@graph.node("decline", description="Close the ticket without refunding")
def decline(state: State, ctx: NodeContext) -> Document:
    _maybe_crash("decline")
    return {"outcome": "declined"}


graph.edge("classify", "lookup_order")


def after_lookup(state: State) -> str:
    """Low-confidence or unpaid orders never reach the irreversible step."""
    if state.get("confidence", 0) < 0.85 or not state.get("order", {}).get("paid"):
        return "decline"
    return "issue_refund"


graph.branch("lookup_order", after_lookup)
graph.edge("issue_refund", "notify")
graph.edge("notify", Graph.END)
graph.edge("decline", Graph.END)
