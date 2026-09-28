from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .signoz import query_range, raw_rows, trace_query, unique_span_rows

SELECTED_ROW_FIELDS = frozenset(
    {
        "effectSource",
        "name",
        "operationName",
        "parentSpanId",
        "serviceName",
        "spanId",
        "traceId",
    }
)


def runtime_api_key(secret_path: Path) -> str:
    if not secret_path.is_file():
        raise ValueError("local SigNoz API key file does not exist")
    for line in secret_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key == "SIGNOZ_API_KEY" and value:
            return value
    raise ValueError("SIGNOZ_API_KEY is missing from the local runtime env")


def _selected_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = [
        {
            "traceId": str(row.get("trace_id", "")).lower(),
            "spanId": str(row.get("span_id", "")).lower(),
            "parentSpanId": (
                str(row["parent_span_id"]).lower()
                if row.get("parent_span_id")
                else None
            ),
            "name": str(row.get("name", "")),
            "serviceName": str(row.get("service.name", "")),
            "operationName": str(row.get("relay.operation.name", "")),
            "effectSource": (
                str(row.get("relay.effect.source"))
                if row.get("relay.effect.source") not in (None, "")
                else None
            ),
        }
        for row in rows
    ]
    return sorted(
        selected,
        key=lambda row: (
            row["spanId"],
            row["name"],
            row["parentSpanId"] or "",
            row["operationName"],
            row["effectSource"] or "",
        ),
    )


def validate_retained_query(
    proof: dict[str, Any], *, spans: list[dict[str, Any]], service_name: str
) -> None:

    if not spans or not service_name:
        raise ValueError("retained query verification requires spans and service name")
    trace_ids = {str(span.get("trace_id", "")).lower() for span in spans}
    if len(trace_ids) != 1:
        raise ValueError("retained query spans must share one trace identity")
    trace_id = next(iter(trace_ids))
    start_millis = min(int(span["start_time_unix_nano"]) for span in spans) // 1_000_000
    end_millis = max(int(span["end_time_unix_nano"]) for span in spans) // 1_000_000
    expected_request = trace_query(
        trace_id,
        start_millis=max(0, start_millis - 300_000),
        end_millis=end_millis + 300_000,
        limit=max(100, len(spans) * 4),
    )
    if proof.get("request") != expected_request:
        raise ValueError(
            "retained query request differs from the registered trace query"
        )
    rows = proof.get("selectedRows")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or set(row) != SELECTED_ROW_FIELDS for row in rows
    ):
        raise ValueError("retained selected-field rows have an invalid shape")
    expected_rows = sorted(
        [
            {
                "traceId": trace_id,
                "spanId": str(span["span_id"]).lower(),
                "parentSpanId": (
                    str(span["parent_span_id"]).lower()
                    if span.get("parent_span_id")
                    else None
                ),
                "name": str(span["name"]),
                "serviceName": service_name,
                "operationName": str(
                    span.get("attributes", {}).get("relay.operation.name", "")
                ),
                "effectSource": (
                    str(span.get("attributes", {}).get("relay.effect.source"))
                    if span.get("attributes", {}).get("relay.effect.source") is not None
                    else None
                ),
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
    if rows != expected_rows:
        raise ValueError(
            "retained SigNoz rows differ from the exported selected fields"
        )
    expected_summary = {
        "attributesVerified": True,
        "duplicateRows": 0,
        "identityVerified": True,
        "serviceNameVerified": True,
        "spansRetrieved": len(expected_rows),
        "spansSent": len(spans),
        "traceId": trace_id,
        "verified": True,
    }
    for field, expected_value in expected_summary.items():
        if proof.get(field) != expected_value:
            raise ValueError(f"retained query summary field changed: {field}")
    attempts = proof.get("queryAttempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise ValueError("retained query attempt count is invalid")


def wait_for_trace(
    *,
    base_url: str,
    api_key: str,
    trace_id: str,
    spans: list[dict[str, Any]],
    service_name: str,
    maximum_attempts: int = 30,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    if not spans:
        raise ValueError("cannot query back an empty trace")
    if maximum_attempts < 1:
        raise ValueError("maximum_attempts must be positive")
    deadline = (
        time.monotonic() + 30.0 if deadline_monotonic is None else deadline_monotonic
    )
    if deadline <= time.monotonic():
        raise ValueError("query-back deadline must be in the future")
    start_millis = min(span["start_time_unix_nano"] for span in spans) // 1_000_000
    end_millis = max(span["end_time_unix_nano"] for span in spans) // 1_000_000
    request = trace_query(
        trace_id,
        start_millis=max(0, start_millis - 300_000),
        end_millis=end_millis + 300_000,
        limit=max(100, len(spans) * 4),
    )
    if not service_name:
        raise ValueError("service_name must not be empty")
    expected = {
        str(span["span_id"]).lower(): {
            "trace_id": trace_id.lower(),
            "span_id": str(span["span_id"]).lower(),
            "parent_span_id": (
                str(span["parent_span_id"]).lower()
                if span.get("parent_span_id")
                else None
            ),
            "name": str(span["name"]),
        }
        for span in spans
    }
    expected_attributes = {
        str(span["span_id"]).lower(): {
            "relay.operation.name": str(
                span.get("attributes", {}).get("relay.operation.name", "")
            ),
            "relay.effect.source": (
                str(span.get("attributes", {}).get("relay.effect.source"))
                if span.get("attributes", {}).get("relay.effect.source") is not None
                else None
            ),
        }
        for span in spans
    }
    if any(
        not attributes["relay.operation.name"]
        for attributes in expected_attributes.values()
    ):
        raise ValueError("planned spans require relay.operation.name")
    if len(expected) != len(spans):
        raise ValueError("planned trace contains duplicate span IDs")
    last_problem = "trace was not returned"
    attempts_made = 0
    for attempt in range(1, maximum_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempts_made = attempt
        try:
            response = query_range(
                base_url,
                api_key,
                request,
                timeout_seconds=max(0.1, min(5.0, remaining)),
            )
        except RuntimeError as error:
            last_problem = str(error)
            if attempt < maximum_attempts:
                sleep_seconds = min(1.0, max(0.0, deadline - time.monotonic()))
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
            continue
        rows = raw_rows(response)
        unique = unique_span_rows(response)
        actual = {
            str(row.get("span_id")).lower(): {
                "trace_id": str(row.get("trace_id", "")).lower(),
                "span_id": str(row.get("span_id")).lower(),
                "parent_span_id": (
                    str(row["parent_span_id"]).lower()
                    if row.get("parent_span_id")
                    else None
                ),
                "name": str(row.get("name")),
            }
            for row in unique
        }
        actual_attributes = {
            str(row.get("span_id")).lower(): {
                "relay.operation.name": str(row.get("relay.operation.name", "")),
                "relay.effect.source": (
                    str(row.get("relay.effect.source"))
                    if row.get("relay.effect.source") not in (None, "")
                    else None
                ),
            }
            for row in unique
        }
        duplicate_rows = len(rows) - len(unique)
        services = {str(row.get("service.name", "")) for row in unique}
        identity_matches = actual == expected
        attributes_match = actual_attributes == expected_attributes
        service_matches = services == {service_name}
        if (
            identity_matches
            and attributes_match
            and service_matches
            and duplicate_rows == 0
        ):
            selected_rows = _selected_rows(rows)
            return {
                "traceId": trace_id,
                "spansSent": len(spans),
                "spansRetrieved": len(unique),
                "queryAttempts": attempt,
                "duplicateRows": 0,
                "identityVerified": True,
                "attributesVerified": True,
                "serviceNameVerified": True,
                "request": request,
                "selectedRows": selected_rows,
                "verified": True,
            }
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        mismatched = sorted(
            span_id
            for span_id in set(expected) & set(actual)
            if expected[span_id] != actual[span_id]
        )
        last_problem = (
            f"missing={missing}, unexpected={unexpected}, mismatched={mismatched}, "
            f"attributes_match={attributes_match}, duplicate_rows={duplicate_rows}, "
            f"services={sorted(services)!r}"
        )
        if attempt < maximum_attempts:
            sleep_seconds = min(1.0, max(0.0, deadline - time.monotonic()))
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    raise RuntimeError(
        f"trace {trace_id} failed exact query-back verification after "
        f"{attempts_made} attempts within its deadline: {last_problem}"
    )
