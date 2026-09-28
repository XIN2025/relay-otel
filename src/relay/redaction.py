from __future__ import annotations

from .journal import Event, EventType
from .types import Document

REDACTED: Document = {"redacted": True}

PUBLIC_FIELDS: dict[EventType, tuple[str, ...]] = {
    EventType.RUN_STARTED: (
        "journal_schema_version",
        "workflow_version",
        "start_node",
        "activation_id",
    ),
    EventType.ATTEMPT_STARTED: ("recovery",),
    EventType.ATTEMPT_ABANDONED: (),
    EventType.ATTEMPT_FAILED: (),
    EventType.EFFECT_INTENT: (
        "effect_id",
        "index",
        "label",
        "request_fingerprint",
        "adapter_version",
        "idempotency_key_fingerprint",
        "retry_safe",
        "started_at_ns",
        "retry_of_intent_seq",
    ),
    EventType.EFFECT_COMPLETED: (
        "effect_id",
        "index",
        "label",
        "request_fingerprint",
        "adapter_version",
        "idempotency_key_fingerprint",
        "retry_safe",
        "source_intent_seq",
        "started_at_ns",
        "ended_at_ns",
    ),
    EventType.EFFECT_RESOLVED: (
        "effect_id",
        "index",
        "label",
        "request_fingerprint",
        "adapter_version",
        "idempotency_key_fingerprint",
        "retry_safe",
        "source_effect_seq",
        "source_intent_seq",
        "started_at_ns",
        "ended_at_ns",
    ),
    EventType.ACTIVATION_FINISHED: ("next_node", "next_activation_id"),
    EventType.AWAITING_APPROVAL: (),
    EventType.APPROVAL_GRANTED: ("at_ns",),
    EventType.APPROVAL_DENIED: ("at_ns",),
    EventType.RUN_FINISHED: (),
    EventType.RUN_FAILED: (),
}

REDACTED_FIELDS: dict[EventType, tuple[str, ...]] = {
    EventType.RUN_STARTED: ("input",),
    EventType.ATTEMPT_ABANDONED: ("reason",),
    EventType.ATTEMPT_FAILED: ("error",),
    EventType.EFFECT_COMPLETED: ("result",),
    EventType.ACTIVATION_FINISHED: ("patch",),
    EventType.AWAITING_APPROVAL: ("state",),
    EventType.APPROVAL_GRANTED: ("by",),
    EventType.APPROVAL_DENIED: ("by",),
    EventType.RUN_FINISHED: ("state",),
    EventType.RUN_FAILED: ("reason",),
}


def public_event_payload(event: Event) -> Document:

    result: Document = {
        field: event.payload[field]
        for field in PUBLIC_FIELDS.get(event.type, ())
        if field in event.payload
    }
    for field in REDACTED_FIELDS.get(event.type, ()):
        if field in event.payload:
            result[field] = dict(REDACTED)
    return result
