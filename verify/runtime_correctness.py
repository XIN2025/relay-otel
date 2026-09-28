"""Bounded real-run verification for relay runtime-v2 correctness invariants.

This is an executable release check, not a unit-test suite. It uses temporary
SQLite databases and one real ``os._exit(9)`` child process, then removes them.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from relay.engine import Engine, Halt, JournalLifecycleError
from relay.graph import Graph, NodeContext, State
from relay.journal import Event, EventType, Journal, JournalReader
from relay.types import Document
from relay_otel.product import product_document
from relay_otel.projector import project_journal
from relay_otel.signoz import trace_query


class VerificationFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationFailure(message)


def read_events(path: Path, run_id: str) -> list[Event]:
    reader = JournalReader(path)
    try:
        return reader.events(run_id)
    finally:
        reader.close()


def event_counts(events: list[Event]) -> Counter[EventType]:
    return Counter(event.type for event in events)


def child_environment() -> dict[str, str]:
    inherited = (
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    )
    environment = {key: os.environ[key] for key in inherited if key in os.environ}
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONUTF8": "1",
        }
    )
    return environment


def verify_crash_resume(workdir: Path) -> dict[str, Any]:
    journal_path = workdir / "crash-journal.sqlite3"
    effects_path = workdir / "crash-effects.sqlite3"
    run_id = "runtime-v2-crash"
    environment = child_environment()
    base_command = [
        sys.executable,
        "-m",
        "relay_otel.crash_runner",
        "--journal",
        str(journal_path),
        "--effects",
        str(effects_path),
        "--run-id",
        run_id,
        "--amount-cents",
        "4200",
    ]
    crashed = subprocess.run(
        base_command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    require(
        crashed.returncode == 9,
        f"hard-crash child returned {crashed.returncode}, expected 9: {crashed.stderr}",
    )
    resumed = subprocess.run(
        [*base_command, "--resume"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    require(
        resumed.returncode == 0,
        f"fresh-process resume failed: {resumed.stdout}\n{resumed.stderr}",
    )
    result = json.loads(resumed.stdout.strip().splitlines()[-1])
    require(result["status"] == "finished", "crash run did not finish")
    require(
        result["external"]["business_executions"] == 1,
        "crash/resume executed the refund business effect more than once",
    )
    require(
        result["external"]["attempts"] == 1,
        "journal resolution unexpectedly called the provider",
    )

    events = read_events(journal_path, run_id)
    counts = event_counts(events)
    expected = {
        EventType.ATTEMPT_STARTED: 2,
        EventType.ATTEMPT_ABANDONED: 1,
        EventType.EFFECT_INTENT: 1,
        EventType.EFFECT_COMPLETED: 1,
        EventType.EFFECT_RESOLVED: 1,
        EventType.ACTIVATION_FINISHED: 1,
        EventType.RUN_FINISHED: 1,
    }
    for event_type, count in expected.items():
        require(
            counts[event_type] == count,
            f"{event_type.value} count {counts[event_type]} != {count}",
        )
    attempts = [event for event in events if event.type is EventType.ATTEMPT_STARTED]
    require(
        len({event.attempt_id for event in attempts}) == 2,
        "retry attempts do not have distinct attempt ids",
    )
    require(
        len({event.activation_id for event in attempts}) == 1,
        "a recovery retry changed the logical activation id",
    )

    before_projection = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    aware = project_journal(journal_path, run_id, replay_aware=True)
    control = project_journal(journal_path, run_id, replay_aware=False)
    after_projection = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    require(
        before_projection == after_projection,
        "read-only projection changed the journal database",
    )
    require(len(aware) == 5 and len(control) == 5, "projected trace is not five spans")
    require(
        sum(span.classification == "executed" for span in aware) == 1,
        "aware trace must contain exactly one external execution",
    )
    require(
        sum(span.classification == "resolved" for span in aware) == 1,
        "aware trace must contain exactly one journal resolution",
    )
    resolution = next(span for span in aware if span.classification == "resolved")
    require(len(resolution.links) == 1, "journal resolution is not linked to execution")
    require(
        resolution.attributes.get("relay.effect.source") == "journal",
        "aware resolution lacks journal source truth",
    )
    require(
        sum(span.classification == "executed" for span in control) == 2,
        "replay-blind control must classify two wrapper executions",
    )
    require(
        all("relay.effect.source" not in span.attributes for span in control),
        "control trace leaked aware journal-source truth",
    )
    for trace in (aware, control):
        span_ids = {span.span_id for span in trace}
        require(
            all(
                span.parent_span_id is None or span.parent_span_id in span_ids
                for span in trace
            ),
            "projected trace contains a disconnected parent",
        )
        require(
            all(
                not key.startswith("gen_ai.")
                for span in trace
                for key in span.attributes
            ),
            "projected trace contains an invented GenAI attribute",
        )
    arm_documents: dict[str, Document] = {}
    query_back: dict[str, Any] = {}
    for mode, trace in (("aware", aware), ("control", control)):
        spans = [span.as_dict() for span in trace]
        trace_id = str(spans[0]["trace_id"])
        arm_documents[mode] = {
            "trace_id": trace_id,
            "resource": {"service.name": f"relay-otel-{mode}"},
            "spans": spans,
        }
        start_millis = (
            min(int(span["start_time_unix_nano"]) for span in spans) // 1_000_000
        )
        end_millis = max(int(span["end_time_unix_nano"]) for span in spans) // 1_000_000
        selected_rows = sorted(
            [
                {
                    "traceId": trace_id,
                    "spanId": str(span["span_id"]),
                    "parentSpanId": span["parent_span_id"],
                    "name": str(span["name"]),
                    "serviceName": f"relay-otel-{mode}",
                    "operationName": str(
                        span["attributes"].get("relay.operation.name", "")
                    ),
                    "effectSource": span["attributes"].get("relay.effect.source"),
                }
                for span in spans
            ],
            key=lambda row: (
                row["spanId"],
                row["name"],
                row["parentSpanId"] or "",
                row["operationName"],
                row["effectSource"] or "",
            ),
        )
        query_back[mode] = {
            "traceId": trace_id,
            "spansSent": len(spans),
            "spansRetrieved": len(spans),
            "duplicateRows": 0,
            "identityVerified": True,
            "attributesVerified": True,
            "serviceNameVerified": True,
            "queryAttempts": 1,
            "request": trace_query(
                trace_id,
                start_millis=max(0, start_millis - 300_000),
                end_millis=end_millis + 300_000,
                limit=max(100, len(spans) * 4),
            ),
            "selectedRows": selected_rows,
            "verified": True,
        }
    product = product_document(
        run_id=run_id,
        seed=4200,
        source="bounded-runtime-verifier",
        events=events,
        aware=arm_documents["aware"],
        control=arm_documents["control"],
        provider_attempts=1,
        business_executions=1,
        hard_exit_code=9,
        provenance={"verification": "ephemeral"},
        query_back=query_back,
        signoz_base_url="http://localhost:3301",
    )
    require(product["schemaVersion"] == 2, "product schema is not v2")
    require(
        product["groundTruth"]
        == {
            "activationAttempts": 2,
            "effectIntents": 1,
            "effectCompletions": 1,
            "journalResolutions": 1,
            "providerAttempts": 1,
            "businessExecutions": 1,
        },
        "product ground truth does not match explicit journal facts",
    )
    require(
        "at" not in product["journal"]["events"][0]
        and isinstance(product["journal"]["events"][0]["atUnixNano"], str),
        "product journal timestamps are not lossless decimal strings",
    )
    require(
        all(
            isinstance(span["duration_nano"], str)
            for mode in ("aware", "control")
            for span in product[mode]["spans"]
        ),
        "product span durations are not lossless decimal strings",
    )
    return {
        "events": len(events),
        "awareSpans": len(aware),
        "controlSpans": len(control),
        "businessExecutions": result["external"]["business_executions"],
    }


def verify_loop_activation_isolation(workdir: Path) -> dict[str, Any]:
    journal_path = workdir / "loop.sqlite3"
    graph = Graph(START="loop", version="loop-verifier/v2")
    external_calls: list[tuple[str, int]] = []

    @graph.node("loop")
    def loop(state: State, ctx: NodeContext) -> Document:
        visit = int(state.get("visit", 0)) + 1

        def record() -> Document:
            external_calls.append((ctx.activation_id, visit))
            return {"visit": visit}

        ctx.call(
            "counter:record",
            record,
            request={"visit": visit},
            adapter_version="counter/v1",
            effect_id="record-visit",
        )
        return {"visit": visit}

    graph.branch("loop", lambda state: Graph.END if state["visit"] >= 2 else "loop")
    journal = Journal(journal_path)
    try:
        state = Engine(graph, journal).start("loop-run", {})
    finally:
        journal.close()
    events = read_events(journal_path, "loop-run")
    completions = [
        event for event in events if event.type is EventType.EFFECT_COMPLETED
    ]
    require(state.status == "finished" and state.state["visit"] == 2, "loop failed")
    require(len(external_calls) == 2, "loop visits shared an external effect")
    require(len(completions) == 2, "loop visits shared a journal completion")
    require(
        len({event.activation_id for event in completions}) == 2,
        "loop visits reused a logical activation id",
    )
    require(
        event_counts(events)[EventType.EFFECT_RESOLVED] == 0,
        "legitimate loop visit was classified as a journal resolution",
    )
    return {
        "activations": len({event.activation_id for event in completions}),
        "businessExecutions": len(external_calls),
    }


def verify_replay_route_is_pure(workdir: Path) -> dict[str, Any]:
    journal_path = workdir / "route.sqlite3"
    chooser_calls: list[str] = []
    graph = Graph(START="route", version="route-verifier/v2")

    @graph.node("route")
    def route(_state: State, _ctx: NodeContext) -> Document:
        return {"target": "end"}

    def choose(_state: State) -> str:
        chooser_calls.append("called")
        return Graph.END

    graph.branch("route", choose)
    journal = Journal(journal_path)
    try:
        engine = Engine(graph, journal)
        engine.start("route-run", {})
        events = journal.events("route-run")
        route_fact = next(
            event for event in events if event.type is EventType.ACTIVATION_FINISHED
        )
        require(len(chooser_calls) == 1, "live route chooser did not run exactly once")
        chooser_calls.clear()
        replayed = engine.replay("route-run", route_fact.seq)
        require(replayed.next_node == Graph.END, "replay ignored recorded route target")
        require(not chooser_calls, "workflow replay invoked the route chooser")
    finally:
        journal.close()
    return {"chooserCallsDuringReplay": len(chooser_calls)}


def verify_skipped_call_is_not_resolution(workdir: Path) -> dict[str, Any]:
    journal_path = workdir / "skipped.sqlite3"
    graph = Graph(START="step", version="skip-verifier/v2")
    mode = {"skip": False}
    external_calls: list[str] = []

    @graph.node("step")
    def step(_state: State, ctx: NodeContext) -> Document:
        if not mode["skip"]:
            ctx.call(
                "service:once",
                lambda: external_calls.append("called") or {"ok": True},
                request={"value": 1},
                adapter_version="service/v1",
                effect_id="once",
            )
            raise Halt()
        return {"done": True}

    graph.edge("step", Graph.END)
    journal = Journal(journal_path)
    try:
        engine = Engine(graph, journal)
        try:
            engine.start("skip-run", {})
        except Halt:
            pass
        else:
            raise VerificationFailure("registered halt did not interrupt first attempt")
        mode["skip"] = True
        state = engine.resume("skip-run")
    finally:
        journal.close()
    events = read_events(journal_path, "skip-run")
    aware = project_journal(journal_path, "skip-run", replay_aware=True)
    require(state.status == "finished", "skipped-call recovery did not finish")
    require(len(external_calls) == 1, "skipped-call scenario changed business count")
    require(
        event_counts(events)[EventType.EFFECT_RESOLVED] == 0,
        "absence of a repeated call produced a false resolution fact",
    )
    require(
        sum(span.classification == "resolved" for span in aware) == 0,
        "projector inferred a resolution from a skipped call",
    )
    first_attempt = next(
        span
        for span in aware
        if span.attributes.get("relay.attempt.outcome") == "abandoned"
    )
    require(
        first_attempt.attributes.get("error.type") == "relay.attempt.abandoned",
        "explicit abandonment was not projected",
    )
    return {"resolutions": 0, "businessExecutions": len(external_calls)}


def verify_identity_mismatch_fails_closed(workdir: Path) -> dict[str, Any]:
    journal_path = workdir / "identity.sqlite3"
    graph = Graph(START="step", version="identity-verifier/v2")
    request = {"value": "a"}
    external_calls: list[str] = []

    @graph.node("step")
    def step(_state: State, ctx: NodeContext) -> Document:
        ctx.call(
            "service:write",
            lambda: external_calls.append(request["value"]) or {"ok": True},
            request=dict(request),
            adapter_version="service/v1",
            effect_id="write",
        )
        raise Halt()

    graph.edge("step", Graph.END)
    journal = Journal(journal_path)
    try:
        engine = Engine(graph, journal)
        try:
            engine.start("identity-run", {})
        except Halt:
            pass
        else:
            raise VerificationFailure("identity scenario did not halt")
        request["value"] = "b"
        state = engine.resume("identity-run")
    finally:
        journal.close()
    events = read_events(journal_path, "identity-run")
    require(state.status == "failed", "identity mismatch did not fail the run")
    require(
        state.failure_reason is not None
        and "EffectIdentityMismatch" in state.failure_reason,
        "identity mismatch failure is not explicit",
    )
    require(external_calls == ["a"], "identity mismatch repeated the provider call")
    require(
        event_counts(events)[EventType.EFFECT_RESOLVED] == 0,
        "identity mismatch was mislabeled as a resolution",
    )

    duplicate_path = workdir / "identity-same-attempt.sqlite3"
    duplicate_graph = Graph(
        START="duplicate", version="identity-same-attempt-verifier/v2"
    )
    duplicate_calls: list[int] = []

    @duplicate_graph.node("duplicate")
    def duplicate(_state: State, ctx: NodeContext) -> Document:
        for value in (1, 2):
            ctx.call(
                "service:write",
                lambda value=value: duplicate_calls.append(value) or {"ok": True},
                request={"value": value},
                adapter_version="service/v1",
                effect_id="stable-write",
            )
        return {"done": True}

    duplicate_graph.edge("duplicate", Graph.END)
    duplicate_journal = Journal(duplicate_path)
    try:
        duplicate_state = Engine(duplicate_graph, duplicate_journal).start(
            "same-attempt-run", {}
        )
    finally:
        duplicate_journal.close()
    require(
        duplicate_state.status == "failed"
        and duplicate_state.failure_reason is not None
        and "EffectIdentityMismatch" in duplicate_state.failure_reason,
        "same-attempt effect id reuse did not fail closed",
    )
    require(
        duplicate_calls == [1],
        "same-attempt effect identity mismatch reached the provider",
    )
    return {
        "providerCalls": len(external_calls),
        "sameAttemptProviderCalls": len(duplicate_calls),
        "status": state.status,
    }


def verify_unknown_outcome_fails_closed(workdir: Path) -> dict[str, Any]:
    journal_path = workdir / "unknown.sqlite3"
    graph = Graph(START="step", version="unknown-verifier/v2")
    provider_calls: list[str] = []

    @graph.node("step")
    def step(_state: State, ctx: NodeContext) -> Document:
        def uncertain() -> Document:
            provider_calls.append("called")
            raise Halt()

        ctx.call(
            "service:uncertain",
            uncertain,
            request={"value": 1},
            adapter_version="service/v1",
            effect_id="uncertain",
        )
        return {"unreachable": True}

    graph.edge("step", Graph.END)
    journal = Journal(journal_path)
    try:
        engine = Engine(graph, journal)
        try:
            engine.start("unknown-run", {})
        except Halt:
            pass
        else:
            raise VerificationFailure("unknown-outcome scenario did not halt")
        state = engine.resume("unknown-run")
    finally:
        journal.close()
    events = read_events(journal_path, "unknown-run")
    counts = event_counts(events)
    require(state.status == "failed", "unknown outcome did not fail closed")
    require(
        state.failure_reason is not None
        and "UnknownEffectOutcome" in state.failure_reason
        and "outcome is unknown" in state.failure_reason,
        "unknown outcome limitation is not explicit in returned state",
    )
    require(
        len(provider_calls) == 1, "unknown external outcome was retried generically"
    )
    require(
        counts[EventType.EFFECT_INTENT] == 1, "unknown outcome wrote another intent"
    )
    require(counts[EventType.EFFECT_COMPLETED] == 0, "unknown outcome was completed")
    return {"providerCalls": len(provider_calls), "status": state.status}


def verify_idempotent_unknown_outcome_recovers(workdir: Path) -> dict[str, Any]:
    journal_path = workdir / "idempotent-unknown.sqlite3"
    graph = Graph(START="step", version="idempotent-unknown-verifier/v2")
    provider_calls: list[str] = []
    business_records: dict[str, Document] = {}

    @graph.node("step")
    def step(_state: State, ctx: NodeContext) -> Document:
        key = f"{ctx.run_id}:{ctx.activation_id}:write"

        def idempotent_provider() -> Document:
            provider_calls.append(key)
            result = business_records.setdefault(key, {"receipt": "stable"})
            if len(provider_calls) == 1:
                raise Halt()
            return result

        result = ctx.call(
            "service:idempotent-write",
            idempotent_provider,
            request={"value": 1},
            adapter_version="idempotent-service/v1",
            effect_id="write",
            idempotency_key=key,
            retry_safe=True,
        )
        return {"receipt": result["receipt"]}

    graph.edge("step", Graph.END)
    journal = Journal(journal_path)
    try:
        engine = Engine(graph, journal)
        try:
            engine.start("idempotent-unknown-run", {})
        except Halt:
            pass
        else:
            raise VerificationFailure("idempotent unknown scenario did not halt")
        state = engine.resume("idempotent-unknown-run")
    finally:
        journal.close()
    events = read_events(journal_path, "idempotent-unknown-run")
    counts = event_counts(events)
    intents = [event for event in events if event.type is EventType.EFFECT_INTENT]
    require(state.status == "finished", "retry-safe unknown outcome did not recover")
    require(len(provider_calls) == 2, "retry-safe adapter was not invoked on recovery")
    require(
        len(business_records) == 1, "idempotency key allowed duplicate business work"
    )
    require(counts[EventType.EFFECT_INTENT] == 2, "safe retry lacks a second intent")
    require(counts[EventType.EFFECT_COMPLETED] == 1, "safe retry lacks one completion")
    require(
        intents[1].payload.get("retry_of_intent_seq") == intents[0].seq,
        "safe retry intent does not reference the unknown source intent",
    )
    return {
        "providerCalls": len(provider_calls),
        "businessExecutions": len(business_records),
        "status": state.status,
    }


def verify_approval_and_start_guards(workdir: Path) -> dict[str, Any]:
    journal_path = workdir / "approval.sqlite3"
    graph = Graph(START="approved", version="approval-verifier/v2")
    executions: list[str] = []

    @graph.node("approved", requires_approval=True)
    def approved(_state: State, _ctx: NodeContext) -> Document:
        executions.append("executed")
        return {"approved": True}

    graph.edge("approved", Graph.END)
    journal = Journal(journal_path)
    try:
        engine = Engine(graph, journal)
        waiting = engine.start("approval-run", {"request": 1})
        require(waiting.status == "awaiting_approval", "approval gate did not pause")
        finished = engine.approve("approval-run", granted=True)
        require(finished.status == "finished", "granted approval did not resume")
        require(executions == ["executed"], "approved node execution count is wrong")
        same = engine.start("approval-run", {"request": 1})
        require(same.status == "finished", "same start request was not idempotent")
        try:
            engine.start("approval-run", {"request": 2})
        except JournalLifecycleError:
            pass
        else:
            raise VerificationFailure("run id reuse with different input did not fail")
    finally:
        journal.close()
    return {"executions": len(executions), "status": finished.status}


def verify_legacy_migration_order(workdir: Path) -> dict[str, Any]:
    legacy_path = workdir / "legacy.sqlite3"
    database = sqlite3.connect(legacy_path)
    try:
        database.executescript(
            """
            CREATE TABLE events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                type TEXT NOT NULL,
                node TEXT,
                payload TEXT NOT NULL,
                at REAL NOT NULL
            );
            CREATE INDEX events_by_run ON events (run_id, seq);
            """
        )
        database.commit()
    finally:
        database.close()
    writer = Journal(legacy_path)
    writer.close()
    database = sqlite3.connect(legacy_path)
    try:
        columns = {str(row[1]) for row in database.execute("PRAGMA table_info(events)")}
    finally:
        database.close()
    require(
        {"activation_id", "attempt_id", "at_ns"}.issubset(columns),
        "legacy schema migration did not add v2 columns before indexing",
    )
    missing_path = workdir / "missing.sqlite3"
    try:
        JournalReader(missing_path)
    except FileNotFoundError:
        pass
    else:
        raise VerificationFailure("read-only reader created a missing journal")
    require(not missing_path.exists(), "read-only reader mutated a missing path")
    return {"migratedColumns": sorted(columns)}


def main() -> int:
    checks: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="relay-runtime-v2-") as directory:
            workdir = Path(directory)
            checks["crashResume"] = verify_crash_resume(workdir)
            checks["loopActivationIsolation"] = verify_loop_activation_isolation(
                workdir
            )
            checks["routeReplay"] = verify_replay_route_is_pure(workdir)
            checks["skippedCall"] = verify_skipped_call_is_not_resolution(workdir)
            checks["identityMismatch"] = verify_identity_mismatch_fails_closed(workdir)
            checks["unknownOutcome"] = verify_unknown_outcome_fails_closed(workdir)
            checks["idempotentUnknownOutcome"] = (
                verify_idempotent_unknown_outcome_recovers(workdir)
            )
            checks["approvalAndStartGuards"] = verify_approval_and_start_guards(workdir)
            checks["legacyMigration"] = verify_legacy_migration_order(workdir)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "verdict": "FAIL",
                    "error": f"{type(exc).__name__}: {exc}",
                    "checks": checks,
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps({"verdict": "PASS", "checks": checks}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
