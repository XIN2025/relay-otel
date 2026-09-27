"""Emit projected records through the OpenTelemetry SDK to JSON and optional OTLP."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import secrets
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags

from relay.types import Document

from . import VERSION
from .mapping import SpanRecord, span_hex, trace_hex


class CaptureExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


class PlannedIdGenerator(IdGenerator):
    def __init__(self, trace_id: int, span_ids: Sequence[int]) -> None:
        self.trace_id = trace_id
        self.span_ids = iter(span_ids)

    def generate_span_id(self) -> int:
        try:
            return next(self.span_ids)
        except StopIteration as exc:
            raise RuntimeError(
                "OpenTelemetry requested an unregistered span id"
            ) from exc

    def generate_trace_id(self) -> int:
        return self.trace_id


class OtlpDeliveryError(RuntimeError):
    """An OTLP failure whose staged envelope and receipt remain on disk."""

    def __init__(self, result: str, receipt_path: Path) -> None:
        super().__init__(
            f"OTLP delivery failed with {result}; durable receipt: {receipt_path}"
        )
        self.result = result
        self.receipt_path = receipt_path


class OtlpCleanupError(RuntimeError):
    """Delivery was acknowledged, but exporter shutdown did not complete."""

    def __init__(self, receipt_path: Path) -> None:
        super().__init__(
            "OTLP delivery was acknowledged but exporter shutdown failed; "
            f"do not retry blindly; durable receipt: {receipt_path}"
        )
        self.receipt_path = receipt_path


def _resource(mode: str) -> Resource:
    return Resource.create(
        {
            "service.name": f"relay-otel-{mode}",
            "service.version": VERSION,
            "telemetry.sdk.language": "python",
        }
    )


def _write_json_exclusive(path: Path, document: Document) -> None:
    """Atomically create one immutable, fsynced JSON artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.parent / (f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to overwrite immutable span export artifact: {path}"
            ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path, artifact_root: Path) -> Document:
    try:
        relative = path.resolve().relative_to(artifact_root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("export artifacts must stay under artifact_root") from exc
    return {
        "path": relative,
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _derived_path(output: Path, label: str) -> Path:
    return output.with_name(f"{output.stem}.{label}{output.suffix}")


def _safe_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return "<invalid-endpoint>"


def _validated_loopback_endpoint(endpoint: str | None) -> None:
    if endpoint is None:
        return
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        loopback = hostname.lower() == "localhost"
        if not loopback:
            loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError as exc:
        raise ValueError(
            "otlp_endpoint must be an uncredentialed loopback HTTP(S) URL"
        ) from exc
    if not loopback:
        raise ValueError("otlp_endpoint must resolve literally to a loopback address")


def _captured_identity(span: ReadableSpan) -> tuple[int, int]:
    context = span.context
    if context is None:
        raise ValueError("captured span has no context")
    return context.trace_id, context.span_id


def _remaining_timeout(deadline_monotonic: float | None) -> float | None:
    if deadline_monotonic is None:
        return None
    if not math.isfinite(deadline_monotonic):
        raise ValueError("deadline_monotonic must be finite")
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("OTLP export deadline expired")
    return remaining


def emit_records(
    records: list[SpanRecord],
    output: Path | str,
    *,
    mode: str,
    run_id: str,
    journal_path: str,
    journal_sha256: str,
    otlp_endpoint: str | None = None,
    allow_replace: bool = False,
    artifact_root: Path | str | None = None,
    provenance: Document | None = None,
    deadline_monotonic: float | None = None,
) -> Document:
    if allow_replace:
        raise ValueError("allow_replace is incompatible with immutable exports")
    if not records:
        raise ValueError("cannot export an empty span list")
    _remaining_timeout(deadline_monotonic)
    trace_ids = {record.trace_id for record in records}
    if len(trace_ids) != 1:
        raise ValueError("one export must contain exactly one trace id")
    planned_identity_list = [(record.trace_id, record.span_id) for record in records]
    if len(set(planned_identity_list)) != len(planned_identity_list):
        raise ValueError("one export cannot contain duplicate planned span identities")
    _validated_loopback_endpoint(otlp_endpoint)
    output_path = Path(output)
    resolved_artifact_root = (
        Path(__file__).resolve().parents[2]
        if artifact_root is None
        else Path(artifact_root)
    )
    try:
        output_path.resolve().relative_to(resolved_artifact_root.resolve())
    except ValueError as exc:
        raise ValueError("output must stay under artifact_root") from exc
    staged_path = _derived_path(output_path, "staged")
    delivery_path = _derived_path(output_path, "delivery")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    collisions = [
        str(path) for path in (output_path, staged_path, delivery_path) if path.exists()
    ]
    if collisions:
        raise FileExistsError(
            "refusing export because immutable output paths already exist: "
            + ", ".join(collisions)
        )

    provider = TracerProvider(
        sampler=ALWAYS_ON,
        resource=_resource(mode),
        id_generator=PlannedIdGenerator(
            records[0].trace_id, [record.span_id for record in records]
        ),
    )
    capture = CaptureExporter()
    provider.add_span_processor(SimpleSpanProcessor(capture))
    try:
        tracer = provider.get_tracer("relay_otel.projector", VERSION)
        handles: dict[int, trace.Span] = {}
        for record in records:
            parent_context = None
            if record.parent_span_id is not None:
                parent = handles.get(record.parent_span_id)
                if parent is None:
                    raise ValueError(
                        f"parent span was not created first: {record.identity}"
                    )
                parent_context = trace.set_span_in_context(parent)
            links = [
                Link(
                    SpanContext(
                        trace_id=link.trace_id,
                        span_id=link.span_id,
                        is_remote=False,
                        trace_flags=TraceFlags(TraceFlags.SAMPLED),
                    )
                )
                for link in record.links
            ]
            span = tracer.start_span(
                record.name,
                context=parent_context,
                kind=record.kind,
                attributes=record.attributes,
                links=links,
                start_time=record.start_time_ns,
            )
            if record.status == "ERROR":
                span.set_status(Status(StatusCode.ERROR))
            handles[record.span_id] = span
        for record in reversed(records):
            handles[record.span_id].end(end_time=record.end_time_ns)
        flush_timeout = _remaining_timeout(deadline_monotonic)
        flushed = provider.force_flush(
            timeout_millis=(
                30_000 if flush_timeout is None else max(1, int(flush_timeout * 1000))
            )
        )
        if not flushed:
            raise TimeoutError("OpenTelemetry SDK capture exceeded its deadline")
        captured = list(capture.spans)

        captured_identity_list = [_captured_identity(span) for span in captured]
        sdk_identity_match = (
            len(captured_identity_list) == len(planned_identity_list)
            and len(set(captured_identity_list)) == len(captured_identity_list)
            and set(planned_identity_list) == set(captured_identity_list)
        )
        if not sdk_identity_match:
            raise RuntimeError(
                "OpenTelemetry SDK capture changed planned trace identities"
            )

        safe_endpoint = _safe_endpoint(otlp_endpoint)
        staged: Document = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": mode,
            "run_id": run_id,
            "journal": {"path": journal_path, "sha256": journal_sha256},
            "resource": dict(_resource(mode).attributes),
            "trace_id": trace_hex(records[0].trace_id),
            "span_count": len(records),
            "sdk_capture_count": len(captured),
            "sdk_identity_match": sdk_identity_match,
            "otlp": {
                "attempted": False,
                "endpoint": safe_endpoint,
                "result": None,
                "state": "STAGED",
            },
            "spans": [record.as_dict() for record in records],
        }
        if provenance is not None:
            staged["provenance"] = provenance
        _write_json_exclusive(staged_path, staged)
        staged_reference = _artifact(staged_path, resolved_artifact_root)

        attempted = otlp_endpoint is not None
        acknowledged = False
        result_name: str | None = None
        state = "NOT_REQUESTED"
        delivery_failure: Exception | None = None
        cleanup_failure: Exception | None = None
        cleanup_state = "NOT_REQUESTED"
        if otlp_endpoint is not None:
            exporter = None
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(
                    endpoint=otlp_endpoint,
                    timeout=_remaining_timeout(deadline_monotonic),
                )
                result = exporter.export(captured)
                result_name = result.name
                acknowledged = result is SpanExportResult.SUCCESS
                state = "DELIVERED" if acknowledged else "FAILED"
            except Exception as exc:
                delivery_failure = exc
                result_name = type(exc).__name__
                state = "FAILED"
            finally:
                if exporter is not None:
                    try:
                        exporter.shutdown()  # type: ignore[no-untyped-call]
                        cleanup_state = "SUCCESS"
                    except Exception as exc:
                        cleanup_failure = exc
                        cleanup_state = "FAILED"

        delivery: Document = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "mode": mode,
            "state": state,
            "attempted": attempted,
            "acknowledged": acknowledged,
            "endpoint": safe_endpoint,
            "exporter_result": result_name,
            "cleanup_state": cleanup_state,
            "staged_envelope": staged_reference,
        }
        if delivery_failure is not None:
            delivery["failure"] = {
                "type": type(delivery_failure).__name__,
                "detail": "the OTLP exporter raised without acknowledging delivery",
            }
        if cleanup_failure is not None:
            delivery["cleanup_failure"] = {
                "type": type(cleanup_failure).__name__,
                "detail": "the OTLP exporter failed during shutdown",
            }
        _write_json_exclusive(delivery_path, delivery)
        delivery_reference = _artifact(delivery_path, resolved_artifact_root)
        if state == "FAILED":
            raise OtlpDeliveryError(result_name or "FAILED", delivery_path)

        document = {
            **staged,
            "otlp": {
                "attempted": attempted,
                "endpoint": safe_endpoint,
                "result": result_name,
                "state": state,
                "acknowledged": acknowledged,
                "cleanup_state": cleanup_state,
                "staged_envelope": staged_reference,
                "delivery_receipt": delivery_reference,
            },
        }
        _write_json_exclusive(output_path, document)
        if cleanup_failure is not None:
            raise OtlpCleanupError(delivery_path)
        return document
    finally:
        provider.shutdown()


def readable_span_document(span: ReadableSpan) -> Document:
    context = span.context
    parent = span.parent
    return {
        "name": span.name,
        "trace_id": trace_hex(context.trace_id) if context else None,
        "span_id": span_hex(context.span_id) if context else None,
        "parent_span_id": span_hex(parent.span_id) if parent else None,
        "kind": span.kind.name,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "status": span.status.status_code.name,
        "attributes": dict(span.attributes or {}),
    }


class DurableJsonlExporter(SpanExporter):
    """A synchronous control exporter whose completed lines survive hard process exit."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self.path.open("ab", buffering=0) as stream:
            for span in spans:
                line = (
                    json.dumps(readable_span_document(span), sort_keys=True).encode(
                        "utf-8"
                    )
                    + b"\n"
                )
                stream.write(line)
            os.fsync(stream.fileno())
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None
