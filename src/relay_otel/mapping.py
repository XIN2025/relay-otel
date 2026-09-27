"""Generic relay span records and deterministic OpenTelemetry identities."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.trace import SpanKind


def _nonzero_digest(value: str, bytes_count: int) -> int:
    digest = int.from_bytes(
        hashlib.sha256(value.encode("utf-8")).digest()[:bytes_count], "big"
    )
    return digest or 1


def trace_id_for(run_id: str, mode: str) -> int:
    return _nonzero_digest(f"relay-otel:trace:{mode}:{run_id}", 16)


def span_id_for(run_id: str, mode: str, identity: str) -> int:
    return _nonzero_digest(f"relay-otel:span:{mode}:{run_id}:{identity}", 8)


def trace_hex(value: int) -> str:
    return f"{value:032x}"


def span_hex(value: int | None) -> str | None:
    return f"{value:016x}" if value is not None else None


@dataclass(frozen=True)
class SpanLinkRecord:
    trace_id: int
    span_id: int

    def as_dict(self) -> dict[str, str]:
        return {
            "trace_id": trace_hex(self.trace_id),
            "span_id": span_hex(self.span_id) or "",
        }


@dataclass(frozen=True)
class SpanRecord:
    identity: str
    name: str
    trace_id: int
    span_id: int
    parent_span_id: int | None
    start_time_ns: int
    end_time_ns: int
    attributes: dict[str, Any]
    kind: SpanKind = SpanKind.INTERNAL
    status: str = "UNSET"
    links: tuple[SpanLinkRecord, ...] = field(default_factory=tuple)
    classification: str = "structural"

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "name": self.name,
            "trace_id": trace_hex(self.trace_id),
            "span_id": span_hex(self.span_id),
            "parent_span_id": span_hex(self.parent_span_id),
            "kind": self.kind.name,
            "start_time_unix_nano": self.start_time_ns,
            "end_time_unix_nano": self.end_time_ns,
            "duration_nano": self.end_time_ns - self.start_time_ns,
            "status": self.status,
            "attributes": self.attributes,
            "links": [link.as_dict() for link in self.links],
            "classification": self.classification,
        }


def run_attributes(run_id: str, workflow_version: str) -> dict[str, Any]:
    return {
        "relay.operation.name": "run",
        "relay.run.id": run_id,
        "relay.workflow.version": workflow_version,
    }


def activation_attributes(
    *,
    run_id: str,
    node: str,
    activation_id: str,
    attempt_id: str,
    outcome: str,
    recovery: bool,
) -> dict[str, Any]:
    return {
        "relay.operation.name": "activation",
        "relay.run.id": run_id,
        "relay.node.name": node,
        "relay.activation.id": activation_id,
        "relay.attempt.id": attempt_id,
        "relay.attempt.outcome": outcome,
        "relay.attempt.recovery": recovery,
    }


def effect_attributes(
    *,
    run_id: str,
    node: str,
    activation_id: str,
    attempt_id: str,
    effect_id: str,
    index: int,
    label: str,
    adapter_version: str,
    request_fingerprint: str,
    operation: str,
    outcome: str,
    source: str | None = None,
    source_sequence: int | None = None,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "relay.operation.name": operation,
        "relay.run.id": run_id,
        "relay.node.name": node,
        "relay.activation.id": activation_id,
        "relay.attempt.id": attempt_id,
        "relay.effect.id": effect_id,
        "relay.effect.index": index,
        "relay.effect.label": label,
        "relay.effect.adapter.version": adapter_version,
        "relay.effect.request.fingerprint": request_fingerprint,
        "relay.effect.outcome": outcome,
    }
    if source is not None:
        attributes["relay.effect.source"] = source
    if source_sequence is not None:
        attributes["relay.effect.source.sequence"] = source_sequence
    return attributes
