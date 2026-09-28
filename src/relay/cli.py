"""Drive the runtime-v2 example from the shell.

relay start   <run-id>   begin, or continue after a crash
relay approve <run-id>   release a held step
relay deny    <run-id>
relay show    <run-id>   the journal, as it stands
relay replay  <run-id> --at SEQ   state as of that event
relay export  <run-id> --out FILE
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import Engine, fold
from .example import graph
from .journal import Event, Journal, JournalReader
from .redaction import public_event_payload

DB = Path("data/relay.sqlite")

STATUS_MARK = {
    "finished": "done",
    "failed": "failed",
    "awaiting_approval": "HELD",
    "running": "running",
}


def _engine() -> Engine:
    return Engine(graph, Journal(DB))


def _print_status(run_id: str, events: list[Event]) -> None:
    rs = fold(graph, events)
    print(f"\n  run {run_id}  [{STATUS_MARK.get(rs.status, rs.status)}]")
    print(f"  completed: {' -> '.join(rs.completed) or '(none)'}")
    if rs.status == "awaiting_approval":
        node = graph.nodes[rs.next_node]
        print(f"  HELD at {rs.next_node!r}: {node.description}")
        print(f"  release with:  relay approve {run_id}")
    print(f"  state: {json.dumps(rs.state, default=str)[:300]}")


def cmd_start(args: argparse.Namespace) -> int:
    engine = _engine()
    try:
        engine.start(args.run_id, {"ticket": args.ticket})
        _print_status(args.run_id, engine.journal.events(args.run_id))
    finally:
        engine.journal.close()
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    engine = _engine()
    try:
        engine.approve(args.run_id, granted=not args.deny)
        _print_status(args.run_id, engine.journal.events(args.run_id))
    finally:
        engine.journal.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        reader = JournalReader(DB)
    except FileNotFoundError:
        print(f"journal does not exist: {DB}")
        return 1
    try:
        events = reader.events(args.run_id)
        if not events:
            print(f"no such run: {args.run_id}")
            return 1
        print(f"\n  {'seq':>4}  {'event':<22} {'node':<14} detail")
        for event in events:
            detail = json.dumps(public_event_payload(event), default=str)
            print(
                f"  {event.seq:>4}  {event.type.value:<22} "
                f"{(event.node or ''):<14} {detail[:90]}"
            )
        _print_status(args.run_id, events)
        return 0
    finally:
        reader.close()


def cmd_replay(args: argparse.Namespace) -> int:
    try:
        reader = JournalReader(DB)
    except FileNotFoundError:
        print(f"journal does not exist: {DB}")
        return 1
    try:
        events = reader.events(args.run_id, upto_seq=args.at)
        if not events:
            print(f"no such run or event prefix: {args.run_id}")
            return 1
        rs = fold(graph, events)
        print(f"\n  state of {args.run_id} as of event {args.at}  [{rs.status}]")
        print(f"  completed: {' -> '.join(rs.completed) or '(none)'}")
        print(f"  next: {rs.next_node}")
        print(f"  state: {json.dumps(rs.state, indent=2, default=str)}")
    finally:
        reader.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    try:
        reader = JournalReader(DB)
    except FileNotFoundError:
        print(f"journal does not exist: {DB}")
        return 1
    try:
        runs = []
        for run_id in reader.runs():
            events = reader.events(run_id)
            rs = fold(graph, events)
            runs.append(
                {
                    "runId": run_id,
                    "status": rs.status,
                    "completed": rs.completed,
                    "nextNode": rs.next_node,
                    "events": [
                        {
                            "seq": event.seq,
                            "type": event.type.value,
                            "node": event.node,
                            "activationId": event.activation_id,
                            "attemptId": event.attempt_id,
                            "payload": public_event_payload(event),
                            "atUnixNano": str(event.time_ns),
                        }
                        for event in events
                    ],
                }
            )
        payload = {
            "schemaVersion": 2,
            "redaction": "business values omitted",
            "graph": {
                "start": graph.START,
                "version": graph.version,
                "nodes": [
                    {
                        "name": node.name,
                        "description": node.description,
                        "requiresApproval": node.requires_approval,
                    }
                    for node in graph.nodes.values()
                ],
            },
            "runs": runs,
        }
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"{len(runs)} runs -> {out}")
        return 0
    finally:
        reader.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="relay", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("run_id")
    s.add_argument("--ticket", default="I was charged twice, please refund")
    s.set_defaults(fn=cmd_start)

    a = sub.add_parser("approve")
    a.add_argument("run_id")
    a.set_defaults(fn=cmd_approve, deny=False)

    d = sub.add_parser("deny")
    d.add_argument("run_id")
    d.set_defaults(fn=cmd_approve, deny=True)

    sh = sub.add_parser("show")
    sh.add_argument("run_id")
    sh.set_defaults(fn=cmd_show)

    r = sub.add_parser("replay")
    r.add_argument("run_id")
    r.add_argument("--at", type=int, required=True)
    r.set_defaults(fn=cmd_replay)

    e = sub.add_parser("export")
    e.add_argument("--out", default="web/data/runs.json")
    e.set_defaults(fn=cmd_export)

    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
