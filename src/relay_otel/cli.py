"""Command-line surface for projection, comparison, export, and local diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import cast

from relay.types import Document

from .exporter import emit_records
from .lineage import LineageValidationError, validate_current_lineage
from .mapping import SpanRecord
from .projector import project_journal
from .signoz import local_base_url

PROJECT_MARKERS = (
    Path("pyproject.toml"),
    Path("deploy/IMAGE-PINS.json"),
    Path("deploy/HISTOGRAM-PINS.json"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def records_for(journal: Path, run_id: str, mode: str) -> list[SpanRecord]:
    if mode == "aware":
        return project_journal(journal, run_id, replay_aware=True)
    return project_journal(journal, run_id, replay_aware=False)


def resolve_project_root(value: Path | None) -> Path:
    candidate = Path.cwd() if value is None else value
    resolved = candidate.resolve()
    missing = [
        marker.as_posix()
        for marker in PROJECT_MARKERS
        if not (resolved / marker).is_file()
    ]
    if missing:
        source = "the current directory" if value is None else "--project-root"
        raise ValueError(
            f"{source} is not a relay-otel project root; missing: {', '.join(missing)}"
        )
    return resolved


def _project_path(project_root: Path, value: str, *, label: str) -> Path:
    candidate = Path(value)
    absolute = candidate if candidate.is_absolute() else project_root / candidate
    lexical = absolute.absolute()
    try:
        lexical.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay under the project root") from exc
    current = lexical
    while current != project_root:
        if current.exists() and (current.is_symlink() or current.is_junction()):
            raise ValueError(f"{label} must not traverse a symbolic link or junction")
        current = current.parent
    resolved = lexical.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay under the project root") from exc
    return resolved


def _snapshot_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.journal.sqlite")


def _derived_export_path(output: Path, label: str) -> Path:
    return output.with_name(f"{output.stem}.{label}{output.suffix}")


def _preflight_export_paths(output: Path, snapshot: Path) -> None:
    paths = (
        output,
        _derived_export_path(output, "staged"),
        _derived_export_path(output, "delivery"),
        snapshot,
    )
    if len(set(paths)) != len(paths):
        raise ValueError("derived evidence paths collide")
    collisions = [path for path in paths if path.exists()]
    if collisions:
        raise FileExistsError(
            "refusing to overwrite immutable evidence artifacts: "
            + ", ".join(str(path) for path in collisions)
        )


def _sqlite_snapshot(source_path: Path, destination_path: Path) -> None:

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.parent / (
        f".{destination_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    )
    source = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(temporary)
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError("SQLite evidence snapshot failed integrity_check")
        destination.close()
        destination = None
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, destination_path)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to overwrite immutable journal snapshot: {destination_path}"
            ) from exc
    finally:
        if destination is not None:
            destination.close()
        source.close()
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def project_command(args: argparse.Namespace) -> int:
    project_root = cast(Path, args.project_root)
    lineage = validate_current_lineage(project_root)
    journal = _project_path(project_root, args.journal, label="journal")
    output = _project_path(project_root, args.output, label="output")
    if not journal.is_file():
        raise FileNotFoundError(f"journal does not exist: {journal}")
    snapshot = _snapshot_path(output)
    _preflight_export_paths(output, snapshot)
    _sqlite_snapshot(journal, snapshot)
    revalidated = validate_current_lineage(project_root)
    if revalidated.provenance() != lineage.provenance():
        raise LineageValidationError(
            "LINEAGE_CHANGED_DURING_EXPORT",
            "the current evidence lineage changed while the journal was snapshotted",
        )
    snapshot_relative = snapshot.relative_to(project_root).as_posix()
    records = records_for(snapshot, args.run_id, args.mode)
    document = emit_records(
        records,
        output,
        mode=args.mode,
        run_id=args.run_id,
        journal_path=snapshot_relative,
        journal_sha256=sha256_file(snapshot),
        otlp_endpoint=args.otlp_endpoint,
        artifact_root=project_root,
        provenance=lineage.provenance(),
    )
    print(
        json.dumps(
            {
                "mode": args.mode,
                "output": str(output),
                "span_count": document["span_count"],
                "trace_id": document["trace_id"],
                "otlp": document["otlp"],
                "lineage_id": lineage.lineage_id,
                "journal_snapshot": {
                    "path": snapshot_relative,
                    "sha256": sha256_file(snapshot),
                    "bytes": snapshot.stat().st_size,
                },
            },
            indent=2,
        )
    )
    return 0


def diff_command(args: argparse.Namespace) -> int:
    journal = Path(args.journal).resolve()
    arms = {
        mode: [record.as_dict() for record in records_for(journal, args.run_id, mode)]
        for mode in ("aware", "control")
    }

    def counts(spans: list[Document]) -> dict[str, int]:
        effects = [
            span for span in spans if span["classification"] in ("executed", "resolved")
        ]
        return {
            "executed": sum(span["classification"] == "executed" for span in effects),
            "resolved": sum(span["classification"] == "resolved" for span in effects),
            "effect_spans": len(effects),
            "total_spans": len(spans),
        }

    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "journal": str(journal),
                "journal_sha256": sha256_file(journal),
                "aware": counts(arms["aware"]),
                "control": counts(arms["control"]),
            },
            indent=2,
        )
    )
    return 0


def signoz_health(base_url: str) -> Document:
    safe_base_url = local_base_url(base_url)
    try:
        with urllib.request.urlopen(
            f"{safe_base_url}/api/v1/health", timeout=10
        ) as response:
            return {
                "base_url": safe_base_url,
                "reachable": response.status == 200,
                "http_status": response.status,
            }
    except (OSError, urllib.error.URLError) as error:
        return {"base_url": safe_base_url, "reachable": False, "error": str(error)}


def doctor_command(args: argparse.Namespace) -> int:
    project_root = cast(Path, args.project_root)
    required = (
        "deploy/IMAGE-PINS.json",
        "deploy/HISTOGRAM-PINS.json",
        "uv.lock",
        "web/app/globals.css",
    )
    files = {
        relative: {
            "exists": (project_root / relative).is_file(),
            "sha256": sha256_file(project_root / relative)
            if (project_root / relative).is_file()
            else None,
        }
        for relative in required
    }
    try:
        lineage = validate_current_lineage(project_root)
        lineage_status: Document = {
            "valid": True,
            "lineage_id": lineage.lineage_id,
            "manifest": lineage.manifest.as_dict(),
            "checked_artifacts": lineage.checked_artifacts,
        }
    except LineageValidationError as error:
        lineage_status = {"valid": False, "error": error.as_dict()}
    health = signoz_health(args.signoz_url)
    passed = (
        all(item["exists"] for item in files.values())
        and lineage_status["valid"] is True
        and (health["reachable"] or not args.require_signoz)
    )
    print(
        json.dumps(
            {
                "project_root": str(project_root),
                "files": files,
                "lineage": lineage_status,
                "signoz": health,
                "require_signoz": args.require_signoz,
                "verdict": "PASS" if passed else "FAIL",
            },
            indent=2,
        )
    )
    return 0 if passed else 1


def add_projection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--journal", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", choices=("aware", "control"), default="aware")
    parser.add_argument("--output", required=True)
    parser.add_argument("--otlp-endpoint")
    parser.set_defaults(handler=project_command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay-otel", description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        help="relay-otel project root; defaults to the current directory after marker checks",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser(
        "project", help="project one lineage-bound trace from an immutable DB snapshot"
    )
    add_projection_arguments(project)
    export = commands.add_parser(
        "export", help="alias for project, with optional loopback OTLP delivery"
    )
    add_projection_arguments(export)

    diff = commands.add_parser("diff", help="compare both projections of one journal")
    diff.add_argument("--journal", required=True)
    diff.add_argument("--run-id", required=True)
    diff.set_defaults(handler=diff_command)

    doctor = commands.add_parser(
        "doctor", help="validate release lineage, local files, and SigNoz health"
    )
    doctor.add_argument("--require-signoz", action="store_true")
    doctor.add_argument("--signoz-url", default="http://127.0.0.1:8080")
    doctor.set_defaults(handler=doctor_command)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    handler = cast(Callable[[argparse.Namespace], int], args.handler)
    try:
        args.project_root = resolve_project_root(args.project_root)
        return handler(args)
    except LineageValidationError as error:
        print(json.dumps(error.as_dict(), separators=(",", ":")), file=sys.stderr)
        return 78
    except ValueError as error:
        print(json.dumps({"error": str(error)}, separators=(",", ":")), file=sys.stderr)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
