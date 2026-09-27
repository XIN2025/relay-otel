"""Run one fresh crash scenario and query both projected traces back from SigNoz."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from relay_otel.runtime_paths import signoz_secret_path

OUTPUT_PATH = PROJECT_ROOT / "receipts" / "work" / "reproduce-latest.json"
PRODUCT_DEADLINE_SECONDS = 90.0
PRODUCT_CALLER_TIMEOUT_SECONDS = 100.0


def child_environment() -> dict[str, str]:
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
    environment = {key: os.environ[key] for key in inherited if key in os.environ}
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(SRC),
            "PYTHONUTF8": "1",
        }
    )
    return environment


def write_atomic(payload: dict) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=OUTPUT_PATH.name + ".", suffix=".tmp", dir=OUTPUT_PATH.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, OUTPUT_PATH)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--candidate-inputs", type=Path)
    args = parser.parse_args()
    if not 0 <= args.seed <= 10_000:
        raise ValueError("seed must be between 0 and 10000")

    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "product_run.py"),
        "--seed",
        str(args.seed),
        "--source",
        "local-reproduction",
        "--deadline-seconds",
        str(PRODUCT_DEADLINE_SECONDS),
        "--signoz-api-key-file",
        str(signoz_secret_path()),
    ]
    if args.candidate_inputs is not None:
        command.extend(("--candidate-inputs", str(args.candidate_inputs)))
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=child_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=PRODUCT_CALLER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "product_caller_timeout",
                        "message": (
                            "product process exceeded its caller grace period after "
                            "the product deadline"
                        ),
                        "details": {
                            "product_deadline_seconds": PRODUCT_DEADLINE_SECONDS,
                            "caller_timeout_seconds": PRODUCT_CALLER_TIMEOUT_SECONDS,
                        },
                    }
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 74
    if result.returncode in {74, 75, 78}:
        print(result.stderr.strip(), file=sys.stderr)
        return result.returncode
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return result.returncode
    run = json.loads(result.stdout.strip())
    summary = {
        "schema_version": 2,
        "run_id": run["runId"],
        "hard_exit_code": run["hardExitCode"],
        "activation_attempts": run["groundTruth"]["activationAttempts"],
        "effect_intents": run["groundTruth"]["effectIntents"],
        "effect_completions": run["groundTruth"]["effectCompletions"],
        "journal_resolutions": run["groundTruth"]["journalResolutions"],
        "provider_attempts": run["groundTruth"]["providerAttempts"],
        "business_executions": run["groundTruth"]["businessExecutions"],
        "aware_executed_effect_spans": run["comparison"]["awareExecutedEffectSpans"],
        "aware_resolved_effect_spans": run["comparison"]["awareResolvedEffectSpans"],
        "control_executed_effect_spans": run["comparison"][
            "controlExecutedEffectSpans"
        ],
        "query_back": run["queryBack"],
        "provenance": run["provenance"],
    }
    write_atomic(summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
