"""Validate, finalize, and exclusively activate one frozen evidence lineage."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
VERIFY = PROJECT_ROOT / "verify"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(VERIFY) not in sys.path:
    sys.path.insert(0, str(VERIFY))

from relay_otel.lineage import (
    CURRENT_POINTER,
    REQUIRED_MANIFEST_EVIDENCE,
    LineageValidationError,
    artifact_reference,
    select_lineage_artifacts,
    validate_candidate_receipts,
    validate_current_lineage,
    validate_lineage_manifest,
    write_json_exclusive,
)
from release_audit import semantic_audit


def _project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--phase-receipt", type=Path, action="append", required=True)
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--publication", type=Path, action="append", default=[])
    args = parser.parse_args()

    inputs_path = _project_path(args.inputs)
    receipt_paths = [_project_path(path) for path in args.phase_receipt]
    evidence_paths = [_project_path(path) for path in args.evidence]
    publication_paths = [_project_path(path) for path in args.publication]
    candidate = validate_candidate_receipts(
        PROJECT_ROOT, inputs_path, receipt_paths, require_complete=True
    )
    lineage_id = candidate.inputs.lineage_id
    lineage_dir = PROJECT_ROOT / "receipts" / "lineages" / lineage_id
    expected_inputs = lineage_dir / "inputs.json"
    if inputs_path.resolve() != expected_inputs.resolve():
        raise LineageValidationError(
            "LINEAGE_INPUTS_LOCATION_INVALID",
            "frozen inputs must use receipts/lineages/<lineage_id>/inputs.json",
            details={"expected": str(expected_inputs), "actual": str(inputs_path)},
        )

    manifest_path = lineage_dir / "manifest.json"
    pointer_path = PROJECT_ROOT / CURRENT_POINTER
    if manifest_path.exists() or pointer_path.exists():
        raise LineageValidationError(
            "LINEAGE_ACTIVATION_COLLISION",
            "refusing to replace an existing manifest or current-lineage pointer",
            details={
                "manifest_exists": manifest_path.exists(),
                "pointer_exists": pointer_path.exists(),
            },
        )

    evidence = [
        artifact_reference(PROJECT_ROOT, path).as_dict() for path in evidence_paths
    ]
    publication = [
        artifact_reference(PROJECT_ROOT, path).as_dict() for path in publication_paths
    ]
    evidence_path_set = {str(reference["path"]) for reference in evidence}
    if not REQUIRED_MANIFEST_EVIDENCE.issubset(evidence_path_set):
        raise LineageValidationError(
            "LINEAGE_REQUIRED_EVIDENCE_MISSING",
            "finalization requires the retained v2 product evidence",
            details={
                "required": sorted(REQUIRED_MANIFEST_EVIDENCE),
                "actual": sorted(evidence_path_set),
            },
        )
    publication_path_set = {str(reference["path"]) for reference in publication}
    expected_publication = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in select_lineage_artifacts(PROJECT_ROOT)["publication"]
    }
    if publication_path_set != expected_publication:
        raise LineageValidationError(
            "LINEAGE_PUBLICATION_SET_MISMATCH",
            "finalization must bind every canonical top-level document",
            details={
                "missing": sorted(expected_publication - publication_path_set),
                "unexpected": sorted(publication_path_set - expected_publication),
            },
        )
    manifest = {
        "schema_version": 1,
        "lineage_id": lineage_id,
        "status": "current",
        "finalized_at": datetime.now(UTC).isoformat(),
        "inputs": candidate.inputs.inputs.as_dict(),
        "phase_receipts": [
            reference.as_dict() for reference in candidate.phase_receipts
        ],
        "evidence": evidence,
        "publication": publication,
        "validation": {
            "checked_artifacts_before_finalization": candidate.checked_artifacts
        },
    }
    manifest_created = False
    pointer_created = False
    try:
        write_json_exclusive(manifest_path, manifest)
        manifest_created = True
        manifest_reference = artifact_reference(PROJECT_ROOT, manifest_path)
        validated_manifest = validate_lineage_manifest(PROJECT_ROOT, manifest_reference)
        semantic_audit(validated_manifest)

        pointer = {
            "schema_version": 1,
            "lineage_id": lineage_id,
            "manifest": manifest_reference.as_dict(),
        }
        write_json_exclusive(pointer_path, pointer)
        pointer_created = True
        validated = validate_current_lineage(PROJECT_ROOT)
    except Exception:
        if pointer_created:
            pointer_path.unlink(missing_ok=True)
        if manifest_created:
            manifest_path.unlink(missing_ok=True)
        raise
    print(
        json.dumps(
            {
                "ready": True,
                "lineage_id": validated.lineage_id,
                "manifest": validated.manifest.as_dict(),
                "checked_artifacts": validated.checked_artifacts,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
