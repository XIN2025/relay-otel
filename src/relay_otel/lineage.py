"""Fail-closed evidence-lineage validation shared by CLI and product entrypoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relay.types import Document

CURRENT_POINTER = Path("receipts/current-lineage.json")
HEX_SHA256_LENGTH = 64
REQUIRED_PHASES = ("01-runtime", "02-integration", "03-publication")
REQUIRED_MANIFEST_EVIDENCE = frozenset({"web/data/featured.json"})
REQUIRED_PHASE_CRITERIA = {
    "01-runtime": frozenset(
        {
            "frozen_uv_sync",
            "exact_uv_lock",
            "ruff",
            "strict_mypy",
            "compileall",
            "frozen_environment_matches_project_venv",
            "bounded_runtime_correctness",
            "real_hard_exit_and_fresh_resume",
        }
    ),
    "02-integration": frozenset(
        {
            "registered_product_document",
            "one_provider_attempt_and_one_durable_refund",
            "aware_and_control_five_spans",
            "immutable_staging_before_acknowledged_otlp",
            "exact_duplicate_free_signoz_queryback",
            "retained_sanitized_source_facts",
            "compose_renders",
            "loopback_only_published_ports",
            "exact_stack_inventory_and_image_pins",
            "required_containers_healthy",
            "mounted_histogram_helper_matches_pin",
            "collector_and_signoz_healthy",
            "service_account_identity_validated",
        }
    ),
    "03-publication": frozenset(
        {
            "featured_v2_proof",
            "frozen_web_install",
            "locked_web_toolchain",
            "pnpm_production_audit",
            "python_dependency_audit",
            "python_source_security_scan",
            "web_typecheck_lint_format_build",
            "operator_observed_responsive_browser_routes",
            "operator_observed_origin_and_unavailable_fail_closed",
            "operator_observed_keyboard_and_reduced_motion",
            "package_readme_source_and_installed_cli",
            "publication_scope_and_v2_vocabulary",
        }
    ),
}


class LineageValidationError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, details: Document | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self) -> Document:
        return {
            "code": self.code,
            "message": str(self),
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    path: str
    sha256: str
    bytes: int

    @classmethod
    def from_document(cls, value: object, *, location: str) -> ArtifactReference:
        if not isinstance(value, dict):
            raise LineageValidationError(
                "LINEAGE_SCHEMA_INVALID",
                f"{location} must be an artifact reference object",
            )
        path = value.get("path")
        digest = value.get("sha256")
        byte_count = value.get("bytes")
        if not isinstance(path, str) or not path:
            raise LineageValidationError(
                "LINEAGE_SCHEMA_INVALID", f"{location}.path must be a non-empty string"
            )
        if (
            not isinstance(digest, str)
            or len(digest) != HEX_SHA256_LENGTH
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            raise LineageValidationError(
                "LINEAGE_SCHEMA_INVALID",
                f"{location}.sha256 must contain 64 hexadecimal characters",
            )
        if (
            not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 0
        ):
            raise LineageValidationError(
                "LINEAGE_SCHEMA_INVALID",
                f"{location}.bytes must be a non-negative integer",
            )
        return cls(path=path, sha256=digest.lower(), bytes=byte_count)

    def as_dict(self) -> Document:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}


@dataclass(frozen=True, slots=True)
class ValidatedInputs:
    lineage_id: str
    inputs: ArtifactReference
    checked_artifacts: int

    def provenance(self) -> Document:
        return {
            "lineageId": self.lineage_id,
            "lineageManifestPath": None,
            "lineageManifestSha256": None,
            "lineageInputsPath": self.inputs.path,
            "lineageInputsSha256": self.inputs.sha256,
            "lineageCheckedArtifacts": self.checked_artifacts,
            "lineageCandidate": True,
            "phaseReceipts": {},
        }


@dataclass(frozen=True, slots=True)
class ValidatedLineage:
    lineage_id: str
    manifest: ArtifactReference
    inputs: ArtifactReference
    phase_receipts: dict[str, ArtifactReference]
    checked_artifacts: int

    def provenance(self) -> Document:
        return {
            "lineageId": self.lineage_id,
            "lineageManifestPath": self.manifest.path,
            "lineageManifestSha256": self.manifest.sha256,
            "lineageInputsPath": self.inputs.path,
            "lineageInputsSha256": self.inputs.sha256,
            "lineageCheckedArtifacts": self.checked_artifacts,
            "phaseReceipts": {
                phase: reference.as_dict()
                for phase, reference in sorted(self.phase_receipts.items())
            },
        }


@dataclass(frozen=True, slots=True)
class ValidatedCandidate:
    inputs: ValidatedInputs
    phase_receipts: tuple[ArtifactReference, ...]
    checked_artifacts: int


def _is_link_like(path: Path) -> bool:

    return path.is_symlink() or path.is_junction()


def _reject_link_components(
    project_root: Path, relative: Path, *, display_path: str
) -> None:
    current = project_root.resolve()
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise LineageValidationError(
                "LINEAGE_PATH_NON_CANONICAL",
                "lineage artifact paths must use canonical project-relative components",
                details={"path": display_path},
            )
        current /= part
        if _is_link_like(current):
            raise LineageValidationError(
                "LINEAGE_SYMLINK_REJECTED",
                "lineage artifacts must not traverse symbolic links or junctions",
                details={"path": display_path, "link_component": str(current)},
            )


def select_lineage_artifacts(project_root: Path) -> dict[str, list[Path]]:

    groups: dict[str, list[Path]] = {
        "source": [],
        "web": [],
        "deployment": [],
        "vendor": [],
        "configuration": [],
        "publication": [],
    }
    ignored_parts = {
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
        "coverage",
        "dist",
        "node_modules",
        "out",
    }
    ignored_web_files = {
        Path("web/.eslintcache"),
        Path("web/data/featured.json"),
        Path("web/next-env.d.ts"),
        Path("web/npm-debug.log"),
        Path("web/pnpm-debug.log"),
        Path("web/yarn-error.log"),
    }
    selected_roots = {
        "src": "source",
        "scripts": "source",
        "verify": "source",
        "web": "web",
        "deploy": "deployment",
        "vendor": "vendor",
    }
    for root_name, group_name in selected_roots.items():
        root = project_root / root_name
        if not root.is_dir():
            continue
        if _is_link_like(root):
            raise LineageValidationError(
                "LINEAGE_SYMLINK_REJECTED",
                "canonical input roots must not be symbolic links",
                details={"path": root.relative_to(project_root).as_posix()},
            )
        for directory, directory_names, file_names in os.walk(root):
            for name in directory_names:
                candidate = Path(directory) / name
                if _is_link_like(candidate):
                    raise LineageValidationError(
                        "LINEAGE_SYMLINK_REJECTED",
                        "canonical input directories must not be symbolic links",
                        details={
                            "path": candidate.relative_to(project_root).as_posix()
                        },
                    )
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in ignored_parts and not name.endswith(".egg-info")
            )
            for file_name in sorted(file_names):
                path = Path(directory) / file_name
                if _is_link_like(path):
                    raise LineageValidationError(
                        "LINEAGE_SYMLINK_REJECTED",
                        "canonical input files must not be symbolic links",
                        details={"path": path.relative_to(project_root).as_posix()},
                    )
                relative_parts = path.relative_to(project_root).parts
                if relative_parts[:3] == ("web", "data", "generated"):
                    continue
                relative_path = path.relative_to(project_root)
                if (
                    relative_path in ignored_web_files
                    or path.suffix == ".tsbuildinfo"
                    or (group_name == "web" and file_name.endswith("-debug.log"))
                ):
                    continue
                groups[group_name].append(path)

    for name in (
        ".env.example",
        ".gitignore",
        ".node-version",
        ".python-version",
        "pyproject.toml",
        "uv.lock",
    ):
        path = project_root / name
        if path.is_file():
            groups["configuration"].append(path)
    for path in project_root.glob("*.md"):
        if path.is_file():
            groups["publication"].append(path)

    return {
        group: sorted(paths, key=lambda path: path.relative_to(project_root).as_posix())
        for group, paths in groups.items()
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_reference(project_root: Path, path: Path) -> ArtifactReference:
    lexical_root = project_root.absolute()
    lexical_path = (path if path.is_absolute() else project_root / path).absolute()
    try:
        relative_path = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise LineageValidationError(
            "LINEAGE_PATH_OUTSIDE_PROJECT",
            "lineage artifacts must stay inside the project root",
            details={"path": str(path)},
        ) from exc
    relative = relative_path.as_posix()
    _reject_link_components(project_root, relative_path, display_path=relative)
    resolved_root = project_root.resolve()
    resolved_path = lexical_path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise LineageValidationError(
            "LINEAGE_PATH_OUTSIDE_PROJECT",
            "lineage artifacts must stay inside the project root",
            details={"path": str(path)},
        ) from exc
    if not resolved_path.is_file():
        raise LineageValidationError(
            "LINEAGE_ARTIFACT_MISSING",
            "cannot reference a missing lineage artifact",
            details={"path": relative},
        )
    return ArtifactReference(
        path=relative,
        sha256=sha256_file(resolved_path),
        bytes=resolved_path.stat().st_size,
    )


def artifact_reference(project_root: Path, path: Path) -> ArtifactReference:

    try:
        return _artifact_reference(project_root, path)
    except LineageValidationError:
        raise
    except OSError as exc:
        raise LineageValidationError(
            "LINEAGE_ARTIFACT_IO_ERROR",
            "a lineage artifact could not be read safely",
            details={"path": str(path), "error_type": type(exc).__name__},
        ) from exc


def write_json_exclusive(path: Path, payload: Document) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")
    temporary = path.parent / (f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise LineageValidationError(
            "LINEAGE_ARTIFACT_EXISTS",
            "could not reserve a temporary lineage artifact",
            details={"path": str(temporary)},
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise LineageValidationError(
                "LINEAGE_ARTIFACT_EXISTS",
                "refusing to replace an existing lineage artifact",
                details={"path": str(path)},
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


def _read_json(path: Path, *, location: str) -> Document:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LineageValidationError(
            "LINEAGE_ARTIFACT_MISSING",
            f"{location} does not exist",
            details={"path": str(path)},
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LineageValidationError(
            "LINEAGE_JSON_INVALID",
            f"{location} is not valid UTF-8 JSON",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(value, dict):
        raise LineageValidationError(
            "LINEAGE_SCHEMA_INVALID", f"{location} must contain a JSON object"
        )
    return value


def _resolved_artifact_path(project_root: Path, reference: ArtifactReference) -> Path:
    candidate = Path(reference.path)
    if candidate.is_absolute() or candidate.anchor:
        raise LineageValidationError(
            "LINEAGE_PATH_ABSOLUTE",
            "lineage artifact paths must be project-relative",
            details={"path": reference.path},
        )
    if candidate.as_posix() != reference.path or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise LineageValidationError(
            "LINEAGE_PATH_NON_CANONICAL",
            "lineage artifact paths must use canonical project-relative components",
            details={"path": reference.path},
        )
    _reject_link_components(project_root, candidate, display_path=reference.path)
    resolved_root = project_root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise LineageValidationError(
            "LINEAGE_PATH_OUTSIDE_PROJECT",
            "lineage artifact resolved outside the project root",
            details={"path": reference.path},
        ) from exc
    return resolved


def _validate_artifact(
    project_root: Path,
    reference: ArtifactReference,
    checked: dict[str, tuple[str, int]],
) -> Path:
    existing = checked.get(reference.path)
    if existing is not None:
        existing_digest, existing_bytes = existing
        if existing_digest != reference.sha256 or existing_bytes != reference.bytes:
            raise LineageValidationError(
                "LINEAGE_REFERENCE_CONFLICT",
                "one lineage path has conflicting expected hash or size metadata",
                details={
                    "path": reference.path,
                    "first_sha256": existing_digest,
                    "second_sha256": reference.sha256,
                    "first_bytes": existing_bytes,
                    "second_bytes": reference.bytes,
                },
            )
        return _resolved_artifact_path(project_root, reference)

    try:
        path = _resolved_artifact_path(project_root, reference)
        if not path.is_file():
            raise LineageValidationError(
                "LINEAGE_ARTIFACT_MISSING",
                "a lineage artifact is missing",
                details={"path": reference.path},
            )
        actual_bytes = path.stat().st_size
        actual_digest = sha256_file(path)
    except LineageValidationError:
        raise
    except OSError as exc:
        raise LineageValidationError(
            "LINEAGE_ARTIFACT_IO_ERROR",
            "a lineage artifact could not be read safely",
            details={
                "path": reference.path,
                "error_type": type(exc).__name__,
            },
        ) from exc
    if actual_bytes != reference.bytes:
        raise LineageValidationError(
            "LINEAGE_SIZE_MISMATCH",
            "a lineage artifact size changed",
            details={
                "path": reference.path,
                "expected_bytes": reference.bytes,
                "actual_bytes": actual_bytes,
            },
        )
    if actual_digest != reference.sha256:
        raise LineageValidationError(
            "LINEAGE_HASH_MISMATCH",
            "a lineage artifact hash changed",
            details={
                "path": reference.path,
                "expected_sha256": reference.sha256,
                "actual_sha256": actual_digest,
            },
        )
    checked[reference.path] = (reference.sha256, reference.bytes)
    return path


def _iter_artifact_references(
    value: object, *, location: str
) -> Iterator[ArtifactReference]:
    if isinstance(value, dict):
        if "sha256" in value or ("path" in value and "bytes" in value):
            if "path" not in value or "sha256" not in value:
                raise LineageValidationError(
                    "LINEAGE_REFERENCE_INCOMPLETE",
                    "artifact references must pair path with sha256",
                    details={"location": location},
                )
            yield ArtifactReference.from_document(value, location=location)
            return
        for key, child in value.items():
            yield from _iter_artifact_references(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_artifact_references(child, location=f"{location}[{index}]")


def _require_lineage_identity(
    document: Document, lineage_id: str, *, location: str
) -> None:
    actual = document.get("lineage_id")
    if actual != lineage_id:
        raise LineageValidationError(
            "LINEAGE_ID_MISMATCH",
            f"{location} belongs to a different evidence lineage",
            details={"expected": lineage_id, "actual": actual},
        )


def _validate_frozen_inputs(
    project_root: Path,
    inputs_reference: ArtifactReference,
    checked: dict[str, tuple[str, int]],
) -> ValidatedInputs:
    inputs_path = _validate_artifact(project_root, inputs_reference, checked)
    inputs = _read_json(inputs_path, location="lineage inputs")
    lineage_id = inputs.get("lineage_id")
    if not isinstance(lineage_id, str) or not lineage_id:
        raise LineageValidationError(
            "LINEAGE_SCHEMA_INVALID", "lineage inputs require lineage_id"
        )
    if inputs.get("schema_version") != 1 or inputs.get("status") != "frozen_inputs":
        raise LineageValidationError(
            "LINEAGE_INPUTS_INVALID",
            "lineage inputs must use schema 1 and status frozen_inputs",
        )
    artifact_groups = inputs.get("artifacts")
    if not isinstance(artifact_groups, dict) or not artifact_groups:
        raise LineageValidationError(
            "LINEAGE_INPUTS_INVALID", "lineage inputs require artifact groups"
        )

    frozen_paths: dict[str, set[str]] = {}
    for group_name, references in artifact_groups.items():
        if not isinstance(group_name, str) or not isinstance(references, list):
            raise LineageValidationError(
                "LINEAGE_INPUTS_INVALID",
                "each lineage input group must be a list",
                details={"group": str(group_name)},
            )
        group_paths: set[str] = set()
        for index, value in enumerate(references):
            reference = ArtifactReference.from_document(
                value, location=f"inputs.artifacts.{group_name}[{index}]"
            )
            if reference.path in group_paths:
                raise LineageValidationError(
                    "LINEAGE_INPUTS_INVALID",
                    "one frozen input group contains a duplicate path",
                    details={"group": group_name, "path": reference.path},
                )
            group_paths.add(reference.path)
            _validate_artifact(project_root, reference, checked)
        frozen_paths[group_name] = group_paths

    current_selection = select_lineage_artifacts(project_root)
    current_paths = {
        group: {path.relative_to(project_root).as_posix() for path in paths}
        for group, paths in current_selection.items()
    }
    if frozen_paths != current_paths:
        missing = {
            group: sorted(paths - frozen_paths.get(group, set()))
            for group, paths in current_paths.items()
            if paths - frozen_paths.get(group, set())
        }
        unexpected = {
            group: sorted(paths - current_paths.get(group, set()))
            for group, paths in frozen_paths.items()
            if paths - current_paths.get(group, set())
        }
        raise LineageValidationError(
            "LINEAGE_INPUT_SET_MISMATCH",
            "the current canonical artifact set differs from the frozen input set",
            details={
                "missing_from_inputs": missing,
                "unexpected_in_inputs": unexpected,
            },
        )
    return ValidatedInputs(
        lineage_id=lineage_id,
        inputs=inputs_reference,
        checked_artifacts=len(checked),
    )


def validate_frozen_inputs(project_root: Path, inputs_path: Path) -> ValidatedInputs:

    try:
        reference = artifact_reference(project_root, inputs_path)
        return _validate_frozen_inputs(project_root, reference, {})
    except LineageValidationError:
        raise
    except OSError as exc:
        raise LineageValidationError(
            "LINEAGE_ARTIFACT_IO_ERROR",
            "the frozen lineage inputs could not be read safely",
            details={"error_type": type(exc).__name__},
        ) from exc


def _validate_phase_receipts(
    project_root: Path,
    *,
    lineage_id: str,
    inputs_reference: ArtifactReference,
    receipt_values: list[object],
    checked: dict[str, tuple[str, int]],
    require_complete: bool = False,
) -> dict[str, ArtifactReference]:
    if not receipt_values:
        raise LineageValidationError(
            "LINEAGE_RECEIPTS_MISSING",
            "a current lineage requires at least one phase receipt",
        )
    phase_receipts: dict[str, ArtifactReference] = {}
    previous: ArtifactReference | None = None
    for index, value in enumerate(receipt_values):
        reference = ArtifactReference.from_document(
            value, location=f"phase_receipts[{index}]"
        )
        receipt_path = _validate_artifact(project_root, reference, checked)
        receipt = _read_json(receipt_path, location=f"phase receipt {reference.path}")
        if receipt.get("schema_version") != 1:
            raise LineageValidationError(
                "LINEAGE_RECEIPT_SCHEMA_UNSUPPORTED",
                "phase receipts must use schema_version 1",
                details={"path": reference.path},
            )
        _require_lineage_identity(receipt, lineage_id, location=reference.path)
        if receipt.get("lineage_inputs_sha256") != inputs_reference.sha256:
            raise LineageValidationError(
                "LINEAGE_RECEIPT_INPUT_MISMATCH",
                "phase receipt was not produced from the selected frozen inputs",
                details={"path": reference.path},
            )
        if receipt.get("verdict") != "PASS":
            raise LineageValidationError(
                "LINEAGE_RECEIPT_NOT_PASS",
                "a current phase receipt is not PASS",
                details={"path": reference.path, "verdict": receipt.get("verdict")},
            )
        phase_key = receipt.get("phase")
        if not isinstance(phase_key, str) or phase_key not in REQUIRED_PHASE_CRITERIA:
            raise LineageValidationError(
                "LINEAGE_RECEIPT_PHASE_INVALID",
                "each phase receipt requires one registered v2 phase",
                details={"path": reference.path},
            )
        criteria = receipt.get("criteria")
        if (
            not isinstance(criteria, dict)
            or not criteria
            or any(value is not True for value in criteria.values())
            or set(criteria) != REQUIRED_PHASE_CRITERIA[phase_key]
        ):
            raise LineageValidationError(
                "LINEAGE_RECEIPT_CRITERIA_INVALID",
                "a PASS phase receipt requires the exact registered all-true criteria",
                details={
                    "path": reference.path,
                    "expected": sorted(REQUIRED_PHASE_CRITERIA[phase_key]),
                    "actual": sorted(criteria) if isinstance(criteria, dict) else None,
                },
            )
        if "evidence" not in receipt:
            raise LineageValidationError(
                "LINEAGE_RECEIPT_EVIDENCE_MISSING",
                "each phase receipt must bind its evidence artifact",
                details={"path": reference.path},
            )
        if "previous_phase_receipt" not in receipt:
            raise LineageValidationError(
                "LINEAGE_RECEIPT_CHAIN_INVALID",
                "each phase receipt must explicitly declare its predecessor or null",
                details={"path": reference.path},
            )
        previous_value = receipt["previous_phase_receipt"]
        if previous is None:
            if previous_value is not None:
                raise LineageValidationError(
                    "LINEAGE_RECEIPT_CHAIN_INVALID",
                    "the first phase receipt must not declare a predecessor",
                    details={"path": reference.path},
                )
        else:
            predecessor = ArtifactReference.from_document(
                previous_value,
                location=f"receipt[{reference.path}].previous_phase_receipt",
            )
            if (
                predecessor.path != previous.path
                or predecessor.sha256 != previous.sha256
                or predecessor.bytes != previous.bytes
            ):
                raise LineageValidationError(
                    "LINEAGE_RECEIPT_CHAIN_INVALID",
                    "phase receipt predecessor does not match the supplied order",
                    details={"path": reference.path},
                )
        for nested in _iter_artifact_references(
            receipt, location=f"receipt[{reference.path}]"
        ):
            _validate_artifact(project_root, nested, checked)
        evidence_reference = ArtifactReference.from_document(
            receipt["evidence"], location=f"receipt[{reference.path}].evidence"
        )
        evidence_path = _validate_artifact(project_root, evidence_reference, checked)
        evidence = _read_json(
            evidence_path, location=f"phase evidence {evidence_reference.path}"
        )
        if (
            evidence.get("schema_version") != 1
            or evidence.get("phase") != receipt.get("phase")
            or evidence.get("lineage_id") != lineage_id
            or evidence.get("lineage_inputs_sha256") != inputs_reference.sha256
            or evidence.get("verdict") != "PASS"
            or evidence.get("criteria") != criteria
        ):
            raise LineageValidationError(
                "LINEAGE_PHASE_EVIDENCE_MISMATCH",
                "phase evidence metadata does not match its receipt",
                details={"path": evidence_reference.path},
            )
        for nested in _iter_artifact_references(
            evidence, location=f"evidence[{evidence_reference.path}]"
        ):
            _validate_artifact(project_root, nested, checked)
        if phase_key in phase_receipts:
            raise LineageValidationError(
                "LINEAGE_RECEIPT_DUPLICATE_PHASE",
                "a lineage contains duplicate phase receipts",
                details={"phase": phase_key},
            )
        phase_receipts[phase_key] = reference
        previous = reference
    actual_phases = tuple(phase_receipts)
    expected_phases = (
        REQUIRED_PHASES if require_complete else REQUIRED_PHASES[: len(actual_phases)]
    )
    if actual_phases != expected_phases:
        raise LineageValidationError(
            "LINEAGE_PHASE_POLICY_MISMATCH",
            "phase receipts do not match the required v2 order",
            details={"expected": list(expected_phases), "actual": list(actual_phases)},
        )
    return phase_receipts


def validate_candidate_receipts(
    project_root: Path,
    inputs_path: Path,
    receipt_paths: list[Path],
    *,
    require_complete: bool = False,
) -> ValidatedCandidate:

    try:
        checked: dict[str, tuple[str, int]] = {}
        inputs_reference = artifact_reference(project_root, inputs_path)
        inputs = _validate_frozen_inputs(project_root, inputs_reference, checked)
        references = tuple(
            artifact_reference(project_root, path) for path in receipt_paths
        )
        _validate_phase_receipts(
            project_root,
            lineage_id=inputs.lineage_id,
            inputs_reference=inputs_reference,
            receipt_values=[reference.as_dict() for reference in references],
            checked=checked,
            require_complete=require_complete,
        )
        return ValidatedCandidate(
            inputs=inputs,
            phase_receipts=references,
            checked_artifacts=len(checked),
        )
    except LineageValidationError:
        raise
    except OSError as exc:
        raise LineageValidationError(
            "LINEAGE_ARTIFACT_IO_ERROR",
            "the candidate lineage could not be read safely",
            details={"error_type": type(exc).__name__},
        ) from exc


def validate_lineage_manifest(
    project_root: Path,
    manifest_reference: ArtifactReference,
    *,
    require_current: bool = True,
) -> ValidatedLineage:

    checked: dict[str, tuple[str, int]] = {}
    manifest_path = _validate_artifact(project_root, manifest_reference, checked)
    manifest = _read_json(manifest_path, location="lineage manifest")
    if manifest.get("schema_version") != 1:
        raise LineageValidationError(
            "LINEAGE_SCHEMA_UNSUPPORTED", "lineage manifest schema_version must be 1"
        )
    lineage_id = manifest.get("lineage_id")
    if not isinstance(lineage_id, str) or not lineage_id:
        raise LineageValidationError(
            "LINEAGE_SCHEMA_INVALID", "lineage manifest requires lineage_id"
        )
    if require_current and manifest.get("status") != "current":
        raise LineageValidationError(
            "LINEAGE_NOT_CURRENT",
            "the selected evidence lineage is not finalized for current execution",
            details={"lineage_id": lineage_id, "status": manifest.get("status")},
        )

    inputs_reference = ArtifactReference.from_document(
        manifest.get("inputs"), location="manifest.inputs"
    )
    validated_inputs = _validate_frozen_inputs(project_root, inputs_reference, checked)
    if validated_inputs.lineage_id != lineage_id:
        raise LineageValidationError(
            "LINEAGE_ID_MISMATCH",
            "lineage inputs belong to a different evidence lineage",
            details={"expected": lineage_id, "actual": validated_inputs.lineage_id},
        )

    receipt_values = manifest.get("phase_receipts")
    if not isinstance(receipt_values, list):
        raise LineageValidationError(
            "LINEAGE_SCHEMA_INVALID", "manifest.phase_receipts must be a list"
        )
    phase_receipts = _validate_phase_receipts(
        project_root,
        lineage_id=lineage_id,
        inputs_reference=inputs_reference,
        receipt_values=receipt_values,
        checked=checked,
        require_complete=True,
    )

    for field in ("evidence", "publication"):
        values = manifest.get(field, [])
        if not isinstance(values, list) or not values:
            raise LineageValidationError(
                "LINEAGE_SCHEMA_INVALID",
                f"manifest.{field} must be a non-empty list",
            )
        field_paths: set[str] = set()
        for index, value in enumerate(values):
            reference = ArtifactReference.from_document(
                value, location=f"manifest.{field}[{index}]"
            )
            if reference.path in field_paths:
                raise LineageValidationError(
                    "LINEAGE_MANIFEST_DUPLICATE_ARTIFACT",
                    f"manifest.{field} contains a duplicate path",
                    details={"path": reference.path},
                )
            field_paths.add(reference.path)
            _validate_artifact(project_root, reference, checked)
        if field == "evidence" and not REQUIRED_MANIFEST_EVIDENCE.issubset(field_paths):
            raise LineageValidationError(
                "LINEAGE_REQUIRED_EVIDENCE_MISSING",
                "manifest evidence omits required product artifacts",
                details={
                    "required": sorted(REQUIRED_MANIFEST_EVIDENCE),
                    "actual": sorted(field_paths),
                },
            )
        if field == "publication":
            expected_publication = {
                path.relative_to(project_root).as_posix()
                for path in select_lineage_artifacts(project_root)["publication"]
            }
            if field_paths != expected_publication:
                raise LineageValidationError(
                    "LINEAGE_PUBLICATION_SET_MISMATCH",
                    "manifest publication must bind every canonical top-level document",
                    details={
                        "missing": sorted(expected_publication - field_paths),
                        "unexpected": sorted(field_paths - expected_publication),
                    },
                )

    return ValidatedLineage(
        lineage_id=lineage_id,
        manifest=manifest_reference,
        inputs=inputs_reference,
        phase_receipts=phase_receipts,
        checked_artifacts=len(checked),
    )


def _validate_current_lineage(project_root: Path) -> ValidatedLineage:
    pointer_path = project_root / CURRENT_POINTER
    _reject_link_components(
        project_root, CURRENT_POINTER, display_path=CURRENT_POINTER.as_posix()
    )
    if not pointer_path.is_file():
        raise LineageValidationError(
            "LINEAGE_POINTER_MISSING",
            "no current evidence lineage is finalized; execution is disabled",
            details={"path": CURRENT_POINTER.as_posix()},
        )
    pointer = _read_json(pointer_path, location="current-lineage pointer")
    if pointer.get("schema_version") != 1:
        raise LineageValidationError(
            "LINEAGE_SCHEMA_UNSUPPORTED", "current-lineage schema_version must be 1"
        )
    lineage_id = pointer.get("lineage_id")
    if not isinstance(lineage_id, str) or not lineage_id:
        raise LineageValidationError(
            "LINEAGE_SCHEMA_INVALID", "current-lineage requires lineage_id"
        )
    manifest_reference = ArtifactReference.from_document(
        pointer.get("manifest"), location="current-lineage.manifest"
    )
    validated = validate_lineage_manifest(project_root, manifest_reference)
    if validated.lineage_id != lineage_id:
        raise LineageValidationError(
            "LINEAGE_ID_MISMATCH",
            "current-lineage and its manifest name different lineages",
            details={"pointer": lineage_id, "manifest": validated.lineage_id},
        )
    return validated


def validate_current_lineage(project_root: Path) -> ValidatedLineage:

    try:
        return _validate_current_lineage(project_root)
    except LineageValidationError:
        raise
    except OSError as exc:
        raise LineageValidationError(
            "LINEAGE_ARTIFACT_IO_ERROR",
            "the current lineage could not be read safely",
            details={"error_type": type(exc).__name__},
        ) from exc


def _status_document(project_root: Path) -> tuple[Document, int]:
    try:
        validated = validate_current_lineage(project_root)
    except LineageValidationError as exc:
        return (
            {
                "ready": False,
                "lineage_id": None,
                "manifest_sha256": None,
                "reason_code": exc.code.lower(),
                "detail": str(exc),
            },
            78,
        )
    return (
        {
            "ready": True,
            "lineage_id": validated.lineage_id,
            "manifest_sha256": validated.manifest.sha256,
            "reason_code": None,
            "detail": "current evidence lineage validated",
        },
        0,
    )


def main() -> int:

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    status, exit_code = _status_document(args.project_root)
    print(json.dumps(status, separators=(",", ":"), sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
