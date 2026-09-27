"""Child process for the real hard-exit and fresh-process resume experiment."""

from __future__ import annotations

import argparse
import json

from opentelemetry import context, trace

from relay.engine import Engine, fold
from relay.journal import Journal

from .demo import RefundStore, refund_graph
from .live import live_provider


def crash(args: argparse.Namespace) -> int:
    provider = None
    tracer = None
    if args.live_mode != "none":
        if not args.live_output:
            raise ValueError("live capture mode requires --live-output")
        provider = live_provider(
            args.live_mode, args.live_output, otlp_endpoint=args.otlp_endpoint
        )
        tracer = provider.get_tracer("relay_otel.live")

    journal = Journal(args.journal)
    graph = refund_graph(args.effects, crash_after_effect=True, tracer=tracer)
    engine = Engine(graph, journal)
    if tracer is None:
        engine.start(args.run_id, {"amount_cents": args.amount_cents})
        raise RuntimeError("registered crash did not terminate the process")

    workflow = tracer.start_span(
        "relay.run",
        attributes={
            "relay.operation.name": "run",
            "relay.run.id": args.run_id,
            "relay.workflow.version": graph.version,
        },
    )
    activation = tracer.start_span(
        "relay.activation issue_refund",
        context=trace.set_span_in_context(workflow),
        attributes={
            "relay.operation.name": "activation",
            "relay.run.id": args.run_id,
            "relay.node.name": "issue_refund",
        },
    )
    token = context.attach(trace.set_span_in_context(activation))
    try:
        engine.start(args.run_id, {"amount_cents": args.amount_cents})
    finally:
        context.detach(token)
        activation.end()
        workflow.end()
        journal.close()
        if provider is not None:
            provider.shutdown()
    raise RuntimeError("registered crash did not terminate the process")


def resume(args: argparse.Namespace) -> int:
    journal = Journal(args.journal)
    try:
        graph = refund_graph(args.effects, crash_after_effect=False)
        state = Engine(graph, journal).resume(args.run_id)
        events = journal.events(args.run_id)
    finally:
        journal.close()
    store = RefundStore(args.effects)
    try:
        external = store.summary()
    finally:
        store.close()
    print(
        json.dumps(
            {
                "status": state.status,
                "state": state.state,
                "event_count": len(events),
                "external": external,
                "fold_status": fold(graph, events).status,
                "failure_reason": state.failure_reason,
            },
            sort_keys=True,
        )
    )
    return 0 if state.status == "finished" else 1


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--journal", required=True)
    command.add_argument("--effects", required=True)
    command.add_argument("--run-id", required=True)
    command.add_argument("--amount-cents", type=int, default=4200)
    command.add_argument("--resume", action="store_true")
    command.add_argument(
        "--live-mode", choices=("none", "batch", "simple"), default="none"
    )
    command.add_argument("--live-output")
    command.add_argument("--otlp-endpoint")
    return command


def main() -> int:
    args = parser().parse_args()
    return resume(args) if args.resume else crash(args)


if __name__ == "__main__":
    raise SystemExit(main())
