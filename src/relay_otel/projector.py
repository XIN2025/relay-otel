"""Project explicit relay journal facts into honest generic telemetry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relay.journal import JOURNAL_SCHEMA_VERSION, Event, EventType, JournalReader

from .mapping import (
    SpanLinkRecord,
    SpanRecord,
    activation_attributes,
    effect_attributes,
    run_attributes,
    span_id_for,
    trace_id_for,
)


class ProjectionError(ValueError):
    """The journal does not provide a valid, explicit projection source."""


@dataclass
class Attempt:
    start: Event
    events: list[Event] = field(default_factory=list)
    terminal: Event | None = None

    @property
    def attempt_id(self) -> str:
        return _text(self.start.attempt_id, "attempt_id", self.start)

    @property
    def activation_id(self) -> str:
        return _text(self.start.activation_id, "activation_id", self.start)

    @property
    def node(self) -> str:
        return _text(self.start.node, "node", self.start)


def _text(value: object, field: str, event: Event) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectionError(
            f"event {event.seq} field {field!r} must be non-empty text"
        )
    return value


def _integer(value: object, field: str, event: Event) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionError(f"event {event.seq} field {field!r} must be an integer")
    return value


def _time(value: object, field: str, event: Event) -> int:
    timestamp = _integer(value, field, event)
    if timestamp < 1:
        raise ProjectionError(f"event {event.seq} field {field!r} must be positive")
    return timestamp


def _scope(event: Event) -> tuple[str, str, str]:
    return (
        _text(event.node, "node", event),
        _text(event.activation_id, "activation_id", event),
        _text(event.attempt_id, "attempt_id", event),
    )


IDENTITY_FIELDS = (
    "effect_id",
    "index",
    "label",
    "request_fingerprint",
    "adapter_version",
    "idempotency_key_fingerprint",
    "retry_safe",
)


def _effect_identity(event: Event) -> dict[str, Any]:
    identity = {field: event.payload.get(field) for field in IDENTITY_FIELDS}
    _text(identity["effect_id"], "effect_id", event)
    index = _integer(identity["index"], "index", event)
    if index < 0:
        raise ProjectionError(f"event {event.seq} effect index must not be negative")
    _text(identity["label"], "label", event)
    _text(identity["request_fingerprint"], "request_fingerprint", event)
    _text(identity["adapter_version"], "adapter_version", event)
    idempotency_fingerprint = identity["idempotency_key_fingerprint"]
    if idempotency_fingerprint is not None and (
        not isinstance(idempotency_fingerprint, str) or not idempotency_fingerprint
    ):
        raise ProjectionError(
            f"event {event.seq} idempotency fingerprint must be null or non-empty text"
        )
    if not isinstance(identity["retry_safe"], bool):
        raise ProjectionError(f"event {event.seq} retry_safe must be boolean")
    return identity


def _same_effect(left: Event, right: Event) -> None:
    if _scope(left)[:2] != _scope(right)[:2]:
        raise ProjectionError(
            f"effect event {right.seq} does not match source event {left.seq} scope"
        )
    if _effect_identity(left) != _effect_identity(right):
        raise ProjectionError(
            f"effect event {right.seq} identity differs from source event {left.seq}"
        )


def _validate_events(
    events: list[Event], run_id: str
) -> tuple[Event, list[Attempt], dict[int, Event], list[Event], Event | None]:
    if not events:
        raise ProjectionError(f"run {run_id!r} has no journal events")
    if any(event.run_id != run_id for event in events):
        raise ProjectionError("projection received events from multiple runs")
    if any(
        left.seq >= right.seq for left, right in zip(events, events[1:], strict=False)
    ):
        raise ProjectionError("journal sequence numbers are not strictly increasing")
    run_start = events[0]
    if run_start.type is not EventType.RUN_STARTED:
        raise ProjectionError("first event must be run_started")
    if run_start.payload.get("journal_schema_version") != JOURNAL_SCHEMA_VERSION:
        raise ProjectionError("legacy journals cannot be projected by telemetry v2")
    _text(run_start.payload.get("workflow_version"), "workflow_version", run_start)

    attempts: list[Attempt] = []
    attempts_by_id: dict[str, Attempt] = {}
    active_attempt_id: str | None = None
    intents: dict[int, Event] = {}
    completions: dict[int, Event] = {}
    completion_by_intent: dict[int, Event] = {}
    resolutions: list[Event] = []
    run_terminal: Event | None = None

    for event in events[1:]:
        if run_terminal is not None:
            raise ProjectionError(f"event {event.seq} follows terminal run event")
        if event.type is EventType.ATTEMPT_STARTED:
            if active_attempt_id is not None:
                raise ProjectionError(
                    f"attempt {active_attempt_id!r} lacks an explicit terminal fact"
                )
            _scope(event)
            attempt_id = _text(event.attempt_id, "attempt_id", event)
            if attempt_id in attempts_by_id:
                raise ProjectionError(f"duplicate attempt id {attempt_id!r}")
            if not isinstance(event.payload.get("recovery"), bool):
                raise ProjectionError(f"event {event.seq} recovery must be boolean")
            attempt = Attempt(start=event)
            attempts.append(attempt)
            attempts_by_id[attempt_id] = attempt
            active_attempt_id = attempt_id
            continue

        if event.type in (
            EventType.EFFECT_INTENT,
            EventType.EFFECT_COMPLETED,
            EventType.EFFECT_RESOLVED,
            EventType.ATTEMPT_ABANDONED,
            EventType.ATTEMPT_FAILED,
            EventType.ACTIVATION_FINISHED,
        ):
            _, _, attempt_id = _scope(event)
            if active_attempt_id != attempt_id or attempt_id not in attempts_by_id:
                raise ProjectionError(
                    f"event {event.seq} does not belong to the active attempt"
                )
            attempt = attempts_by_id[attempt_id]
            if (event.node, event.activation_id) != (
                attempt.start.node,
                attempt.start.activation_id,
            ):
                raise ProjectionError(f"event {event.seq} changes attempt scope")
            attempt.events.append(event)

            if event.type is EventType.EFFECT_INTENT:
                _effect_identity(event)
                _time(event.payload.get("started_at_ns"), "started_at_ns", event)
                retry_of = event.payload.get("retry_of_intent_seq")
                if retry_of is not None:
                    retry_source = intents.get(
                        _integer(retry_of, "retry_of_intent_seq", event)
                    )
                    if retry_source is None:
                        raise ProjectionError(
                            f"event {event.seq} references missing retry intent"
                        )
                    _same_effect(retry_source, event)
                intents[event.seq] = event

            elif event.type is EventType.EFFECT_COMPLETED:
                _effect_identity(event)
                source_seq = _integer(
                    event.payload.get("source_intent_seq"),
                    "source_intent_seq",
                    event,
                )
                source = intents.get(source_seq)
                if source is None:
                    raise ProjectionError(
                        f"event {event.seq} references missing effect intent {source_seq}"
                    )
                _same_effect(source, event)
                if source.attempt_id != event.attempt_id:
                    raise ProjectionError(
                        f"effect completion {event.seq} changed attempt from its intent"
                    )
                if source_seq in completion_by_intent:
                    raise ProjectionError(
                        f"effect intent {source_seq} has multiple completions"
                    )
                started = _time(
                    event.payload.get("started_at_ns"), "started_at_ns", event
                )
                ended = _time(event.payload.get("ended_at_ns"), "ended_at_ns", event)
                if ended < started:
                    raise ProjectionError(
                        f"effect completion {event.seq} has negative time"
                    )
                completion_by_intent[source_seq] = event
                completions[event.seq] = event

            elif event.type is EventType.EFFECT_RESOLVED:
                _effect_identity(event)
                source_seq = _integer(
                    event.payload.get("source_effect_seq"),
                    "source_effect_seq",
                    event,
                )
                source = completions.get(source_seq)
                if source is None:
                    raise ProjectionError(
                        f"event {event.seq} references missing completion {source_seq}"
                    )
                _same_effect(source, event)
                source_intent_seq = _integer(
                    event.payload.get("source_intent_seq"),
                    "source_intent_seq",
                    event,
                )
                if source.payload.get("source_intent_seq") != source_intent_seq:
                    raise ProjectionError(
                        f"resolution {event.seq} names the wrong source intent"
                    )
                started = _time(
                    event.payload.get("started_at_ns"), "started_at_ns", event
                )
                ended = _time(event.payload.get("ended_at_ns"), "ended_at_ns", event)
                if ended < started:
                    raise ProjectionError(
                        f"effect resolution {event.seq} has negative time"
                    )
                resolutions.append(event)

            else:
                attempt.terminal = event
                active_attempt_id = None

        elif event.type in (
            EventType.AWAITING_APPROVAL,
            EventType.APPROVAL_GRANTED,
            EventType.APPROVAL_DENIED,
        ):
            if active_attempt_id is not None:
                raise ProjectionError(f"approval event {event.seq} overlaps an attempt")

        elif event.type in (EventType.RUN_FINISHED, EventType.RUN_FAILED):
            if active_attempt_id is not None:
                raise ProjectionError(
                    f"terminal run event {event.seq} overlaps an open attempt"
                )
            run_terminal = event

        else:
            raise ProjectionError(
                f"legacy or unsupported event {event.type.value!r} at sequence {event.seq}"
            )

    if not attempts:
        raise ProjectionError(f"run {run_id!r} has no explicit attempts")
    return run_start, attempts, completions, resolutions, run_terminal


def _attempt_end(attempt: Attempt) -> int:
    if attempt.terminal is not None:
        end = attempt.terminal.time_ns
    elif attempt.events:
        end = attempt.events[-1].time_ns
    else:
        end = attempt.start.time_ns
    if end < attempt.start.time_ns:
        raise ProjectionError(f"attempt {attempt.attempt_id!r} has negative time")
    return end


def project_events(
    events: list[Event], *, run_id: str, replay_aware: bool = True
) -> list[SpanRecord]:
    """Project only explicit facts; never execute workflow or routing callables."""

    run_start, attempts, completions, resolutions, run_terminal = _validate_events(
        events, run_id
    )
    mode = "aware" if replay_aware else "control"
    trace_id = trace_id_for(run_id, mode)
    root_id = span_id_for(run_id, mode, "run")
    workflow_version = _text(
        run_start.payload.get("workflow_version"), "workflow_version", run_start
    )
    root_end = run_terminal.time_ns if run_terminal is not None else events[-1].time_ns
    if root_end < run_start.time_ns:
        raise ProjectionError("run has negative time")
    root_attrs = run_attributes(run_id, workflow_version)
    root_attrs["relay.run.outcome"] = (
        "finished"
        if run_terminal is not None and run_terminal.type is EventType.RUN_FINISHED
        else "failed"
        if run_terminal is not None
        else "open"
    )
    records = [
        SpanRecord(
            identity="run",
            name="relay.run",
            trace_id=trace_id,
            span_id=root_id,
            parent_span_id=None,
            start_time_ns=run_start.time_ns,
            end_time_ns=root_end,
            attributes=root_attrs,
            status=(
                "ERROR"
                if run_terminal is not None
                and run_terminal.type is EventType.RUN_FAILED
                else "UNSET"
            ),
        )
    ]

    attempt_span_ids: dict[str, int] = {}
    for attempt in attempts:
        identity = f"attempt:{attempt.attempt_id}"
        attempt_span_id = span_id_for(run_id, mode, identity)
        attempt_span_ids[attempt.attempt_id] = attempt_span_id
        terminal_type = attempt.terminal.type if attempt.terminal is not None else None
        outcome = (
            {
                EventType.ATTEMPT_ABANDONED: "abandoned",
                EventType.ATTEMPT_FAILED: "failed",
                EventType.ACTIVATION_FINISHED: "completed",
            }[terminal_type]
            if terminal_type is not None
            else "open"
        )
        attributes = activation_attributes(
            run_id=run_id,
            node=attempt.node,
            activation_id=attempt.activation_id,
            attempt_id=attempt.attempt_id,
            outcome=outcome,
            recovery=bool(attempt.start.payload["recovery"]),
        )
        if outcome == "abandoned":
            attributes["error.type"] = "relay.attempt.abandoned"
        records.append(
            SpanRecord(
                identity=identity,
                name=f"relay.activation {attempt.node}",
                trace_id=trace_id,
                span_id=attempt_span_id,
                parent_span_id=root_id,
                start_time_ns=attempt.start.time_ns,
                end_time_ns=_attempt_end(attempt),
                attributes=attributes,
                status="ERROR" if outcome in ("abandoned", "failed") else "UNSET",
            )
        )

    completion_span_ids: dict[int, int] = {}
    for completion_seq, completion in completions.items():
        node, activation_id, attempt_id = _scope(completion)
        identity_data = _effect_identity(completion)
        identity = f"effect-execute:{completion_seq}"
        completion_span_id = span_id_for(run_id, mode, identity)
        completion_span_ids[completion_seq] = completion_span_id
        records.append(
            SpanRecord(
                identity=identity,
                name=f"relay.effect.execute {identity_data['label']}",
                trace_id=trace_id,
                span_id=completion_span_id,
                parent_span_id=attempt_span_ids[attempt_id],
                start_time_ns=_time(
                    completion.payload.get("started_at_ns"),
                    "started_at_ns",
                    completion,
                ),
                end_time_ns=_time(
                    completion.payload.get("ended_at_ns"), "ended_at_ns", completion
                ),
                attributes=effect_attributes(
                    run_id=run_id,
                    node=node,
                    activation_id=activation_id,
                    attempt_id=attempt_id,
                    effect_id=str(identity_data["effect_id"]),
                    index=int(identity_data["index"]),
                    label=str(identity_data["label"]),
                    adapter_version=str(identity_data["adapter_version"]),
                    request_fingerprint=str(identity_data["request_fingerprint"]),
                    operation="effect.execute",
                    outcome="executed",
                    source="external" if replay_aware else None,
                ),
                classification="executed",
            )
        )

    for resolution in resolutions:
        node, activation_id, attempt_id = _scope(resolution)
        identity_data = _effect_identity(resolution)
        source_seq = _integer(
            resolution.payload.get("source_effect_seq"),
            "source_effect_seq",
            resolution,
        )
        identity = f"effect-{'resolve' if replay_aware else 'execute'}:{resolution.seq}"
        links: tuple[SpanLinkRecord, ...]
        if replay_aware:
            name = f"relay.effect.resolve {identity_data['label']}"
            attributes = effect_attributes(
                run_id=run_id,
                node=node,
                activation_id=activation_id,
                attempt_id=attempt_id,
                effect_id=str(identity_data["effect_id"]),
                index=int(identity_data["index"]),
                label=str(identity_data["label"]),
                adapter_version=str(identity_data["adapter_version"]),
                request_fingerprint=str(identity_data["request_fingerprint"]),
                operation="effect.resolve",
                outcome="resolved_from_journal",
                source="journal",
                source_sequence=source_seq,
            )
            links = (
                SpanLinkRecord(
                    trace_id=trace_id, span_id=completion_span_ids[source_seq]
                ),
            )
            classification = "resolved"
        else:
            name = f"relay.effect.execute {identity_data['label']}"
            attributes = effect_attributes(
                run_id=run_id,
                node=node,
                activation_id=activation_id,
                attempt_id=attempt_id,
                effect_id=str(identity_data["effect_id"]),
                index=int(identity_data["index"]),
                label=str(identity_data["label"]),
                adapter_version=str(identity_data["adapter_version"]),
                request_fingerprint=str(identity_data["request_fingerprint"]),
                operation="effect.execute",
                outcome="executed",
            )
            links = ()
            classification = "executed"
        records.append(
            SpanRecord(
                identity=identity,
                name=name,
                trace_id=trace_id,
                span_id=span_id_for(run_id, mode, identity),
                parent_span_id=attempt_span_ids[attempt_id],
                start_time_ns=_time(
                    resolution.payload.get("started_at_ns"),
                    "started_at_ns",
                    resolution,
                ),
                end_time_ns=_time(
                    resolution.payload.get("ended_at_ns"),
                    "ended_at_ns",
                    resolution,
                ),
                attributes=attributes,
                links=links,
                classification=classification,
            )
        )

    return records


def project_journal(
    journal_path: Path | str, run_id: str, *, replay_aware: bool = True
) -> list[SpanRecord]:
    reader = JournalReader(journal_path)
    try:
        events = reader.events(run_id)
    finally:
        reader.close()
    return project_events(events, run_id=run_id, replay_aware=replay_aware)
