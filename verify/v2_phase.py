"""Run and immutably record one preregistered v2 release phase."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
SCRIPTS = PROJECT_ROOT / "scripts"
for source_path in (SRC, SCRIPTS):
    if str(source_path) not in sys.path:
        sys.path.insert(0, str(source_path))

from materialize_web_data import validate_document
from _command_truth import (
    json_stdout,
    last_json_line,
    validate_bandit,
    validate_eslint,
    validate_mypy_output,
    validate_pip_audit,
    validate_pnpm_audit,
    validate_ruff_output,
    validate_runtime_document,
    validate_web_text_outputs,
)
from relay_otel.deployment import EXPECTED_STACK
from relay_otel.lineage import (
    ArtifactReference,
    ValidatedInputs,
    artifact_reference,
    validate_candidate_receipts,
    validate_frozen_inputs,
    write_json_exclusive,
)
from relay_otel.runtime_paths import signoz_secret_path

PHASES = {
    "runtime": "01-runtime",
    "integration": "02-integration",
    "publication": "03-publication",
}
EXPECTED_BROWSER_ROUTES = {
    "/",
    "/architecture",
    "/contract",
    "/runs",
    "/spec",
}
EXPECTED_VIEWPORT_WIDTHS = {375, 768, 1024, 1440}


class PhaseFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PhaseFailure(message)


def project_path(value: Path) -> Path:
    path = value if value.is_absolute() else PROJECT_ROOT / value
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise PhaseFailure(f"path is outside the project: {value}") from exc
    return resolved


def read_json(path: Path) -> dict[str, Any]:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{path} must contain a JSON object")
    return value


def display_command(command: list[str]) -> list[str]:
    root = str(PROJECT_ROOT.resolve())
    displayed: list[str] = []
    redact_next = False
    for part in command:
        if redact_next:
            displayed.append("<host-local-signoz-env>")
            redact_next = False
            continue
        displayed.append(part.replace(root, "."))
        redact_next = part == "--env-file"
    return displayed


def display_output(value: str) -> str:

    root = str(PROJECT_ROOT.resolve())
    json_escaped_root = json.dumps(root)[1:-1]
    return value.replace(json_escaped_root, ".").replace(root, ".")


def command_environment() -> dict[str, str]:
    inherited = (
        "APPDATA",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "ProgramFiles",
        "ProgramW6432",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "UV_CACHE_DIR",
        "USERPROFILE",
    )
    environment = {key: os.environ[key] for key in inherited if key in os.environ}
    environment.update(
        {
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def required_executable(name: str) -> str:
    discovered = shutil.which(name, path=command_environment().get("PATH"))
    if discovered:
        return discovered
    if name == "docker" and os.name == "nt":
        docker_desktop = Path("C:/Program Files/Docker/Docker/resources/bin/docker.exe")
        if docker_desktop.is_file():
            return str(docker_desktop)
    raise PhaseFailure(f"required executable is unavailable: {name}")


def execution_command(command: list[str]) -> list[str]:
    executable = Path(command[0])
    resolved = (
        str(executable) if executable.is_absolute() else required_executable(command[0])
    )
    normalized = [resolved, *command[1:]]
    if os.name == "nt" and Path(resolved).suffix.lower() in {".bat", ".cmd"}:
        system_root = command_environment().get("SystemRoot", "C:/Windows")
        command_processor = str(Path(system_root) / "System32" / "cmd.exe")
        return [
            command_processor,
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(normalized),
        ]
    return normalized


def run_command(
    command: list[str], *, cwd: Path = PROJECT_ROOT, timeout: int = 180
) -> dict[str, Any]:
    result = subprocess.run(
        execution_command(command),
        cwd=cwd,
        env=command_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    record = {
        "command": display_command(command),
        "cwd": cwd.relative_to(PROJECT_ROOT).as_posix() or ".",
        "exit_code": result.returncode,
        "stdout": display_output(result.stdout[-50_000:]),
        "stderr": display_output(result.stderr[-50_000:]),
    }
    require(result.returncode == 0, f"command failed: {record['command']}")
    return record


def exact_artifact(value: object, *, location: str) -> ArtifactReference:
    reference = ArtifactReference.from_document(value, location=location)
    actual = artifact_reference(PROJECT_ROOT, project_path(Path(reference.path)))
    require(actual == reference, f"artifact reference changed: {reference.path}")
    return actual


def copy_artifact_exclusive(source: Path, destination: Path) -> ArtifactReference:

    destination.parent.mkdir(parents=True, exist_ok=True)
    require(not destination.exists(), f"snapshot already exists: {destination}")
    temporary = destination.parent / (
        f".{destination.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with (
            source.open("rb") as input_stream,
            os.fdopen(descriptor, "wb") as output_stream,
        ):
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise PhaseFailure(f"snapshot already exists: {destination}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    source_reference = artifact_reference(PROJECT_ROOT, source)
    snapshot_reference = artifact_reference(PROJECT_ROOT, destination)
    require(
        source_reference.sha256 == snapshot_reference.sha256
        and source_reference.bytes == snapshot_reference.bytes,
        f"snapshot bytes changed while copying {source}",
    )
    return snapshot_reference


def require_candidate_provenance(
    document: dict[str, Any],
    *,
    validated_inputs: ValidatedInputs,
    inputs_reference: ArtifactReference,
    location: str,
    allow_journal: bool,
) -> dict[str, Any]:
    provenance = document.get("provenance")
    require(isinstance(provenance, dict), f"{location} lacks provenance")
    expected = validated_inputs.provenance()
    for field, value in expected.items():
        require(
            provenance.get(field) == value,
            f"{location} provenance changed at {field}",
        )
    require(
        provenance.get("lineageInputsPath") == inputs_reference.path
        and provenance.get("lineageInputsSha256") == inputs_reference.sha256,
        f"{location} does not bind the selected inputs artifact",
    )
    extra_fields = {"journalPath", "journalSha256"} if allow_journal else set()
    require(
        set(provenance) == set(expected) | extra_fields,
        f"{location} provenance field set changed",
    )
    return provenance


def runtime_phase(
    python: Path, validated_inputs: ValidatedInputs
) -> tuple[dict[str, Any], dict[str, bool]]:
    inputs_document = read_json(project_path(Path(validated_inputs.inputs.path)))
    frozen_environment = inputs_document.get("environment")
    require(isinstance(frozen_environment, dict), "frozen environment is missing")
    frozen_packages = frozen_environment.get("packages")
    require(
        isinstance(frozen_packages, dict) and frozen_packages,
        "frozen packages are missing",
    )
    probe_script = (
        "import importlib.metadata as metadata, json, platform; "
        f"names={list(frozen_packages)!r}; "
        "print(json.dumps({'python': platform.python_version(), "
        "'implementation': platform.python_implementation(), "
        "'platform': platform.platform(), "
        "'packages': {name: metadata.version(name) for name in names}}, "
        "sort_keys=True))"
    )
    commands = [
        run_command(["uvx", "uv@0.12.5", "sync", "--frozen"]),
        run_command(["uvx", "uv@0.12.5", "lock", "--check"]),
        run_command(
            [str(PROJECT_ROOT / ".venv" / "Scripts" / "ruff.exe"), "check", "."]
        ),
        run_command([str(PROJECT_ROOT / ".venv" / "Scripts" / "mypy.exe")]),
        run_command(
            [
                str(python),
                "-m",
                "compileall",
                "-q",
                "src",
                "scripts",
                "verify",
            ]
        ),
        run_command([str(python), "-c", probe_script]),
        run_command([str(python), "verify/runtime_correctness.py"]),
    ]
    validate_ruff_output(commands[2], require=require)
    validate_mypy_output(commands[3], require=require)
    actual_environment = last_json_line(
        commands[-2], location="frozen environment probe", require=require
    )
    require(
        actual_environment == frozen_environment,
        "project .venv does not match the frozen candidate environment",
    )
    runtime_document = validate_runtime_document(
        last_json_line(commands[-1], location="runtime verifier", require=require),
        require=require,
    )
    criteria = {
        "frozen_uv_sync": True,
        "exact_uv_lock": True,
        "ruff": True,
        "strict_mypy": True,
        "compileall": True,
        "frozen_environment_matches_project_venv": True,
        "bounded_runtime_correctness": True,
        "real_hard_exit_and_fresh_resume": True,
    }
    return {
        "commands": commands,
        "environment": actual_environment,
        "runtime": runtime_document,
    }, criteria


def http_status(url: str) -> int:
    with urllib.request.urlopen(url, timeout=10) as response:
        return int(response.status)


def integration_phase(
    product_run: Path,
    *,
    lineage_dir: Path,
    validated_inputs: ValidatedInputs,
    inputs_reference: ArtifactReference,
) -> tuple[dict[str, Any], dict[str, bool], list[Path]]:
    document = validate_document(read_json(product_run))
    run_dir = product_run.parent
    require(
        run_dir.parent.resolve()
        == (PROJECT_ROOT / "receipts" / "work" / "product-runs").resolve(),
        "product run is outside the generated run root",
    )
    journal_path = run_dir / "journal.sqlite"
    effect_path = run_dir / "effects.sqlite"
    for path in (journal_path, effect_path):
        require(
            path.is_file() and not path.is_symlink(), f"run state is missing: {path}"
        )
    provenance = require_candidate_provenance(
        document,
        validated_inputs=validated_inputs,
        inputs_reference=inputs_reference,
        location="product run",
        allow_journal=True,
    )
    journal_reference = artifact_reference(PROJECT_ROOT, journal_path)
    require(
        provenance.get("journalPath") == journal_reference.path
        and provenance.get("journalSha256") == journal_reference.sha256,
        "product provenance does not bind the exact checkpointed journal",
    )
    source_artifacts: dict[str, Path] = {"product": product_run}
    exports: dict[str, Any] = {}
    for mode in ("aware", "control"):
        final_path = run_dir / f"{mode}-spans.json"
        envelope = read_json(final_path)
        require(envelope.get("mode") == mode, f"{mode} export mode changed")
        require(envelope.get("span_count") == 5, f"{mode} export is not five spans")
        require_candidate_provenance(
            envelope,
            validated_inputs=validated_inputs,
            inputs_reference=inputs_reference,
            location=f"{mode} export",
            allow_journal=False,
        )
        otlp = envelope.get("otlp")
        require(isinstance(otlp, dict), f"{mode} export lacks delivery state")
        require(
            otlp.get("attempted") is True
            and otlp.get("state") == "DELIVERED"
            and otlp.get("result") == "SUCCESS",
            f"{mode} OTLP delivery was not acknowledged",
        )
        require(
            otlp.get("acknowledged") is True and otlp.get("cleanup_state") == "SUCCESS",
            f"{mode} OTLP delivery lifecycle did not complete cleanly",
        )
        staged = exact_artifact(
            otlp.get("staged_envelope"), location=f"{mode}.staged_envelope"
        )
        delivery = exact_artifact(
            otlp.get("delivery_receipt"), location=f"{mode}.delivery_receipt"
        )
        staged_path = project_path(Path(staged.path))
        delivery_path = project_path(Path(delivery.path))
        delivery_document = read_json(project_path(Path(delivery.path)))
        require(
            delivery_document.get("state") == "DELIVERED"
            and delivery_document.get("attempted") is True
            and delivery_document.get("acknowledged") is True
            and delivery_document.get("cleanup_state") == "SUCCESS",
            f"{mode} delivery receipt is not DELIVERED",
        )
        source_artifacts[f"{mode}_final"] = final_path
        source_artifacts[f"{mode}_staged"] = staged_path
        source_artifacts[f"{mode}_delivery"] = delivery_path

    secret_path = signoz_secret_path()
    docker_config = PROJECT_ROOT / "receipts" / "work" / "docker-config"
    docker = required_executable("docker")
    compose_prefix = [
        docker,
        "--config",
        str(docker_config),
        "compose",
        "--env-file",
        str(secret_path),
        "-f",
        "deploy/pours/deployment/compose.yaml",
        "-f",
        "deploy/root-user.override.yaml",
    ]
    compose = run_command([*compose_prefix, "config", "--quiet"])
    compose_hash_command = run_command([*compose_prefix, "config", "--hash", "*"])
    compose_hash_lines = [
        line for line in compose_hash_command["stdout"].splitlines() if line.strip()
    ]
    require(
        len(compose_hash_lines) == len(EXPECTED_STACK)
        and all(len(line.split(maxsplit=1)) == 2 for line in compose_hash_lines),
        "rendered Compose hash output is malformed",
    )
    compose_hashes = dict(line.split(maxsplit=1) for line in compose_hash_lines)
    require(
        set(compose_hashes) == set(EXPECTED_STACK),
        "rendered Compose service set differs from the registered stack",
    )
    container_names_command = run_command(
        [
            docker,
            "--config",
            str(docker_config),
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.project=relay-otel-signoz",
            "--format",
            "{{.Names}}",
        ]
    )
    expected_container_names = {
        str(spec["container"]) for spec in EXPECTED_STACK.values()
    }
    actual_container_name_lines = [
        line.strip()
        for line in container_names_command["stdout"].splitlines()
        if line.strip()
    ]
    actual_container_names = set(actual_container_name_lines)
    require(
        len(actual_container_name_lines) == len(expected_container_names)
        and actual_container_names == expected_container_names,
        "Compose project container set differs from the registered stack",
    )
    port_inspect_command = run_command(
        [
            docker,
            "--config",
            str(docker_config),
            "inspect",
            "--format",
            "{{.Name}}|{{json .NetworkSettings.Ports}}",
            *sorted(expected_container_names),
        ]
    )
    published_ports: list[dict[str, str]] = []
    observed_port_containers: set[str] = set()
    for line in port_inspect_command["stdout"].splitlines():
        raw_name, separator, raw_ports = line.partition("|")
        require(separator == "|", "Docker port inspection output is malformed")
        name = raw_name.removeprefix("/")
        require(
            name in expected_container_names and name not in observed_port_containers,
            "Docker port inspection container set is invalid",
        )
        observed_port_containers.add(name)
        port_map: Any = json.loads(raw_ports)
        require(isinstance(port_map, dict), "Docker port map is not an object")
        for container_port, bindings in port_map.items():
            require(isinstance(container_port, str), "Docker container port is invalid")
            if bindings is None:
                continue
            require(isinstance(bindings, list), "Docker host bindings are invalid")
            for binding in bindings:
                require(
                    isinstance(binding, dict)
                    and set(binding) == {"HostIp", "HostPort"}
                    and isinstance(binding.get("HostIp"), str)
                    and isinstance(binding.get("HostPort"), str),
                    "Docker host binding is malformed",
                )
                published_ports.append(
                    {
                        "container": name,
                        "container_port": container_port,
                        "host_ip": binding["HostIp"],
                        "host_port": binding["HostPort"],
                    }
                )
    require(
        observed_port_containers == expected_container_names,
        "Docker port inspection omitted a registered container",
    )
    published_ports.sort(
        key=lambda value: (
            value["container"],
            value["container_port"],
            value["host_ip"],
            value["host_port"],
        )
    )
    require(
        published_ports
        == [
            {
                "container": "relay-otel-signoz-ingester-1",
                "container_port": "13133/tcp",
                "host_ip": "127.0.0.1",
                "host_port": "13133",
            },
            {
                "container": "relay-otel-signoz-ingester-1",
                "container_port": "4318/tcp",
                "host_ip": "127.0.0.1",
                "host_port": "4318",
            },
            {
                "container": "relay-otel-signoz-signoz-0",
                "container_port": "8080/tcp",
                "host_ip": "127.0.0.1",
                "host_port": "8080",
            },
        ],
        "published Docker bindings differ from the exact loopback-only set",
    )
    inspect_format = (
        '{{.Name}}|{{index .Config.Labels "com.docker.compose.service"}}|'
        "{{.Config.Image}}|{{.Image}}|{{.State.Status}}|{{.State.ExitCode}}|"
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|"
        '{{index .Config.Labels "com.docker.compose.config-hash"}}'
    )
    container_inspect_command = run_command(
        [
            docker,
            "--config",
            str(docker_config),
            "inspect",
            "--format",
            inspect_format,
            *sorted(expected_container_names),
        ]
    )
    image_pin_document = read_json(PROJECT_ROOT / "deploy" / "IMAGE-PINS.json")
    image_pins = image_pin_document.get("images")
    require(isinstance(image_pins, dict), "IMAGE-PINS.json lacks images")
    require(
        set(image_pins)
        == {
            "clickhouse/clickhouse-keeper",
            "clickhouse/clickhouse-server",
            "signoz/signoz",
            "signoz/signoz-otel-collector",
        },
        "IMAGE-PINS.json image set changed",
    )
    container_inventory: dict[str, dict[str, object]] = {}
    for line in container_inspect_command["stdout"].splitlines():
        parts = line.split("|")
        require(len(parts) == 8, "Docker container inspection output is malformed")
        (
            raw_name,
            service,
            configured_image,
            image_id,
            state,
            raw_exit_code,
            docker_health,
            config_hash,
        ) = parts
        name = raw_name.removeprefix("/")
        require(service in EXPECTED_STACK, f"unexpected Compose service: {service}")
        require(
            service not in container_inventory,
            f"duplicate Compose service inspection: {service}",
        )
        expected = EXPECTED_STACK[service]
        repository = str(expected["image"])
        digest = image_pins.get(repository)
        require(isinstance(digest, str), f"image pin missing for {repository}")
        require(name == expected["container"], f"container name changed for {service}")
        require(
            configured_image == f"{repository}@{digest}" and image_id == digest,
            f"running image differs from the authoritative pin for {service}",
        )
        require(
            config_hash == compose_hashes[service],
            f"running container config differs from rendered Compose for {service}",
        )
        exit_code = int(raw_exit_code)
        if expected["lifecycle"] == "running_healthy":
            require(
                state == "running" and exit_code == 0 and docker_health == "healthy",
                f"required service is not running and healthy: {service}",
            )
        else:
            require(
                state == "exited" and exit_code == 0 and docker_health == "none",
                f"required one-shot service did not complete successfully: {service}",
            )
        container_inventory[service] = {
            "config_hash": config_hash,
            "configured_image": configured_image,
            "container": name,
            "docker_health": docker_health,
            "exit_code": exit_code,
            "image_id": image_id,
            "lifecycle": expected["lifecycle"],
            "state": state,
        }
    require(
        set(container_inventory) == set(EXPECTED_STACK),
        "Docker inspection omitted a registered Compose service",
    )
    container_health = {
        str(record["container"]): record["docker_health"]
        for record in container_inventory.values()
        if record["lifecycle"] == "running_healthy"
    }
    require(
        container_health
        == {
            "relay-otel-signoz-ingester-1": "healthy",
            "relay-otel-signoz-signoz-0": "healthy",
            "relay-otel-signoz-telemetrykeeper-clickhousekeeper-0": "healthy",
            "relay-otel-signoz-telemetrystore-clickhouse-0-0": "healthy",
        },
        "required SigNoz containers are not Docker-healthy",
    )
    histogram_command = run_command(
        [
            docker,
            "--config",
            str(docker_config),
            "exec",
            "relay-otel-signoz-telemetrystore-clickhouse-0-0",
            "sh",
            "-ec",
            "sha256sum /var/lib/clickhouse/user_scripts/histogramQuantile; "
            "wc -c < /var/lib/clickhouse/user_scripts/histogramQuantile",
        ]
    )
    histogram_lines = histogram_command["stdout"].splitlines()
    require(len(histogram_lines) == 2, "histogram helper inspection output changed")
    histogram_digest = histogram_lines[0].split(maxsplit=1)[0]
    histogram_bytes = int(histogram_lines[1].strip())
    histogram_pins = read_json(PROJECT_ROOT / "deploy" / "HISTOGRAM-PINS.json")
    histogram_artifacts = histogram_pins.get("artifacts")
    require(
        isinstance(histogram_artifacts, dict), "HISTOGRAM-PINS.json lacks artifacts"
    )
    amd64_pin = histogram_artifacts.get("linux_amd64")
    require(isinstance(amd64_pin, dict), "linux_amd64 histogram pin is missing")
    require(
        histogram_digest == amd64_pin.get("binary_sha256")
        and histogram_bytes == amd64_pin.get("binary_bytes"),
        "mounted histogram helper differs from its authoritative binary pin",
    )
    histogram_helper = {
        "binary_bytes": histogram_bytes,
        "binary_sha256": histogram_digest,
        "container_path": "/var/lib/clickhouse/user_scripts/histogramQuantile",
        "platform": "linux_amd64",
    }
    health = {
        "collector": http_status("http://127.0.0.1:13133/"),
        "signoz": http_status("http://127.0.0.1:8080/api/v1/health"),
    }
    require(health == {"collector": 200, "signoz": 200}, "local stack is unhealthy")
    python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = PROJECT_ROOT / ".venv" / "bin" / "python"
    require(python.is_file(), "locked project Python is missing")
    access_command = run_command(
        [str(python), "scripts/bootstrap_signoz_access.py"], timeout=60
    )
    access_path = PROJECT_ROOT / "receipts" / "work" / "signoz-access.json"
    access = read_json(access_path)
    require(
        access.get("status") in {"created", "reused"}
        and access.get("service_account") == "relay-otel-query"
        and access.get("service_account_status") == "active"
        and access.get("role") == "signoz-viewer"
        and access.get("roles") == ["signoz-viewer"]
        and access.get("identity_endpoint") == "/api/v1/service_accounts/me"
        and access.get("identity_http_status") == 200
        and access.get("key_validated") is True
        and access.get("key_persisted_in_host_local_env") is True,
        "SigNoz service-account identity evidence is invalid",
    )
    source_artifacts["signoz_access"] = access_path

    effect_reference = artifact_reference(PROJECT_ROOT, effect_path)
    effect_uri = effect_path.resolve().as_uri() + "?mode=ro"
    effect_db = sqlite3.connect(effect_uri, uri=True, isolation_level=None, timeout=5)
    try:
        effect_db.execute("PRAGMA query_only=ON")
        attempt_keys = [
            str(row[0])
            for row in effect_db.execute(
                "SELECT idempotency_key FROM attempts ORDER BY seq"
            ).fetchall()
        ]
        refund_keys = [
            str(row[0])
            for row in effect_db.execute(
                "SELECT idempotency_key FROM refunds ORDER BY idempotency_key"
            ).fetchall()
        ]
    finally:
        effect_db.close()
    require(
        len(attempt_keys) == 1
        and len(refund_keys) == 1
        and attempt_keys[0] == refund_keys[0],
        "effect source facts do not prove one attempted and one durable refund identity",
    )
    source_facts_document = {
        "schema_version": 1,
        "lineage_id": validated_inputs.lineage_id,
        "lineage_inputs_sha256": inputs_reference.sha256,
        "run_id": document["runId"],
        "journal_phase_time_observation": {
            "observed_bytes": journal_reference.bytes,
            "observed_path": journal_reference.path,
            "observed_sha256": journal_reference.sha256,
        },
        "effect_phase_time_observation": {
            "observed_bytes": effect_reference.bytes,
            "observed_path": effect_reference.path,
            "observed_sha256": effect_reference.sha256,
        },
        "sanitized_journal_events": document["journal"]["events"],
        "sanitized_effect_facts": {
            "business_executions": len(refund_keys),
            "idempotency_key_sha256": hashlib.sha256(
                refund_keys[0].encode("utf-8")
            ).hexdigest(),
            "provider_attempts": len(attempt_keys),
        },
    }

    snapshot_root = lineage_dir / "integration"
    source_facts_path = snapshot_root / "source-facts.json"
    write_json_exclusive(source_facts_path, source_facts_document)
    snapshot_paths: list[Path] = [source_facts_path]
    snapshots: dict[str, ArtifactReference] = {
        "source_facts": artifact_reference(PROJECT_ROOT, source_facts_path)
    }
    for role, source_path in source_artifacts.items():
        suffix = source_path.suffix or ".bin"
        destination = snapshot_root / f"{role.replace('_', '-')}{suffix}"
        snapshots[role] = copy_artifact_exclusive(source_path, destination)
        snapshot_paths.append(destination)
    for mode in ("aware", "control"):
        exports[mode] = {
            "final": snapshots[f"{mode}_final"].as_dict(),
            "staged": snapshots[f"{mode}_staged"].as_dict(),
            "delivery": snapshots[f"{mode}_delivery"].as_dict(),
        }

    criteria = {
        "registered_product_document": True,
        "one_provider_attempt_and_one_durable_refund": True,
        "aware_and_control_five_spans": True,
        "immutable_staging_before_acknowledged_otlp": True,
        "exact_duplicate_free_signoz_queryback": True,
        "retained_sanitized_source_facts": True,
        "compose_renders": True,
        "loopback_only_published_ports": True,
        "exact_stack_inventory_and_image_pins": True,
        "required_containers_healthy": True,
        "mounted_histogram_helper_matches_pin": True,
        "collector_and_signoz_healthy": True,
        "service_account_identity_validated": True,
    }
    evidence = {
        "product_snapshot": snapshots["product"].as_dict(),
        "source_product_observation": {
            "relative_path": product_run.relative_to(PROJECT_ROOT).as_posix(),
            "observed_sha256": artifact_reference(PROJECT_ROOT, product_run).sha256,
            "observed_bytes": product_run.stat().st_size,
        },
        "run_id": document["runId"],
        "ground_truth": document["groundTruth"],
        "comparison": document["comparison"],
        "query_back": document["queryBack"],
        "exports": exports,
        "compose": compose,
        "compose_hash_command": compose_hash_command,
        "compose_hashes": compose_hashes,
        "port_inspect_command": port_inspect_command,
        "published_ports": published_ports,
        "container_names_command": container_names_command,
        "container_inspect_command": container_inspect_command,
        "container_inventory": container_inventory,
        "container_health": container_health,
        "histogram_command": histogram_command,
        "histogram_helper": histogram_helper,
        "health": health,
        "service_account_command": access_command,
        "service_account_snapshot": snapshots["signoz_access"].as_dict(),
        "source_facts_snapshot": snapshots["source_facts"].as_dict(),
        "snapshots": {
            role: reference.as_dict() for role, reference in sorted(snapshots.items())
        },
    }
    return evidence, criteria, snapshot_paths


def package_contents(path: Path) -> set[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    with tarfile.open(path, "r:gz") as archive:
        return set(archive.getnames())


def validate_browser_evidence(
    path: Path,
    *,
    featured: ArtifactReference,
    validated_inputs: ValidatedInputs,
) -> tuple[dict[str, Any], list[Path]]:
    lineage_dir = project_path(Path(validated_inputs.inputs.path)).parent
    try:
        path.relative_to(lineage_dir)
    except ValueError as exc:
        raise PhaseFailure(
            "browser evidence must be retained under its lineage"
        ) from exc
    evidence = read_json(path)
    require(evidence.get("verdict") == "PASS", "browser evidence is not PASS")
    require(evidence.get("console_errors") == 0, "browser console contains errors")
    require(evidence.get("page_overflow") is False, "browser found page overflow")
    require(evidence.get("keyboard_navigation") is True, "keyboard QA is missing")
    require(evidence.get("reduced_motion") is True, "reduced-motion QA is missing")
    require(
        evidence.get("lineage_id") == validated_inputs.lineage_id
        and evidence.get("lineage_inputs_sha256") == validated_inputs.inputs.sha256,
        "browser evidence belongs to another candidate lineage",
    )
    featured_reference = exact_artifact(
        evidence.get("featured"), location="browser.featured"
    )
    require(
        featured_reference == featured, "browser evidence used another featured proof"
    )
    viewports = evidence.get("viewports")
    require(isinstance(viewports, list), "browser viewports are missing")
    viewport_records = [entry for entry in viewports if isinstance(entry, dict)]
    require(
        {entry.get("width") for entry in viewport_records} == EXPECTED_VIEWPORT_WIDTHS,
        "browser viewport set changed",
    )
    require(
        len(viewport_records) == len(EXPECTED_VIEWPORT_WIDTHS)
        and all(
            entry.get("page_overflow") is False
            and isinstance(entry.get("height"), int)
            and entry["height"] >= 600
            for entry in viewport_records
        ),
        "each registered viewport needs a no-overflow observation",
    )
    routes = evidence.get("routes")
    require(isinstance(routes, list), "browser routes are missing")
    featured_document = validate_document(
        read_json(project_path(Path(featured_reference.path)))
    )
    required_routes = {
        *EXPECTED_BROWSER_ROUTES,
        f"/runs/{featured_document['runId']}",
    }
    require(required_routes.issubset(set(routes)), "browser route set is incomplete")
    require(
        evidence.get("origin_security")
        == {
            "cross_site_status": 403,
            "https_origin_status": 403,
            "missing_origin_status": 403,
            "same_origin_unavailable_status": 503,
        },
        "request-origin fail-closed checks are incomplete",
    )
    require(
        evidence.get("status_surfaces")
        == {
            "evidence": True,
            "lineage": True,
            "runner": True,
            "signoz": True,
        },
        "the four independent status surfaces were not observed",
    )
    require(
        evidence.get("keyboard")
        == {
            "disabled_state": True,
            "error_state": True,
            "internal_table_scroll": True,
            "skip_link": True,
            "visible_focus": True,
        },
        "keyboard, focus, and state checks are incomplete",
    )
    screenshots = evidence.get("screenshots")
    require(
        isinstance(screenshots, list)
        and len(screenshots) >= len(EXPECTED_VIEWPORT_WIDTHS),
        "browser screenshots are incomplete",
    )
    screenshot_paths: list[Path] = []
    for index, value in enumerate(screenshots):
        reference = exact_artifact(value, location=f"browser.screenshots[{index}]")
        screenshot_path = project_path(Path(reference.path))
        try:
            screenshot_path.relative_to(lineage_dir)
        except ValueError as exc:
            raise PhaseFailure(
                "browser screenshots must be retained under their lineage"
            ) from exc
        screenshot_paths.append(screenshot_path)
    return evidence, screenshot_paths


def publication_phase(
    featured: Path,
    browser_path: Path,
    *,
    expected_product: ArtifactReference,
    validated_inputs: ValidatedInputs,
    inputs_reference: ArtifactReference,
) -> tuple[dict[str, Any], dict[str, bool], list[Path]]:
    require(
        featured.relative_to(PROJECT_ROOT).as_posix() == "web/data/featured.json",
        "publication must certify the canonical featured proof",
    )
    featured_document = validate_document(read_json(featured))
    require_candidate_provenance(
        featured_document,
        validated_inputs=validated_inputs,
        inputs_reference=inputs_reference,
        location="featured proof",
        allow_journal=True,
    )
    featured_reference = artifact_reference(PROJECT_ROOT, featured)
    require(
        featured_reference.sha256 == expected_product.sha256
        and featured_reference.bytes == expected_product.bytes,
        "featured proof is not the product certified by integration",
    )
    browser, screenshot_paths = validate_browser_evidence(
        browser_path,
        featured=featured_reference,
        validated_inputs=validated_inputs,
    )
    web_root = PROJECT_ROOT / "web"
    expected_node = (PROJECT_ROOT / ".node-version").read_text(encoding="utf-8").strip()
    package_document = read_json(web_root / "package.json")
    expected_pnpm = "11.16.0"
    require(
        package_document.get("version") == "0.2.0"
        and package_document.get("packageManager") == f"pnpm@{expected_pnpm}"
        and package_document.get("engines")
        == {"node": expected_node, "pnpm": expected_pnpm},
        "web package toolchain metadata is not exact",
    )
    node_version = run_command(["node", "--version"])
    pnpm_version = run_command(["corepack", "pnpm", "--version"])
    require(
        node_version["stdout"].strip() == f"v{expected_node}"
        and pnpm_version["stdout"].strip() == expected_pnpm,
        "active Node or pnpm does not match the frozen web toolchain",
    )
    web_install = run_command(
        ["corepack", "pnpm", "install", "--frozen-lockfile"], cwd=web_root
    )
    audit = run_command(
        ["corepack", "pnpm", "audit", "--prod", "--json"],
        cwd=web_root,
    )
    validate_pnpm_audit(
        json_stdout(audit, location="pnpm audit", require=require), require=require
    )
    web_verify = {
        "typecheck": run_command(
            ["corepack", "pnpm", "typecheck"], cwd=web_root, timeout=300
        ),
        "lint": run_command(
            ["corepack", "pnpm", "exec", "eslint", "--format", "json", "."],
            cwd=web_root,
            timeout=300,
        ),
        "format": run_command(
            ["corepack", "pnpm", "format:check"], cwd=web_root, timeout=300
        ),
        "build": run_command(["corepack", "pnpm", "build"], cwd=web_root, timeout=300),
    }
    validate_eslint(
        json_stdout(web_verify["lint"], location="ESLint", require=require),
        require=require,
    )
    validate_web_text_outputs(web_verify, require=require)
    python_root = PROJECT_ROOT / ".venv" / "Scripts"
    with tempfile.TemporaryDirectory(
        prefix="relay-otel-audit-", dir=PROJECT_ROOT / "receipts" / "work"
    ) as audit_directory:
        requirements_path = Path(audit_directory) / "runtime-requirements.txt"
        dependency_export = run_command(
            [
                "uvx",
                "uv@0.12.5",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements.txt",
                "--output-file",
                str(requirements_path),
            ]
        )
        dependency_audit = run_command(
            [
                str(python_root / "pip-audit.exe"),
                "--strict",
                "--requirement",
                str(requirements_path),
                "--format",
                "json",
            ],
            timeout=300,
        )
        validate_pip_audit(
            json_stdout(dependency_audit, location="pip-audit", require=require),
            require=require,
        )
    source_security = run_command(
        [
            str(python_root / "bandit.exe"),
            "-r",
            "src",
            "scripts",
            "-lll",
            "-iii",
            "-f",
            "json",
        ],
        timeout=300,
    )
    validate_bandit(
        json_stdout(source_security, location="Bandit", require=require),
        require=require,
    )

    package_paths: list[Path] = []
    with tempfile.TemporaryDirectory(
        prefix="relay-otel-build-", dir=PROJECT_ROOT / "receipts" / "work"
    ) as directory:
        build_dir = Path(directory)
        package_build = run_command(
            [
                "uvx",
                "uv@0.12.5",
                "build",
                "--out-dir",
                str(build_dir),
            ],
            timeout=180,
        )
        build_entries = sorted(build_dir.iterdir())
        distributions = [
            path
            for path in build_entries
            if path.suffix == ".whl" or path.name.endswith(".tar.gz")
        ]
        unexpected_entries = [
            path.name
            for path in build_entries
            if path not in distributions and path.name != ".gitignore"
        ]
        require(
            not unexpected_entries,
            f"package build created unexpected entries: {unexpected_entries}",
        )
        wheel_paths = [path for path in distributions if path.suffix == ".whl"]
        sdist_paths = [path for path in distributions if path.name.endswith(".tar.gz")]
        require(
            len(wheel_paths) == 1 and len(sdist_paths) == 1,
            "package build did not create exactly one wheel and one sdist",
        )
        contents = {path.name: package_contents(path) for path in distributions}
        sdist_entries = contents[sdist_paths[0].name]
        wheel_entries = contents[wheel_paths[0].name]
        wheel_path = wheel_paths[0]
        require(
            any(name.endswith("/README.md") for name in sdist_entries),
            "sdist omitted README.md",
        )
        require(
            any("/src/relay/__init__.py" in name for name in sdist_entries)
            and any("/src/relay_otel/__init__.py" in name for name in sdist_entries)
            and "relay/__init__.py" in wheel_entries
            and "relay_otel/__init__.py" in wheel_entries,
            "package distributions omitted expected relay source",
        )
        with zipfile.ZipFile(wheel_path) as wheel_archive:
            entry_points_name = next(
                (
                    name
                    for name in wheel_archive.namelist()
                    if name.endswith(".dist-info/entry_points.txt")
                ),
                None,
            )
            require(entry_points_name is not None, "wheel omitted console entry points")
            entry_points = wheel_archive.read(entry_points_name).decode("utf-8")
        require(
            "relay-otel = relay_otel.cli:main" in entry_points,
            "wheel does not expose the registered relay-otel CLI",
        )
        installed_site = build_dir / "installed-wheel"
        package_install = run_command(
            [
                "uvx",
                "uv@0.12.5",
                "pip",
                "install",
                "--target",
                str(installed_site),
                "--no-deps",
                str(wheel_path),
            ],
            timeout=180,
        )
        python_executable = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
        installed_site_relative = installed_site.relative_to(PROJECT_ROOT)
        smoke_program = (
            "import json,pathlib,sys;"
            f"site=pathlib.Path({json.dumps(str(installed_site_relative))}).resolve();"
            "sys.path.insert(0,str(site));"
            "import relay_otel.cli as cli;"
            "root=cli.resolve_project_root(pathlib.Path('.'));"
            "module=pathlib.Path(cli.__file__).resolve();"
            "assert module.is_relative_to(site);"
            "assert root == pathlib.Path('.').resolve();"
            "print(json.dumps({'module_under_installed_target':True,"
            "'project_root_verified':True},sort_keys=True))"
        )
        package_smoke = run_command(
            [str(python_executable), "-I", "-c", smoke_program], timeout=60
        )
        smoke_document = json.loads(package_smoke["stdout"])
        require(
            smoke_document
            == {
                "module_under_installed_target": True,
                "project_root_verified": True,
            },
            "installed wheel did not resolve the explicit project root",
        )
        package_root = (
            project_path(Path(validated_inputs.inputs.path)).parent
            / "publication"
            / "packages"
        )
        retained_distributions: dict[str, ArtifactReference] = {}
        for distribution in distributions:
            retained = package_root / distribution.name
            retained_distributions[distribution.name] = copy_artifact_exclusive(
                distribution, retained
            )
            package_paths.append(retained)
        package_summary = {
            "command": package_build,
            "contents": {
                name: sorted(entries) for name, entries in sorted(contents.items())
            },
            "distributions": {
                name: reference.as_dict()
                for name, reference in sorted(retained_distributions.items())
            },
            "entry_points": entry_points,
            "install_command": package_install,
            "installed_smoke_command": package_smoke,
        }

    markdown = list(PROJECT_ROOT.glob("*.md"))
    require(markdown, "publication Markdown is missing")
    for path in markdown:
        text = path.read_text(encoding="utf-8")
        require("```mermaid" not in text.lower(), f"Mermaid found in {path.name}")
    require(
        "does not audit signoz code"
        in (PROJECT_ROOT / "README.md").read_text(encoding="utf-8").lower(),
        "publication lacks the independent-PoC/company-code-audit disclaimer",
    )
    publication_text = "\n".join(
        path.read_text(encoding="utf-8").lower() for path in markdown
    )
    for obsolete_claim in (
        "naive projection",
        "naive trace",
        "genai conformance",
        "semantic-convention compliant",
    ):
        require(
            obsolete_claim not in publication_text,
            f"obsolete active vocabulary remains: {obsolete_claim}",
        )
    criteria = {
        "featured_v2_proof": True,
        "frozen_web_install": True,
        "locked_web_toolchain": True,
        "pnpm_production_audit": True,
        "python_dependency_audit": True,
        "python_source_security_scan": True,
        "web_typecheck_lint_format_build": True,
        "operator_observed_responsive_browser_routes": True,
        "operator_observed_origin_and_unavailable_fail_closed": True,
        "operator_observed_keyboard_and_reduced_motion": True,
        "package_readme_source_and_installed_cli": True,
        "publication_scope_and_v2_vocabulary": True,
    }
    evidence = {
        "featured": artifact_reference(PROJECT_ROOT, featured).as_dict(),
        "featured_run_id": featured_document["runId"],
        "browser": artifact_reference(PROJECT_ROOT, browser_path).as_dict(),
        "browser_assurance": {
            "kind": "hash-bound-operator-observation",
            "release_audit_replays_browser": False,
            "release_audit_revalidates": [
                "artifact-hashes-and-sizes",
                "featured-proof-binding",
                "registered-observation-schema",
                "screenshot-references",
            ],
        },
        "browser_summary": browser,
        "node_version": node_version,
        "pnpm_version": pnpm_version,
        "web_install": web_install,
        "pnpm_audit": audit,
        "python_dependency_audit": dependency_audit,
        "python_dependency_export": dependency_export,
        "python_source_security_scan": source_security,
        "web_verify": web_verify,
        "package": package_summary,
        "publication": [
            artifact_reference(PROJECT_ROOT, path).as_dict()
            for path in sorted(markdown)
        ],
    }
    return (
        evidence,
        criteria,
        [
            featured,
            browser_path,
            *screenshot_paths,
            *package_paths,
            *markdown,
        ],
    )


def integration_product_snapshot(receipt_path: Path) -> ArtifactReference:
    receipt = read_json(receipt_path)
    require(
        receipt.get("phase") == PHASES["integration"], "integration receipt missing"
    )
    evidence_reference = exact_artifact(
        receipt.get("evidence"), location="integration receipt evidence"
    )
    phase_evidence = read_json(project_path(Path(evidence_reference.path)))
    evidence = phase_evidence.get("evidence")
    require(isinstance(evidence, dict), "integration phase evidence payload is missing")
    return exact_artifact(
        evidence.get("product_snapshot"), location="integration product snapshot"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=tuple(PHASES))
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--receipt-chain", type=Path, action="append", default=[])
    parser.add_argument("--product-run", type=Path)
    parser.add_argument("--featured", type=Path, default=Path("web/data/featured.json"))
    parser.add_argument("--browser-evidence", type=Path)
    args = parser.parse_args()

    inputs_path = project_path(args.inputs)
    chain = [project_path(path) for path in args.receipt_chain]
    expected_chain_length = {"runtime": 0, "integration": 1, "publication": 2}[
        args.phase
    ]
    require(
        len(chain) == expected_chain_length,
        f"{args.phase} requires {expected_chain_length} predecessor receipt(s)",
    )
    if chain:
        candidate = validate_candidate_receipts(PROJECT_ROOT, inputs_path, chain)
        validated_inputs = candidate.inputs
    else:
        validated_inputs = validate_frozen_inputs(PROJECT_ROOT, inputs_path)
    inputs_reference = artifact_reference(PROJECT_ROOT, inputs_path)
    phase_name = PHASES[args.phase]
    lineage_dir = inputs_path.parent
    evidence_path = lineage_dir / f"phase-{phase_name}.evidence.json"
    receipt_path = lineage_dir / f"phase-{phase_name}.json"
    require(
        not evidence_path.exists() and not receipt_path.exists(),
        f"phase output already exists for {phase_name}",
    )

    supporting_paths: list[Path] = []
    if args.phase == "runtime":
        python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
        require(python.is_file(), "locked project Python is missing")
        evidence, criteria = runtime_phase(python, validated_inputs)
    elif args.phase == "integration":
        require(args.product_run is not None, "integration requires --product-run")
        product_run = project_path(args.product_run)
        evidence, criteria, supporting_paths = integration_phase(
            product_run,
            lineage_dir=lineage_dir,
            validated_inputs=validated_inputs,
            inputs_reference=inputs_reference,
        )
    else:
        require(
            args.browser_evidence is not None,
            "publication requires --browser-evidence",
        )
        featured = project_path(args.featured)
        browser = project_path(args.browser_evidence)
        expected_product = integration_product_snapshot(chain[-1])
        evidence, criteria, supporting_paths = publication_phase(
            featured,
            browser,
            expected_product=expected_product,
            validated_inputs=validated_inputs,
            inputs_reference=inputs_reference,
        )

    evidence_document = {
        "schema_version": 1,
        "phase": phase_name,
        "lineage_id": validated_inputs.lineage_id,
        "lineage_inputs_sha256": inputs_reference.sha256,
        "generated_at": datetime.now(UTC).isoformat(),
        "verdict": "PASS",
        "criteria": criteria,
        "evidence": evidence,
    }
    write_json_exclusive(evidence_path, evidence_document)
    evidence_reference = artifact_reference(PROJECT_ROOT, evidence_path)
    previous_reference = (
        artifact_reference(PROJECT_ROOT, chain[-1]).as_dict() if chain else None
    )
    receipt = {
        "schema_version": 1,
        "phase": phase_name,
        "lineage_id": validated_inputs.lineage_id,
        "lineage_inputs_sha256": inputs_reference.sha256,
        "generated_at": datetime.now(UTC).isoformat(),
        "verdict": "PASS",
        "previous_phase_receipt": previous_reference,
        "criteria": criteria,
        "evidence": evidence_reference.as_dict(),
        "supporting_artifacts": [
            artifact_reference(PROJECT_ROOT, path).as_dict()
            for path in dict.fromkeys(supporting_paths)
        ],
    }
    write_json_exclusive(receipt_path, receipt)
    print(
        json.dumps(
            {
                "verdict": "PASS",
                "phase": phase_name,
                "evidence": evidence_reference.as_dict(),
                "receipt": artifact_reference(PROJECT_ROOT, receipt_path).as_dict(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
