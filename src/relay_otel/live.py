from __future__ import annotations

from pathlib import Path

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from . import VERSION
from .exporter import DurableJsonlExporter


def live_provider(
    mode: str, output: Path | str, *, otlp_endpoint: str | None = None
) -> TracerProvider:
    exporter = DurableJsonlExporter(output)
    provider = TracerProvider(
        sampler=ALWAYS_ON,
        resource=Resource.create(
            {"service.name": f"relay-otel-{mode}-live", "service.version": VERSION}
        ),
    )
    if mode == "batch":
        provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                schedule_delay_millis=60_000,
                max_queue_size=2_048,
                max_export_batch_size=512,
            )
        )
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=otlp_endpoint),
                    schedule_delay_millis=60_000,
                    max_queue_size=2_048,
                    max_export_batch_size=512,
                )
            )
    elif mode == "simple":
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(
                SimpleSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
            )
    else:
        raise ValueError(f"unknown live capture mode: {mode}")
    return provider
