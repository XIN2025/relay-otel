"""Execute the web product's real crash, resume, project, and OTLP action."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from relay_otel.lineage import (
    LineageValidationError,
    validate_current_lineage,
    validate_frozen_inputs,
)
from relay_otel.runtime_paths import signoz_secret_path

DEFAULT_PRODUCT_DEADLINE_SECONDS = 90.0
MIN_PRODUCT_DEADLINE_SECONDS = 1.0
PRODUCT_DEADLINE_EXIT_CODE = 74
PRODUCT_SOURCES = ("direct-product-run", "local-reproduction", "web-action")


class ProductDeadlineExceeded(TimeoutError):
    def __init__(self, stage: str) -> None:
        super().__init__(f"product deadline expired during {stage}")
        self.stage = stage


class ProductStageFailure(RuntimeError):
    def __init__(self, stage: str, cause_type: str) -> None:
        super().__init__(f"product stage failed: {stage} ({cause_type})")
        self.stage = stage
        self.cause_type = cause_type


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def environment() -> dict[str, str]:
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
    result = {key: os.environ[key] for key in inherited if key in os.environ}
    result.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(SRC),
            "PYTHONUTF8": "1",
        }
    )
    return result


def checkpoint(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        connection.close()


def write_atomic(path: Path, payload: dict) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def remaining_seconds(deadline_monotonic: float, stage: str) -> float:
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise ProductDeadlineExceeded(stage)
    return remaining


def run_child(
    command: list[str], *, deadline_monotonic: float, stage: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=remaining_seconds(deadline_monotonic, stage),
        )
    except subprocess.TimeoutExpired as exc:
        raise ProductDeadlineExceeded(stage) from exc


def retained_product_error(
    error: ProductDeadlineExceeded | ProductStageFailure,
    *,
    run_id: str,
    run_dir: Path,
    started_monotonic: float,
    deadline_seconds: float,
) -> int:
    deadline_exceeded = isinstance(error, ProductDeadlineExceeded)
    code = "product_deadline_exceeded" if deadline_exceeded else "product_stage_failed"
    message = (
        "product run exceeded its end-to-end deadline; inspect retained local state before retrying"
        if deadline_exceeded
        else "product run failed before completion; inspect retained local state before retrying"
    )
    cause_type = (
        type(error).__name__
        if isinstance(error, ProductDeadlineExceeded)
        else error.cause_type
    )
    details: dict[str, object] = {
        "run_id": run_id,
        "run_directory": run_dir.relative_to(PROJECT_ROOT).as_posix(),
        "stage": error.stage,
        "cause_type": cause_type,
        "deadline_seconds": deadline_seconds,
        "elapsed_milliseconds": int((time.monotonic() - started_monotonic) * 1000),
        "run_json_written": (run_dir / "run.json").is_file(),
    }
    failure_path = run_dir / "failure.json"
    details["failure_artifact"] = failure_path.relative_to(PROJECT_ROOT).as_posix()
    failure = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "failed",
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "error": {"code": code, "message": message, "details": details},
    }
    try:
        write_atomic(failure_path, failure)
    except OSError as retention_error:
        details["failure_artifact"] = None
        details["retention_failure_type"] = type(retention_error).__name__
    print(
        json.dumps(
            {"error": {"code": code, "message": message, "details": details}},
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return PRODUCT_DEADLINE_EXIT_CODE if deadline_exceeded else 1


def selected_lineage(candidate_inputs: Path | None):
    if candidate_inputs is None:
        return validate_current_lineage(PROJECT_ROOT)
    candidate_path = candidate_inputs
    if not candidate_path.is_absolute():
        candidate_path = PROJECT_ROOT / candidate_path
    return validate_frozen_inputs(PROJECT_ROOT, candidate_path)


def lineage_error(exc: LineageValidationError) -> int:
    error = {
        "error": {
            "code": "lineage_mismatch",
            "message": "selected evidence lineage validation failed before execution",
            "details": {
                "reason_code": exc.code.lower(),
                "reason_details": exc.details,
            },
        }
    }
    print(json.dumps(error, separators=(",", ":"), sort_keys=True), file=sys.stderr)
    return 78


def lineage_changed_error(
    exc: LineageValidationError, *, run_dir: Path, stage: str
) -> int:
    error = {
        "error": {
            "code": "lineage_changed_during_run",
            "message": (
                "selected evidence lineage changed after execution began; "
                "run.json was withheld and staged local effects/evidence remain"
            ),
            "details": {
                "reason_code": exc.code.lower(),
                "reason_details": exc.details,
                "run_directory": run_dir.relative_to(PROJECT_ROOT).as_posix(),
                "stage": stage,
            },
        }
    }
    print(json.dumps(error, separators=(",", ":"), sort_keys=True), file=sys.stderr)
    return 75


def revalidate_selected_lineage(
    candidate_inputs: Path | None, expected_provenance: dict
) -> None:
    validated = selected_lineage(candidate_inputs)
    actual = validated.provenance()
    for field in (
        "lineageId",
        "lineageInputsSha256",
        "lineageManifestSha256",
    ):
        if actual.get(field) != expected_provenance.get(field):
            raise LineageValidationError(
                "LINEAGE_CHANGED_DURING_RUN",
                "selected evidence lineage changed while the product proof was running",
                details={
                    "field": field,
                    "expected": expected_provenance.get(field),
                    "actual": actual.get(field),
                },
            )


def execute_product(
    args: argparse.Namespace,
    *,
    lineage_provenance: dict,
    run_id: str,
    run_dir: Path,
    deadline_monotonic: float,
) -> dict | int:
    stage = "lineage_preflight"
    try:
        remaining_seconds(deadline_monotonic, stage)

        # Load the runtime only after the canonical source set has passed its first
        # freshness check. Later checks prevent publication if that set changes.
        from relay.journal import JournalReader
        from relay_otel.demo import RefundStore
        from relay_otel.exporter import emit_records
        from relay_otel.product import product_document
        from relay_otel.projector import project_journal
        from relay_otel.queryback import runtime_api_key, wait_for_trace

        journal_path = run_dir / "journal.sqlite"
        effect_path = run_dir / "effects.sqlite"
        aware_path = run_dir / "aware-spans.json"
        control_path = run_dir / "control-spans.json"
        base_command = [
            sys.executable,
            "-m",
            "relay_otel.crash_runner",
            "--journal",
            str(journal_path),
            "--effects",
            str(effect_path),
            "--run-id",
            run_id,
            "--amount-cents",
            str(6200 + args.seed),
            "--live-mode",
            "none",
        ]

        stage = "crash_child"
        crashed = run_child(
            base_command,
            deadline_monotonic=deadline_monotonic,
            stage=stage,
        )
        stage = "resume_child"
        resumed = run_child(
            [*base_command, "--resume"],
            deadline_monotonic=deadline_monotonic,
            stage=stage,
        )
        if crashed.returncode != 9 or resumed.returncode != 0:
            raise RuntimeError(
                "product children returned unexpected exit codes: "
                f"crash={crashed.returncode}, resume={resumed.returncode}"
            )

        stage = "post_resume_lineage"
        remaining_seconds(deadline_monotonic, stage)
        try:
            revalidate_selected_lineage(args.candidate_inputs, lineage_provenance)
        except LineageValidationError as exc:
            return lineage_changed_error(exc, run_dir=run_dir, stage="post_resume")

        stage = "journal_snapshot"
        remaining_seconds(deadline_monotonic, stage)
        checkpoint(journal_path)
        checkpoint(effect_path)
        journal_sha = sha256_file(journal_path)
        remaining_seconds(deadline_monotonic, stage)

        stage = "aware_otlp_export"
        aware_records = project_journal(journal_path, run_id, replay_aware=True)
        remaining_seconds(deadline_monotonic, stage)
        aware = emit_records(
            aware_records,
            aware_path,
            mode="aware",
            run_id=run_id,
            journal_path=journal_path.relative_to(PROJECT_ROOT).as_posix(),
            journal_sha256=journal_sha,
            otlp_endpoint=args.otlp_endpoint,
            artifact_root=PROJECT_ROOT,
            provenance=lineage_provenance,
            deadline_monotonic=deadline_monotonic,
        )
        remaining_seconds(deadline_monotonic, stage)

        stage = "control_otlp_export"
        control_records = project_journal(journal_path, run_id, replay_aware=False)
        remaining_seconds(deadline_monotonic, stage)
        control = emit_records(
            control_records,
            control_path,
            mode="control",
            run_id=run_id,
            journal_path=journal_path.relative_to(PROJECT_ROOT).as_posix(),
            journal_sha256=journal_sha,
            otlp_endpoint=args.otlp_endpoint,
            artifact_root=PROJECT_ROOT,
            provenance=lineage_provenance,
            deadline_monotonic=deadline_monotonic,
        )
        remaining_seconds(deadline_monotonic, stage)

        stage = "ground_truth_read"
        journal = JournalReader(journal_path)
        try:
            events = journal.events(run_id)
        finally:
            journal.close()
        store = RefundStore(effect_path)
        try:
            external = store.summary()
        finally:
            store.close()
        remaining_seconds(deadline_monotonic, stage)

        stage = "query_back_credentials"
        api_key = runtime_api_key(args.signoz_api_key_file)
        query_back: dict[str, object] = {}
        for mode, exported in (("aware", aware), ("control", control)):
            stage = f"query_back_{mode}"
            remaining_seconds(deadline_monotonic, stage)
            query_back[mode] = wait_for_trace(
                base_url=args.signoz_url,
                api_key=api_key,
                trace_id=exported["trace_id"],
                spans=exported["spans"],
                service_name=exported["resource"]["service.name"],
                deadline_monotonic=deadline_monotonic,
            )
            remaining_seconds(deadline_monotonic, stage)

        stage = "post_queryback_lineage"
        try:
            revalidate_selected_lineage(args.candidate_inputs, lineage_provenance)
        except LineageValidationError as exc:
            return lineage_changed_error(exc, run_dir=run_dir, stage="post_queryback")
        remaining_seconds(deadline_monotonic, stage)

        stage = "product_document"
        document = product_document(
            run_id=run_id,
            seed=args.seed,
            source=args.source,
            events=events,
            aware=aware,
            control=control,
            provider_attempts=external["attempts"],
            business_executions=external["business_executions"],
            hard_exit_code=crashed.returncode,
            query_back=query_back,
            signoz_base_url=args.signoz_url,
            provenance={
                **lineage_provenance,
                "journalPath": journal_path.relative_to(PROJECT_ROOT).as_posix(),
                "journalSha256": journal_sha,
            },
        )
        remaining_seconds(deadline_monotonic, stage)

        stage = "product_write"
        write_atomic(run_dir / "run.json", document)
        return document
    except ProductDeadlineExceeded:
        raise
    except Exception as exc:
        if time.monotonic() >= deadline_monotonic:
            raise ProductDeadlineExceeded(stage) from exc
        raise ProductStageFailure(stage, type(exc).__name__) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument(
        "--source",
        choices=PRODUCT_SOURCES,
        default="direct-product-run",
        help="bounded invocation provenance retained in the product document",
    )
    parser.add_argument("--otlp-endpoint", default="http://127.0.0.1:4318/v1/traces")
    parser.add_argument("--signoz-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--candidate-inputs",
        type=Path,
        help="explicit frozen inputs used only while generating a not-yet-current lineage",
    )
    parser.add_argument(
        "--signoz-api-key-file",
        type=Path,
        help="host-local key file; defaults to the platform-local runtime path",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_PRODUCT_DEADLINE_SECONDS,
        help="one end-to-end product budget, capped at 90 seconds",
    )
    args = parser.parse_args()
    if args.signoz_api_key_file is None:
        args.signoz_api_key_file = signoz_secret_path()
    if not 0 <= args.seed <= 10_000:
        raise ValueError("seed must be between 0 and 10000")
    if (
        not math.isfinite(args.deadline_seconds)
        or not MIN_PRODUCT_DEADLINE_SECONDS
        <= args.deadline_seconds
        <= DEFAULT_PRODUCT_DEADLINE_SECONDS
    ):
        raise ValueError("deadline-seconds must be finite and between 1 and 90")

    started_monotonic = time.monotonic()
    deadline_monotonic = started_monotonic + args.deadline_seconds
    try:
        validated_lineage = selected_lineage(args.candidate_inputs)
    except LineageValidationError as exc:
        return lineage_error(exc)
    lineage_provenance = validated_lineage.provenance()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:-3]
    run_id = f"product-{stamp}-{args.seed:04d}-{secrets.token_hex(3)}"
    run_dir = PROJECT_ROOT / "receipts" / "work" / "product-runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        result = execute_product(
            args,
            lineage_provenance=lineage_provenance,
            run_id=run_id,
            run_dir=run_dir,
            deadline_monotonic=deadline_monotonic,
        )
    except (ProductDeadlineExceeded, ProductStageFailure) as error:
        return retained_product_error(
            error,
            run_id=run_id,
            run_dir=run_dir,
            started_monotonic=started_monotonic,
            deadline_seconds=args.deadline_seconds,
        )
    if isinstance(result, int):
        return result
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
