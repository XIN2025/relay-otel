from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from relay.types import Document

QUERY_PATH = "/api/v5/query_range"
TRACE_ID_PATTERN = re.compile(r"[0-9a-fA-F]{32}")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def local_base_url(base_url: str) -> str:

    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("SigNoz base URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or port is None
    ):
        raise ValueError(
            "SigNoz base URL must be a credential-free loopback HTTP origin with a port"
        )
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"http://{host}:{port}"


def trace_query(
    trace_id: str, *, start_millis: int, end_millis: int, limit: int = 1_000
) -> Document:
    if TRACE_ID_PATTERN.fullmatch(trace_id) is None:
        raise ValueError("trace_id must contain exactly 32 hexadecimal characters")
    if start_millis < 0 or end_millis <= start_millis:
        raise ValueError("trace query time range is invalid")
    if not 1 <= limit <= 10_000:
        raise ValueError("trace query limit must be between 1 and 10000")
    trace_id = trace_id.lower()
    return {
        "start": start_millis,
        "end": end_millis,
        "requestType": "raw",
        "variables": {},
        "compositeQuery": {
            "queries": [
                {
                    "type": "builder_query",
                    "spec": {
                        "name": "A",
                        "signal": "traces",
                        "filter": {"expression": f"trace_id = '{trace_id}'"},
                        "selectFields": [
                            {"name": "trace_id", "fieldContext": "span"},
                            {"name": "span_id", "fieldContext": "span"},
                            {"name": "parent_span_id", "fieldContext": "span"},
                            {"name": "name", "fieldContext": "span"},
                            {"name": "service.name", "fieldContext": "resource"},
                            {
                                "name": "relay.operation.name",
                                "fieldContext": "span",
                                "fieldDataType": "string",
                            },
                            {
                                "name": "relay.effect.source",
                                "fieldContext": "span",
                                "fieldDataType": "string",
                            },
                        ],
                        "order": [{"key": {"name": "timestamp"}, "direction": "asc"}],
                        "limit": limit,
                        "offset": 0,
                        "disabled": False,
                    },
                }
            ]
        },
    }


def query_range(
    base_url: str,
    api_key: str,
    payload: Document,
    *,
    timeout_seconds: float = 5.0,
) -> Document:
    if not 0 < timeout_seconds <= 20:
        raise ValueError("SigNoz query timeout must be between 0 and 20 seconds")
    origin = local_base_url(base_url)
    request = urllib.request.Request(
        origin + QUERY_PATH,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "SIGNOZ-API-KEY": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            document = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(
            f"SigNoz trace query returned {error.code}: {detail}"
        ) from error
    except (TimeoutError, urllib.error.URLError) as error:
        raise RuntimeError(
            "SigNoz trace query did not return before its timeout"
        ) from error
    if not isinstance(document, dict):
        raise RuntimeError("SigNoz trace query returned a non-object response")
    if document.get("status") != "success":
        raise RuntimeError(f"SigNoz trace query was not successful: {document!r}")
    return document


def raw_rows(document: Document) -> list[dict[str, Any]]:
    results = document["data"]["data"]["results"]
    rows: list[dict[str, Any]] = []
    for result in results:
        if result.get("queryName") == "A":
            rows.extend(row["data"] for row in (result.get("rows") or []))
    return rows


def unique_span_rows(document: Document) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in raw_rows(document):
        normalized = dict(row)
        normalized["parent_span_id"] = normalized.get("parent_span_id") or None
        by_id[normalized["span_id"]] = normalized
    return sorted(
        by_id.values(), key=lambda row: (row.get("timestamp", ""), row["span_id"])
    )
