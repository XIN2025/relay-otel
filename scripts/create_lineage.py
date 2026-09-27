"""Freeze the canonical project inputs for a new, not-yet-current evidence lineage."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from relay_otel.lineage import (
    artifact_reference,
    select_lineage_artifacts,
    write_json_exclusive,
)

LINEAGE_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?\Z")
ENVIRONMENT_PACKAGES = (
    "bandit",
    "mypy",
    "opentelemetry-api",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-sdk",
    "pip-audit",
    "PyYAML",
    "relay-otel",
    "ruff",
)


def _installed_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lineage_id")
    args = parser.parse_args()
    if LINEAGE_ID.fullmatch(args.lineage_id) is None:
        parser.error(
            "lineage_id must be 1-64 lowercase letters, digits, dots, underscores, or hyphens"
        )
    expected_python = (
        (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    )
    project_interpreters = {
        (PROJECT_ROOT / ".venv" / "Scripts" / "python.exe").absolute(),
        (PROJECT_ROOT / ".venv" / "bin" / "python").absolute(),
    }
    if Path(sys.executable).absolute() not in project_interpreters:
        raise RuntimeError("candidate creation must run with the project .venv Python")
    if platform.python_version() != expected_python:
        raise RuntimeError(
            f"project Python must be exactly {expected_python}, "
            f"got {platform.python_version()}"
        )

    lineage_dir = PROJECT_ROOT / "receipts" / "lineages" / args.lineage_id
    inputs_path = lineage_dir / "inputs.json"
    candidate_path = lineage_dir / "manifest.candidate.json"
    if inputs_path.exists() or candidate_path.exists():
        raise FileExistsError(
            f"refusing to reuse an existing lineage id: {args.lineage_id}"
        )

    groups = select_lineage_artifacts(PROJECT_ROOT)
    artifacts = {
        group: [artifact_reference(PROJECT_ROOT, path).as_dict() for path in paths]
        for group, paths in groups.items()
    }
    inputs = {
        "schema_version": 1,
        "lineage_id": args.lineage_id,
        "status": "frozen_inputs",
        "created_at": datetime.now(UTC).isoformat(),
        "selection_policy": {
            "id": "relay-otel-lineage-v1",
            "implementation": "src/relay_otel/lineage.py:select_lineage_artifacts",
            "generated_exclusions": [
                ".git",
                ".mypy_cache",
                ".next",
                ".open-next",
                ".pytest_cache",
                ".ruff_cache",
                ".turbo",
                ".venv",
                ".vercel",
                "__pycache__",
                "*.egg-info",
                "coverage",
                "dist",
                "node_modules",
                "out",
                "receipts",
                "web/data/featured.json",
                "web/data/generated",
                "web/.eslintcache",
                "web/next-env.d.ts",
                "web/*-debug.log",
                "web/*.tsbuildinfo",
            ],
        },
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "packages": {
                name: _installed_version(name) for name in ENVIRONMENT_PACKAGES
            },
        },
        "artifacts": artifacts,
    }
    write_json_exclusive(inputs_path, inputs)
    inputs_reference = artifact_reference(PROJECT_ROOT, inputs_path)
    candidate = {
        "schema_version": 1,
        "lineage_id": args.lineage_id,
        "status": "candidate",
        "inputs": inputs_reference.as_dict(),
        "phase_receipts": [],
        "evidence": [],
        "publication": [],
        "activation": {
            "required_pointer": "receipts/current-lineage.json",
            "state": "not_current",
        },
    }
    write_json_exclusive(candidate_path, candidate)
    print(
        json.dumps(
            {
                "lineage_id": args.lineage_id,
                "inputs": inputs_reference.as_dict(),
                "candidate_manifest": artifact_reference(
                    PROJECT_ROOT, candidate_path
                ).as_dict(),
                "current": False,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
