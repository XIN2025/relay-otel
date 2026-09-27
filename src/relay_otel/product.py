"""Strict portable product proof assembled from journal and projected evidence."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any, cast

from relay.journal import Event, EventType
from relay.redaction import public_event_payload
from relay.types import Document

from .queryback import validate_retained_query

REGISTERED_EVENT_COUNTS = {
    EventType.RUN_STARTED: 1,
    EventType.ATTEMPT_STARTED: 2,
    EventType.EFFECT_INTENT: 1,
    EventType.EFFECT_COMPLETED: 1,
    EventType.ATTEMPT_ABANDONED: 1,
    EventType.EFFECT_RESOLVED: 1,
    EventType.ACTIVATION_FINISHED: 1,
    EventType.RUN_FINISHED: 1,
}
EFFECT_IDENTITY_FIELDS = (
    "effect_id",
    "index",
    "label",
    "request_fingerprint",
    "adapter_version",
    "idempotency_key_fingerprint",
    "retry_safe",
)


def _events(events: list[Event]) -> list[dict[str, Any]]:
    return [
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
    ]


def _one_event(events: list[Event], event_type: EventType) -> Event:
    matches = [event for event in events if event.type is event_type]
    if len(matches) != 1:
        raise ValueError(f"registered proof requires one {event_type.value} event")
    return matches[0]


def _validate_registered_events(events: list[Event]) -> None:
    counts = Counter(event.type for event in events)
    if counts != Counter(REGISTERED_EVENT_COUNTS):
        raise ValueError("journal event counts differ from the registered proof")
    if any(
        left.seq >= right.seq for left, right in zip(events, events[1:], strict=False)
    ):
        raise ValueError("journal sequences must increase strictly")
    if any(
        left.time_ns > right.time_ns
        for left, right in zip(events, events[1:], strict=False)
    ):
        raise ValueError("journal timestamps must be non-decreasing")

    attempts = [event for event in events if event.type is EventType.ATTEMPT_STARTED]
    first_attempt, second_attempt = attempts
    if (
        not first_attempt.activation_id
        or first_attempt.activation_id != second_attempt.activation_id
        or not first_attempt.attempt_id
        or not second_attempt.attempt_id
        or first_attempt.attempt_id == second_attempt.attempt_id
    ):
        raise ValueError(
            "registered proof requires two distinct attempts in one activation"
        )
    intent = _one_event(events, EventType.EFFECT_INTENT)
    completion = _one_event(events, EventType.EFFECT_COMPLETED)
    abandonment = _one_event(events, EventType.ATTEMPT_ABANDONED)
    resolution = _one_event(events, EventType.EFFECT_RESOLVED)
    activation_finished = _one_event(events, EventType.ACTIVATION_FINISHED)
    if any(
        event.activation_id != first_attempt.activation_id
        for event in (intent, completion, resolution)
    ):
        raise ValueError("registered effect facts changed logical activation")
    if (
        intent.attempt_id != first_attempt.attempt_id
        or completion.attempt_id != first_attempt.attempt_id
        or abandonment.attempt_id != first_attempt.attempt_id
        or resolution.attempt_id != second_attempt.attempt_id
        or activation_finished.attempt_id != second_attempt.attempt_id
    ):
        raise ValueError("registered facts do not match their expected attempt scopes")
    if (
        completion.payload.get("source_intent_seq") != intent.seq
        or resolution.payload.get("source_intent_seq") != intent.seq
        or resolution.payload.get("source_effect_seq") != completion.seq
    ):
        raise ValueError("effect completion/resolution source chain is invalid")
    for field in EFFECT_IDENTITY_FIELDS:
        expected = intent.payload.get(field)
        if (
            completion.payload.get(field) != expected
            or resolution.payload.get(field) != expected
        ):
            raise ValueError(f"effect identity field changed: {field}")
    if (
        intent.payload.get("effect_id") != "refund"
        or intent.payload.get("index") != 0
        or intent.payload.get("label") != "payments:refund"
        or intent.payload.get("adapter_version") != "refund-store/v1"
        or intent.payload.get("retry_safe") is not True
    ):
        raise ValueError("registered refund effect identity changed")
    for field in ("request_fingerprint", "idempotency_key_fingerprint"):
        value = intent.payload.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"effect field {field!r} is not a lowercase SHA-256")


def _validated_spans(document: Document, mode: str) -> list[Document]:
    spans_value = document.get("spans")
    if not isinstance(spans_value, list) or len(spans_value) != 5:
        raise ValueError(f"{mode} registered proof requires five spans")
    if not all(isinstance(span, dict) for span in spans_value):
        raise ValueError(f"{mode} spans must be objects")
    spans = cast(list[Document], spans_value)
    trace_id = document.get("trace_id")
    if not isinstance(trace_id, str) or len(trace_id) != 32:
        raise ValueError(f"{mode} trace id is invalid")
    ids = [span.get("span_id") for span in spans]
    if any(
        not isinstance(span_id, str) or len(span_id) != 16 for span_id in ids
    ) or len(set(ids)) != len(ids):
        raise ValueError(f"{mode} span ids must be unique 16-character strings")
    by_id = {str(span["span_id"]): span for span in spans}
    roots = [span for span in spans if span.get("parent_span_id") is None]
    if len(roots) != 1:
        raise ValueError(f"{mode} trace must contain one root")
    for span in spans:
        if span.get("trace_id") != trace_id:
            raise ValueError(f"{mode} span changed trace id")
        parent_id = span.get("parent_span_id")
        if parent_id is not None and parent_id not in by_id:
            raise ValueError(f"{mode} span has a missing parent")
        start = span.get("start_time_unix_nano")
        end = span.get("end_time_unix_nano")
        duration = span.get("duration_nano")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or isinstance(duration, bool)
            or not isinstance(duration, int)
            or end < start
            or duration != end - start
        ):
            raise ValueError(f"{mode} span has an invalid recorded interval")
        if parent_id is not None:
            parent = by_id[str(parent_id)]
            if start < int(parent["start_time_unix_nano"]) or end > int(
                parent["end_time_unix_nano"]
            ):
                raise ValueError(f"{mode} child span escapes its parent interval")

    visited: set[str] = set()
    pending = [str(roots[0]["span_id"])]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        pending.extend(
            str(span["span_id"])
            for span in spans
            if span.get("parent_span_id") == current
        )
    if len(visited) != len(spans):
        raise ValueError(f"{mode} trace is not a connected tree")
    expected_service = f"relay-otel-{mode}"
    resource = document.get("resource")
    if (
        not isinstance(resource, dict)
        or resource.get("service.name") != expected_service
    ):
        raise ValueError(f"{mode} resource service name changed")
    root = roots[0]
    root_attributes = root.get("attributes")
    if (
        root.get("identity") != "run"
        or root.get("name") != "relay.run"
        or root.get("classification") != "structural"
        or root.get("kind") != "INTERNAL"
        or root.get("links") != []
        or not isinstance(root_attributes, dict)
        or root_attributes.get("relay.operation.name") != "run"
        or root_attributes.get("relay.workflow.version") != "refund-v2"
    ):
        raise ValueError(f"{mode} registered root span contract changed")
    activations = [
        span
        for span in spans
        if isinstance(span.get("attributes"), dict)
        and span["attributes"].get("relay.operation.name") == "activation"
    ]
    if len(activations) != 2:
        raise ValueError(f"{mode} requires exactly two activation spans")
    activation_contracts: set[tuple[object, object]] = set()
    for activation in activations:
        attributes = cast(dict[str, Any], activation["attributes"])
        if (
            activation.get("name") != "relay.activation issue_refund"
            or activation.get("classification") != "structural"
            or activation.get("parent_span_id") != root.get("span_id")
            or activation.get("kind") != "INTERNAL"
            or activation.get("links") != []
            or attributes.get("relay.node.name") != "issue_refund"
        ):
            raise ValueError(f"{mode} registered activation span contract changed")
        activation_contracts.add(
            (
                attributes.get("relay.attempt.outcome"),
                attributes.get("relay.attempt.recovery"),
            )
        )
    if activation_contracts != {("abandoned", False), ("completed", True)}:
        raise ValueError(f"{mode} activation outcome/recovery topology changed")
    return spans


def _validate_projection(aware: Document, control: Document) -> tuple[int, int, int]:
    aware_spans = _validated_spans(aware, "aware")
    control_spans = _validated_spans(control, "control")
    if aware.get("trace_id") == control.get("trace_id"):
        raise ValueError("aware and control traces must have different identities")
    aware_executed = [
        span for span in aware_spans if span.get("classification") == "executed"
    ]
    aware_resolved = [
        span for span in aware_spans if span.get("classification") == "resolved"
    ]
    control_executed = [
        span for span in control_spans if span.get("classification") == "executed"
    ]
    control_resolved = [
        span for span in control_spans if span.get("classification") == "resolved"
    ]
    if len(aware_executed) != 1 or len(aware_resolved) != 1:
        raise ValueError("aware proof requires one execution and one resolution")
    if len(control_executed) != 2 or control_resolved:
        raise ValueError("control proof requires two executions and no resolution")
    for mode, effect_spans in (
        ("aware", [*aware_executed, *aware_resolved]),
        ("control", control_executed),
    ):
        for span in effect_spans:
            attributes = span.get("attributes")
            if (
                not isinstance(attributes, dict)
                or attributes.get("relay.effect.id") != "refund"
                or attributes.get("relay.effect.index") != 0
                or attributes.get("relay.effect.label") != "payments:refund"
                or attributes.get("relay.effect.adapter.version") != "refund-store/v1"
                or span.get("kind") != "INTERNAL"
            ):
                raise ValueError(f"{mode} registered effect span contract changed")
    executed_attributes = aware_executed[0].get("attributes")
    resolved_attributes = aware_resolved[0].get("attributes")
    if (
        not isinstance(executed_attributes, dict)
        or executed_attributes.get("relay.operation.name") != "effect.execute"
        or executed_attributes.get("relay.effect.source") != "external"
    ):
        raise ValueError("aware execution lacks external source truth")
    if (
        not isinstance(resolved_attributes, dict)
        or resolved_attributes.get("relay.operation.name") != "effect.resolve"
        or resolved_attributes.get("relay.effect.source") != "journal"
    ):
        raise ValueError("aware resolution lacks journal source truth")
    links = aware_resolved[0].get("links")
    if links != [
        {
            "trace_id": aware.get("trace_id"),
            "span_id": aware_executed[0].get("span_id"),
        }
    ]:
        raise ValueError("aware resolution does not link to its source execution")
    for span in control_executed:
        attributes = span.get("attributes")
        if (
            not isinstance(attributes, dict)
            or attributes.get("relay.operation.name") != "effect.execute"
            or "relay.effect.source" in attributes
            or span.get("links") != []
        ):
            raise ValueError("control effect is not a replay-blind wrapper execution")
    if (
        aware_executed[0].get("name") != "relay.effect.execute payments:refund"
        or aware_resolved[0].get("name") != "relay.effect.resolve payments:refund"
    ):
        raise ValueError("aware registered effect span names changed")
    if any(
        span.get("name") != "relay.effect.execute payments:refund"
        for span in control_executed
    ):
        raise ValueError("control registered effect span names changed")

    def parent_outcome(spans: list[Document], effect: Document) -> object:
        parents = {
            span.get("span_id"): span
            for span in spans
            if isinstance(span.get("attributes"), dict)
            and span["attributes"].get("relay.operation.name") == "activation"
        }
        parent = parents.get(effect.get("parent_span_id"))
        if parent is None:
            return None
        return cast(dict[str, Any], parent["attributes"]).get("relay.attempt.outcome")

    if (
        parent_outcome(aware_spans, aware_executed[0]) != "abandoned"
        or parent_outcome(aware_spans, aware_resolved[0]) != "completed"
        or {parent_outcome(control_spans, effect) for effect in control_executed}
        != {"abandoned", "completed"}
    ):
        raise ValueError("registered effect-to-activation topology changed")
    return len(aware_executed), len(aware_resolved), len(control_executed)


def _arm(document: Document, mode: str, signoz_base_url: str) -> Document:
    spans = [
        {
            **span,
            "start_time_unix_nano": str(span["start_time_unix_nano"]),
            "end_time_unix_nano": str(span["end_time_unix_nano"]),
            "duration_nano": str(span["duration_nano"]),
        }
        for span in cast(list[Document], document["spans"])
    ]
    return {
        "mode": mode,
        "traceId": document["trace_id"],
        "serviceName": document["resource"]["service.name"],
        "signozUrl": f"{signoz_base_url.rstrip('/')}/trace/{document['trace_id']}",
        "spans": spans,
    }


def product_document(
    *,
    run_id: str,
    seed: int,
    source: str,
    events: list[Event],
    aware: Document,
    control: Document,
    provider_attempts: int,
    business_executions: int,
    hard_exit_code: int,
    provenance: dict[str, Any],
    query_back: dict[str, Any],
    signoz_base_url: str,
) -> Document:
    if hard_exit_code != 9:
        raise ValueError("product proof requires the registered hard exit code 9")
    if provider_attempts != 1 or business_executions != 1:
        raise ValueError(
            "product proof requires one provider attempt and business record"
        )
    _validate_registered_events(events)
    aware_executed, aware_resolved, control_executed = _validate_projection(
        aware, control
    )

    for mode, document in (("aware", aware), ("control", control)):
        proof = query_back.get(mode)
        if not isinstance(proof, dict) or proof.get("verified") is not True:
            raise ValueError(f"{mode} query-back proof is not verified")
        for field in (
            "identityVerified",
            "serviceNameVerified",
            "attributesVerified",
        ):
            if proof.get(field) is not True:
                raise ValueError(f"{mode} query-back field {field} is not verified")
        span_count = len(cast(list[Document], document["spans"]))
        if (
            proof.get("duplicateRows") != 0
            or proof.get("spansSent") != span_count
            or proof.get("spansRetrieved") != span_count
            or str(proof.get("traceId", "")).lower()
            != str(document.get("trace_id", "")).lower()
        ):
            raise ValueError(f"{mode} query-back does not exactly match the export")
        resource = document.get("resource")
        if not isinstance(resource, dict):
            raise ValueError(f"{mode} export resource is missing")
        service_name = resource.get("service.name")
        if not isinstance(service_name, str):
            raise ValueError(f"{mode} export service name is missing")
        validate_retained_query(
            proof,
            spans=cast(list[dict[str, Any]], document["spans"]),
            service_name=service_name,
        )

    return {
        "schemaVersion": 2,
        "generatedAt": datetime.now(UTC).isoformat(),
        "runId": run_id,
        "seed": seed,
        "source": source,
        "status": "finished",
        "hardExitCode": hard_exit_code,
        "groundTruth": {
            "activationAttempts": 2,
            "effectIntents": 1,
            "effectCompletions": 1,
            "journalResolutions": 1,
            "providerAttempts": provider_attempts,
            "businessExecutions": business_executions,
        },
        "journal": {"events": _events(events)},
        "aware": _arm(aware, "aware", signoz_base_url),
        "control": _arm(control, "control", signoz_base_url),
        "comparison": {
            "awareExecutedEffectSpans": aware_executed,
            "awareResolvedEffectSpans": aware_resolved,
            "controlExecutedEffectSpans": control_executed,
        },
        "queryBack": query_back,
        "provenance": provenance,
    }
