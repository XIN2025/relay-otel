"""Materialize one verified v2 product run as the web app's retained proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from relay_otel.queryback import validate_retained_query

PRODUCT_RUNS_ROOT = PROJECT_ROOT / "receipts" / "work" / "product-runs"
OUTPUT = PROJECT_ROOT / "web" / "data" / "featured.json"
SENSITIVE_FIELDS = {"by", "error", "input", "patch", "reason", "result", "state"}


class MaterializationError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise MaterializationError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_atomic(path: Path, payload: dict[str, Any], *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise FileExistsError(
            f"refusing to replace {path}; pass --replace-retained-proof explicitly"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _local_trace_url(value: object, trace_id: str) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == f"/trace/{trace_id}"
        and not parsed.query
        and not parsed.fragment
    )


def validate_document(value: object) -> dict[str, Any]:
    require(isinstance(value, dict), "product run must be a JSON object")
    document = value
    require(document.get("schemaVersion") == 2, "retained proof must use schema v2")
    require(document.get("hardExitCode") == 9, "retained proof requires exit code 9")
    require(document.get("status") == "finished", "retained proof must be finished")
    require(document.get("seed") == 23, "retained proof must use registered seed 23")

    expected_truth = {
        "activationAttempts": 2,
        "businessExecutions": 1,
        "effectCompletions": 1,
        "effectIntents": 1,
        "journalResolutions": 1,
        "providerAttempts": 1,
    }
    require(
        document.get("groundTruth") == expected_truth,
        "retained proof ground truth differs from the registered scenario",
    )
    require(
        document.get("comparison")
        == {
            "awareExecutedEffectSpans": 1,
            "awareResolvedEffectSpans": 1,
            "controlExecutedEffectSpans": 2,
        },
        "retained proof comparison differs from the registered scenario",
    )

    provenance = document.get("provenance")
    require(isinstance(provenance, dict), "retained proof lacks provenance")
    require(
        isinstance(provenance.get("lineageId"), str) and bool(provenance["lineageId"]),
        "retained proof lacks a lineage id",
    )
    inputs_sha = provenance.get("lineageInputsSha256")
    require(
        isinstance(inputs_sha, str)
        and len(inputs_sha) == 64
        and all(character in "0123456789abcdef" for character in inputs_sha),
        "retained proof lacks a lowercase lineage inputs hash",
    )

    journal = document.get("journal")
    require(isinstance(journal, dict), "retained proof lacks journal evidence")
    events = journal.get("events")
    require(isinstance(events, list) and len(events) == 9, "expected nine v2 events")
    event_counts: dict[str, int] = {}
    for event in events:
        require(isinstance(event, dict), "journal event must be an object")
        event_type = event.get("type")
        require(isinstance(event_type, str), "journal event type must be text")
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        require(
            isinstance(event.get("atUnixNano"), str)
            and str(event["atUnixNano"]).isdigit(),
            "v2 journal time must be a decimal string",
        )
        payload = event.get("payload")
        require(isinstance(payload, dict), "journal payload must be an object")
        for field in SENSITIVE_FIELDS & payload.keys():
            require(
                payload[field] == {"redacted": True},
                f"portable journal field {field!r} is not redacted",
            )
    require(
        event_counts
        == {
            "activation_finished": 1,
            "attempt_abandoned": 1,
            "attempt_started": 2,
            "effect_completed": 1,
            "effect_intent": 1,
            "effect_resolved": 1,
            "run_finished": 1,
            "run_started": 1,
        },
        "journal event counts differ from the registered scenario",
    )

    serialized = json.dumps(document, sort_keys=True)
    require("gen_ai." not in serialized, "active v2 proof contains a GenAI attribute")
    for mode, service_name in (
        ("aware", "relay-otel-aware"),
        ("control", "relay-otel-control"),
    ):
        arm = document.get(mode)
        require(isinstance(arm, dict), f"retained proof lacks {mode} arm")
        require(arm.get("mode") == mode, f"{mode} arm mode is invalid")
        require(
            arm.get("serviceName") == service_name,
            f"{mode} service name is invalid",
        )
        trace_id = arm.get("traceId")
        require(
            isinstance(trace_id, str)
            and len(trace_id) == 32
            and all(character in "0123456789abcdef" for character in trace_id),
            f"{mode} trace id is invalid",
        )
        require(
            _local_trace_url(arm.get("signozUrl"), trace_id),
            f"{mode} SigNoz URL is not the matching loopback trace URL",
        )
        spans = arm.get("spans")
        require(isinstance(spans, list) and len(spans) == 5, f"{mode} needs five spans")
        for span in spans:
            require(isinstance(span, dict), f"{mode} span must be an object")
            require(
                all(
                    isinstance(span.get(field), str) and str(span[field]).isdigit()
                    for field in (
                        "start_time_unix_nano",
                        "end_time_unix_nano",
                        "duration_nano",
                    )
                ),
                f"{mode} span times must be decimal strings",
            )

        query_back = document.get("queryBack")
        require(isinstance(query_back, dict), "retained proof lacks query-back")
        proof = query_back.get(mode)
        require(isinstance(proof, dict), f"retained proof lacks {mode} query-back")
        require(
            proof.get("verified") is True
            and proof.get("identityVerified") is True
            and proof.get("attributesVerified") is True
            and proof.get("serviceNameVerified") is True
            and proof.get("duplicateRows") == 0
            and proof.get("traceId") == trace_id
            and proof.get("spansSent") == 5
            and proof.get("spansRetrieved") == 5,
            f"{mode} query-back is not exact",
        )
        try:
            validate_retained_query(
                proof,
                spans=spans,
                service_name=service_name,
            )
        except ValueError as exc:
            raise MaterializationError(
                f"{mode} retained query rows are not independently valid: {exc}"
            ) from exc
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", type=Path, required=True)
    parser.add_argument("--replace-retained-proof", action="store_true")
    args = parser.parse_args()

    source = (
        args.run_json if args.run_json.is_absolute() else PROJECT_ROOT / args.run_json
    )
    source = source.resolve()
    try:
        source.relative_to(PRODUCT_RUNS_ROOT.resolve())
    except ValueError as exc:
        raise MaterializationError(
            "run JSON must stay under receipts/work/product-runs"
        ) from exc
    require(source.name == "run.json" and source.is_file(), "run JSON is missing")
    document = validate_document(json.loads(source.read_text(encoding="utf-8")))
    write_atomic(OUTPUT, document, replace=args.replace_retained_proof)
    print(
        json.dumps(
            {
                "input": {
                    "path": source.relative_to(PROJECT_ROOT).as_posix(),
                    "sha256": sha256_file(source),
                    "bytes": source.stat().st_size,
                },
                "output": {
                    "path": OUTPUT.relative_to(PROJECT_ROOT).as_posix(),
                    "sha256": sha256_file(OUTPUT),
                    "bytes": OUTPUT.stat().st_size,
                },
                "run_id": document["runId"],
                "schema_version": 2,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
