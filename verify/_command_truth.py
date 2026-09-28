"""Semantic validation helpers for retained release-command observations.

The phase runner records commands.  The release audit treats those records as
untrusted documents: command identity, working directory, result shape, and
machine-readable output are all re-evaluated here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, cast

Require = Callable[[bool, str], None]


def validate_command(
    value: object,
    *,
    expected: list[str],
    cwd: str,
    location: str,
    require: Require,
) -> dict[str, Any]:
    require(isinstance(value, dict), f"{location} command record is invalid")
    record = cast(dict[str, Any], value)
    require(
        set(record) == {"command", "cwd", "exit_code", "stdout", "stderr"},
        f"{location} command record field set changed",
    )
    require(record.get("command") == expected, f"{location} command vector changed")
    require(record.get("cwd") == cwd, f"{location} working directory changed")
    require(record.get("exit_code") == 0, f"{location} command did not pass")
    require(
        isinstance(record.get("stdout"), str) and isinstance(record.get("stderr"), str),
        f"{location} command output is invalid",
    )
    return record


def json_stdout(record: Mapping[str, Any], *, location: str, require: Require) -> Any:
    stdout = record.get("stdout")
    require(
        isinstance(stdout, str) and stdout.strip() != "", f"{location} output is empty"
    )
    text = cast(str, stdout)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        require(False, f"{location} output is not one JSON document: {exc}")
        raise AssertionError("unreachable") from exc


def last_json_line(
    record: Mapping[str, Any], *, location: str, require: Require
) -> dict[str, Any]:
    stdout = record.get("stdout")
    require(
        isinstance(stdout, str) and stdout.strip() != "", f"{location} output is empty"
    )
    text = cast(str, stdout)
    try:
        value: Any = json.loads(text.strip().splitlines()[-1])
    except json.JSONDecodeError as exc:
        require(False, f"{location} final output line is not JSON: {exc}")
        raise AssertionError("unreachable") from exc
    require(isinstance(value, dict), f"{location} JSON output must be an object")
    return cast(dict[str, Any], value)


def validate_ruff_output(record: Mapping[str, Any], *, require: Require) -> None:
    output = f"{record['stdout']}\n{record['stderr']}"
    require("All checks passed!" in output, "ruff success output is missing")


def validate_mypy_output(record: Mapping[str, Any], *, require: Require) -> None:
    output = f"{record['stdout']}\n{record['stderr']}"
    require(
        re.search(r"Success: no issues found in \d+ source files?", output) is not None,
        "mypy strict success summary is missing",
    )


def validate_runtime_document(value: object, *, require: Require) -> dict[str, Any]:
    require(isinstance(value, dict), "runtime verifier output is not an object")
    document = cast(dict[str, Any], value)
    require(
        set(document) == {"checks", "verdict"}, "runtime verifier field set changed"
    )
    require(document.get("verdict") == "PASS", "runtime verifier is not PASS")
    checks = document.get("checks")
    require(isinstance(checks, dict), "runtime verifier checks are missing")
    expected: dict[str, dict[str, Any]] = {
        "approvalAndStartGuards": {"executions": 1, "status": "finished"},
        "crashResume": {
            "awareSpans": 5,
            "businessExecutions": 1,
            "controlSpans": 5,
            "events": 9,
        },
        "idempotentUnknownOutcome": {
            "businessExecutions": 1,
            "providerCalls": 2,
            "status": "finished",
        },
        "identityMismatch": {
            "providerCalls": 1,
            "sameAttemptProviderCalls": 1,
            "status": "failed",
        },
        "loopActivationIsolation": {"activations": 2, "businessExecutions": 2},
        "routeReplay": {"chooserCallsDuringReplay": 0},
        "skippedCall": {"businessExecutions": 1, "resolutions": 0},
        "unknownOutcome": {"providerCalls": 1, "status": "failed"},
    }
    require(set(checks) == {*expected, "legacyMigration"}, "runtime check set changed")
    for name, expected_value in expected.items():
        require(checks.get(name) == expected_value, f"runtime check changed: {name}")
    legacy = checks.get("legacyMigration")
    require(
        isinstance(legacy, dict)
        and set(legacy) == {"migratedColumns"}
        and isinstance(legacy.get("migratedColumns"), list)
        and {
            "activation_id",
            "at",
            "at_ns",
            "attempt_id",
            "node",
            "payload",
            "run_id",
            "seq",
            "type",
        }.issubset(set(legacy["migratedColumns"])),
        "runtime legacy-migration observation changed",
    )
    return document


def validate_pnpm_audit(value: object, *, require: Require) -> None:
    require(isinstance(value, dict), "pnpm audit output is not an object")
    document = cast(dict[str, Any], value)
    metadata = document.get("metadata")
    vulnerabilities = (
        metadata.get("vulnerabilities") if isinstance(metadata, dict) else None
    )
    require(
        isinstance(vulnerabilities, dict)
        and bool(vulnerabilities)
        and all(
            isinstance(count, int) and count == 0 for count in vulnerabilities.values()
        ),
        "pnpm production audit reports vulnerabilities or malformed totals",
    )


def validate_pip_audit(value: object, *, require: Require) -> None:
    require(isinstance(value, dict), "pip-audit output is not an object")
    document = cast(dict[str, Any], value)
    dependencies = document.get("dependencies")
    require(
        isinstance(dependencies, list) and dependencies,
        "pip-audit dependencies are missing",
    )
    for index, dependency in enumerate(cast(list[object], dependencies)):
        require(
            isinstance(dependency, dict), f"pip-audit dependency {index} is invalid"
        )
        record = cast(dict[str, Any], dependency)
        require(
            isinstance(record.get("name"), str)
            and isinstance(record.get("version"), str)
            and record.get("vulns") == [],
            f"pip-audit dependency {index} is vulnerable or malformed",
        )


def validate_bandit(value: object, *, require: Require) -> None:
    require(isinstance(value, dict), "Bandit output is not an object")
    document = cast(dict[str, Any], value)
    require(document.get("results") == [], "Bandit retained high-severity findings")
    require(document.get("errors") == [], "Bandit retained scan errors")
    metrics = document.get("metrics")
    require(
        isinstance(metrics, dict) and isinstance(metrics.get("_totals"), dict),
        "Bandit metrics are missing",
    )


def validate_eslint(value: object, *, require: Require) -> None:
    require(isinstance(value, list) and value, "ESLint JSON report is missing")
    for index, result in enumerate(cast(list[object], value)):
        require(isinstance(result, dict), f"ESLint result {index} is invalid")
        record = cast(dict[str, Any], result)
        require(
            record.get("errorCount") == 0
            and record.get("warningCount") == 0
            and record.get("messages") == [],
            f"ESLint result {index} contains findings",
        )


def validate_web_text_outputs(
    checks: Mapping[str, Mapping[str, Any]], *, require: Require
) -> None:
    typecheck = f"{checks['typecheck']['stdout']}\n{checks['typecheck']['stderr']}"
    formatting = f"{checks['format']['stdout']}\n{checks['format']['stderr']}"
    build = f"{checks['build']['stdout']}\n{checks['build']['stderr']}"
    require(
        "$ tsc --noEmit" in typecheck, "TypeScript registered command output changed"
    )
    require(
        "All matched files use Prettier code style!" in formatting,
        "Prettier success summary is missing",
    )
    require(
        "Compiled successfully" in build and "Generating static pages" in build,
        "Next production-build success output is incomplete",
    )
