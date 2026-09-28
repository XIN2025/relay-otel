"""Recompute current hashes and re-evaluate retained v2 phase evidence."""

from __future__ import annotations

import json
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
SCRIPTS = PROJECT_ROOT / "scripts"
OUTPUT = PROJECT_ROOT / "receipts" / "work" / "release-audit-current.json"
for import_root in (SRC, SCRIPTS, Path(__file__).resolve().parent):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from materialize_web_data import validate_document
from _command_truth import (
    json_stdout,
    last_json_line,
    validate_bandit,
    validate_command,
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
    LineageValidationError,
    ValidatedLineage,
    validate_current_lineage,
)

from _common import sha256_file, utc_now, write_json_atomic


class SemanticEvidenceError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SemanticEvidenceError(message)


def read_json(path: Path) -> dict[str, Any]:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"expected a JSON object: {path}")
    return value


def package_contents(path: Path) -> set[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    with tarfile.open(path, "r:gz") as archive:
        return set(archive.getnames())


def referenced_path(value: object, *, location: str) -> Path:
    reference = ArtifactReference.from_document(value, location=location)
    path = (PROJECT_ROOT / reference.path).resolve()
    try:
        path.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise SemanticEvidenceError(
            f"artifact escaped the project: {location}"
        ) from exc
    require(path.is_file(), f"referenced artifact is missing: {reference.path}")
    require(
        sha256_file(path) == reference.sha256,
        f"artifact hash changed: {reference.path}",
    )
    require(
        path.stat().st_size == reference.bytes,
        f"artifact size changed: {reference.path}",
    )
    return path


def phase_evidence(validated: ValidatedLineage, phase: str) -> dict[str, Any]:
    receipt_reference = validated.phase_receipts[phase]
    receipt = read_json(PROJECT_ROOT / receipt_reference.path)
    evidence_path = referenced_path(
        receipt.get("evidence"), location=f"{phase}.evidence"
    )
    evidence = read_json(evidence_path)
    require(evidence.get("phase") == phase, f"{phase} evidence phase changed")
    require(evidence.get("verdict") == "PASS", f"{phase} evidence is not PASS")
    payload = evidence.get("evidence")
    require(isinstance(payload, dict), f"{phase} evidence payload is missing")
    return payload


def displayed(command: list[str]) -> list[str]:
    root = str(PROJECT_ROOT.resolve())
    return [part.replace(root, ".") for part in command]


def validate_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    commands_value = payload.get("commands")
    require(
        isinstance(commands_value, list) and len(commands_value) == 7,
        "runtime command set changed",
    )
    environment = payload.get("environment")
    require(isinstance(environment, dict), "runtime environment observation is missing")
    packages = environment.get("packages")
    require(isinstance(packages, dict) and packages, "runtime package set is missing")
    probe_script = (
        "import importlib.metadata as metadata, json, platform; "
        f"names={list(packages)!r}; "
        "print(json.dumps({'python': platform.python_version(), "
        "'implementation': platform.python_implementation(), "
        "'platform': platform.platform(), "
        "'packages': {name: metadata.version(name) for name in names}}, "
        "sort_keys=True))"
    )
    python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    registered = [
        ["uvx", "uv@0.12.5", "sync", "--frozen"],
        ["uvx", "uv@0.12.5", "lock", "--check"],
        [str(PROJECT_ROOT / ".venv" / "Scripts" / "ruff.exe"), "check", "."],
        [str(PROJECT_ROOT / ".venv" / "Scripts" / "mypy.exe")],
        [str(python), "-m", "compileall", "-q", "src", "scripts", "verify"],
        [str(python), "-c", probe_script],
        [str(python), "verify/runtime_correctness.py"],
    ]
    commands = [
        validate_command(
            value,
            expected=displayed(expected),
            cwd=".",
            location=f"runtime[{index}]",
            require=require,
        )
        for index, (value, expected) in enumerate(
            zip(commands_value, registered, strict=True)
        )
    ]
    validate_ruff_output(commands[2], require=require)
    validate_mypy_output(commands[3], require=require)
    require(
        last_json_line(
            commands[5], location="runtime environment probe", require=require
        )
        == environment,
        "runtime environment output differs from its retained document",
    )
    runtime = validate_runtime_document(
        last_json_line(commands[6], location="runtime verifier", require=require),
        require=require,
    )
    require(
        runtime == payload.get("runtime"),
        "runtime parsed output differs from its retained summary",
    )
    return {"commands": len(commands), "checks": 9, "verdict": "PASS"}


def validate_export_snapshots(
    payload: dict[str, Any], product: dict[str, Any]
) -> dict[str, Any]:
    snapshots = payload.get("snapshots")
    require(isinstance(snapshots, dict), "integration snapshots are missing")
    expected_roles = {
        "aware_delivery",
        "aware_final",
        "aware_staged",
        "control_delivery",
        "control_final",
        "control_staged",
        "product",
        "signoz_access",
        "source_facts",
    }
    require(set(snapshots) == expected_roles, "integration snapshot role set changed")
    for mode in ("aware", "control"):
        final = read_json(
            referenced_path(snapshots[f"{mode}_final"], location=f"{mode}.final")
        )
        staged_reference = ArtifactReference.from_document(
            snapshots[f"{mode}_staged"], location=f"{mode}.staged"
        )
        delivery_reference = ArtifactReference.from_document(
            snapshots[f"{mode}_delivery"], location=f"{mode}.delivery"
        )
        staged = read_json(
            referenced_path(staged_reference.as_dict(), location=f"{mode}.staged")
        )
        delivery = read_json(
            referenced_path(delivery_reference.as_dict(), location=f"{mode}.delivery")
        )
        require(
            final.get("mode") == mode
            and final.get("run_id") == product.get("runId")
            and final.get("span_count") == 5
            and staged.get("spans") == final.get("spans"),
            f"{mode} final/staged export mismatch",
        )
        otlp = final.get("otlp")
        require(isinstance(otlp, dict), f"{mode} final OTLP proof is missing")
        embedded_staged = ArtifactReference.from_document(
            otlp.get("staged_envelope"), location=f"{mode}.embedded_staged"
        )
        embedded_delivery = ArtifactReference.from_document(
            otlp.get("delivery_receipt"), location=f"{mode}.embedded_delivery"
        )
        require(
            embedded_staged.sha256 == staged_reference.sha256
            and embedded_staged.bytes == staged_reference.bytes
            and embedded_delivery.sha256 == delivery_reference.sha256
            and embedded_delivery.bytes == delivery_reference.bytes,
            f"{mode} final envelope does not bind its retained snapshots",
        )
        require(
            otlp.get("attempted") is True
            and otlp.get("acknowledged") is True
            and otlp.get("state") == "DELIVERED"
            and otlp.get("result") == "SUCCESS"
            and otlp.get("cleanup_state") == "SUCCESS",
            f"{mode} final envelope lacks acknowledged clean delivery",
        )
        require(
            delivery.get("attempted") is True
            and delivery.get("acknowledged") is True
            and delivery.get("state") == "DELIVERED"
            and delivery.get("exporter_result") == "SUCCESS"
            and delivery.get("cleanup_state") == "SUCCESS",
            f"{mode} delivery receipt is not an acknowledged clean delivery",
        )
    access = read_json(
        referenced_path(snapshots["signoz_access"], location="signoz_access")
    )
    require(
        access.get("service_account") == "relay-otel-query"
        and access.get("service_account_status") == "active"
        and access.get("role") == "signoz-viewer"
        and access.get("roles") == ["signoz-viewer"]
        and access.get("identity_endpoint") == "/api/v1/service_accounts/me"
        and access.get("identity_http_status") == 200
        and access.get("key_validated") is True,
        "retained service-account identity proof is invalid",
    )
    source_facts = read_json(
        referenced_path(snapshots["source_facts"], location="source_facts")
    )
    journal_observation = source_facts.get("journal_phase_time_observation")
    effect_observation = source_facts.get("effect_phase_time_observation")
    ground_truth = product.get("groundTruth")
    product_journal = product.get("journal")
    provenance = product.get("provenance")
    require(
        source_facts.get("schema_version") == 1
        and source_facts.get("run_id") == product.get("runId")
        and isinstance(journal_observation, dict)
        and isinstance(effect_observation, dict)
        and isinstance(ground_truth, dict)
        and isinstance(product_journal, dict)
        and isinstance(provenance, dict),
        "retained sanitized source facts have an invalid shape",
    )
    require(
        source_facts.get("sanitized_journal_events") == product_journal.get("events")
        and journal_observation.get("observed_path") == provenance.get("journalPath")
        and journal_observation.get("observed_sha256")
        == provenance.get("journalSha256")
        and isinstance(journal_observation.get("observed_bytes"), int)
        and journal_observation["observed_bytes"] > 0,
        "retained journal source observation differs from the product",
    )
    effect_facts = source_facts.get("sanitized_effect_facts")
    expected_effect_path = (
        f"receipts/work/product-runs/{product['runId']}/effects.sqlite"
    )
    require(
        isinstance(effect_facts, dict)
        and effect_facts.get("provider_attempts")
        == ground_truth.get("providerAttempts")
        and effect_facts.get("business_executions")
        == ground_truth.get("businessExecutions")
        and isinstance(effect_facts.get("idempotency_key_sha256"), str)
        and len(effect_facts["idempotency_key_sha256"]) == 64
        and all(
            character in "0123456789abcdef"
            for character in effect_facts["idempotency_key_sha256"]
        )
        and effect_observation.get("observed_path") == expected_effect_path
        and isinstance(effect_observation.get("observed_sha256"), str)
        and len(effect_observation["observed_sha256"]) == 64
        and isinstance(effect_observation.get("observed_bytes"), int)
        and effect_observation["observed_bytes"] > 0,
        "retained effect source observation differs from the registered product facts",
    )
    return {"snapshot_roles": len(snapshots), "verdict": "PASS"}


def validate_stack_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    image_document = read_json(PROJECT_ROOT / "deploy" / "IMAGE-PINS.json")
    image_pins = image_document.get("images")
    require(isinstance(image_pins, dict), "authoritative image pins are missing")
    compose_hashes = payload.get("compose_hashes")
    inventory = payload.get("container_inventory")
    require(isinstance(compose_hashes, dict), "retained Compose hashes are missing")
    require(isinstance(inventory, dict), "retained container inventory is missing")
    require(
        set(compose_hashes) == set(EXPECTED_STACK)
        and set(inventory) == set(EXPECTED_STACK),
        "retained Compose service set changed",
    )
    compose_hash_command = payload.get("compose_hash_command")
    names_command = payload.get("container_names_command")
    inspect_command = payload.get("container_inspect_command")
    require(isinstance(compose_hash_command, dict), "Compose hash command is missing")
    compose_vector = compose_hash_command.get("command")
    require(
        isinstance(compose_vector, list)
        and compose_vector
        and isinstance(compose_vector[0], str)
        and Path(compose_vector[0]).name.lower() in {"docker", "docker.exe"},
        "Compose executable is not Docker",
    )
    docker = compose_vector[0]
    docker_prefix = [
        docker,
        "--config",
        displayed([str(PROJECT_ROOT / "receipts" / "work" / "docker-config")])[0],
    ]
    compose_prefix = [
        *docker_prefix,
        "compose",
        "--env-file",
        "<host-local-signoz-env>",
        "-f",
        "deploy/pours/deployment/compose.yaml",
        "-f",
        "deploy/root-user.override.yaml",
    ]
    validate_command(
        payload.get("compose"),
        expected=[*compose_prefix, "config", "--quiet"],
        cwd=".",
        location="Compose render",
        require=require,
    )
    compose_hash_command = validate_command(
        compose_hash_command,
        expected=[*compose_prefix, "config", "--hash", "*"],
        cwd=".",
        location="Compose hash",
        require=require,
    )
    names_command = validate_command(
        names_command,
        expected=[
            *docker_prefix,
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.project=relay-otel-signoz",
            "--format",
            "{{.Names}}",
        ],
        cwd=".",
        location="container names",
        require=require,
    )
    inspect_format = (
        '{{.Name}}|{{index .Config.Labels "com.docker.compose.service"}}|'
        "{{.Config.Image}}|{{.Image}}|{{.State.Status}}|{{.State.ExitCode}}|"
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|"
        '{{index .Config.Labels "com.docker.compose.config-hash"}}'
    )
    expected_container_names = sorted(
        str(spec["container"]) for spec in EXPECTED_STACK.values()
    )
    inspect_command = validate_command(
        inspect_command,
        expected=[
            *docker_prefix,
            "inspect",
            "--format",
            inspect_format,
            *expected_container_names,
        ],
        cwd=".",
        location="container inventory",
        require=require,
    )
    hash_lines = [
        line
        for line in str(compose_hash_command["stdout"]).splitlines()
        if line.strip()
    ]
    require(
        len(hash_lines) == len(EXPECTED_STACK)
        and all(len(line.split(maxsplit=1)) == 2 for line in hash_lines),
        "Compose hash output cardinality changed",
    )
    observed_hashes = dict(line.split(maxsplit=1) for line in hash_lines)
    require(observed_hashes == compose_hashes, "Compose hash observation was altered")
    observed_name_lines = [
        line.strip()
        for line in str(names_command["stdout"]).splitlines()
        if line.strip()
    ]
    observed_names = set(observed_name_lines)
    expected_names = {spec["container"] for spec in EXPECTED_STACK.values()}
    require(
        len(observed_name_lines) == len(expected_names)
        and observed_names == expected_names,
        "retained container name set changed",
    )
    parsed_inventory: dict[str, dict[str, object]] = {}
    for line in str(inspect_command["stdout"]).splitlines():
        parts = line.split("|")
        require(len(parts) == 8, "retained Docker inspect output is malformed")
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
        require(service in EXPECTED_STACK, "retained Docker service is unexpected")
        require(
            service not in parsed_inventory, "retained Docker service was duplicated"
        )
        expected = EXPECTED_STACK[service]
        repository = expected["image"]
        digest = image_pins.get(repository)
        require(isinstance(digest, str), f"image pin missing for {repository}")
        lifecycle = expected["lifecycle"]
        exit_code = int(raw_exit_code)
        require(
            raw_name.removeprefix("/") == expected["container"]
            and configured_image == f"{repository}@{digest}"
            and image_id == digest
            and config_hash == compose_hashes[service],
            f"retained image/config identity changed for {service}",
        )
        if lifecycle == "running_healthy":
            require(
                state == "running" and exit_code == 0 and docker_health == "healthy",
                f"retained healthy-service state changed for {service}",
            )
        else:
            require(
                state == "exited" and exit_code == 0 and docker_health == "none",
                f"retained one-shot state changed for {service}",
            )
        parsed_inventory[service] = {
            "config_hash": config_hash,
            "configured_image": configured_image,
            "container": expected["container"],
            "docker_health": docker_health,
            "exit_code": exit_code,
            "image_id": image_id,
            "lifecycle": lifecycle,
            "state": state,
        }
    require(parsed_inventory == inventory, "retained container inventory was altered")

    port_command_value = payload.get("port_inspect_command")
    require(isinstance(port_command_value, dict), "retained port inspection is missing")
    raw_port_vector = port_command_value.get("command")
    require(
        isinstance(raw_port_vector, list)
        and raw_port_vector
        and isinstance(raw_port_vector[0], str)
        and raw_port_vector[0] == docker,
        "retained port inspection executable is not Docker",
    )
    port_command = validate_command(
        port_command_value,
        expected=[
            *docker_prefix,
            "inspect",
            "--format",
            "{{.Name}}|{{json .NetworkSettings.Ports}}",
            *expected_container_names,
        ],
        cwd=".",
        location="Docker port inspection",
        require=require,
    )
    parsed_ports: list[dict[str, str]] = []
    observed_port_containers: set[str] = set()
    for line in str(port_command["stdout"]).splitlines():
        raw_name, separator, raw_ports = line.partition("|")
        require(separator == "|", "retained Docker port output is malformed")
        name = raw_name.removeprefix("/")
        require(
            name in expected_container_names and name not in observed_port_containers,
            "retained Docker port container set is invalid",
        )
        observed_port_containers.add(name)
        try:
            port_map: Any = json.loads(raw_ports)
        except json.JSONDecodeError as exc:
            raise SemanticEvidenceError("retained Docker port map is not JSON") from exc
        require(isinstance(port_map, dict), "retained Docker port map is not an object")
        for container_port, bindings in port_map.items():
            require(
                isinstance(container_port, str), "retained container port is invalid"
            )
            if bindings is None:
                continue
            require(isinstance(bindings, list), "retained Docker bindings are invalid")
            for binding in bindings:
                require(
                    isinstance(binding, dict)
                    and set(binding) == {"HostIp", "HostPort"}
                    and isinstance(binding.get("HostIp"), str)
                    and isinstance(binding.get("HostPort"), str),
                    "retained Docker binding is malformed",
                )
                parsed_ports.append(
                    {
                        "container": name,
                        "container_port": container_port,
                        "host_ip": binding["HostIp"],
                        "host_port": binding["HostPort"],
                    }
                )
    parsed_ports.sort(
        key=lambda value: (
            value["container"],
            value["container_port"],
            value["host_ip"],
            value["host_port"],
        )
    )
    expected_ports = [
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
    ]
    require(
        observed_port_containers == set(expected_container_names)
        and parsed_ports == expected_ports
        and payload.get("published_ports") == expected_ports,
        "retained Docker bindings differ from the exact loopback-only set",
    )

    histogram_command = validate_command(
        payload.get("histogram_command"),
        expected=[
            *docker_prefix,
            "exec",
            "relay-otel-signoz-telemetrystore-clickhouse-0-0",
            "sh",
            "-ec",
            "sha256sum /var/lib/clickhouse/user_scripts/histogramQuantile; "
            "wc -c < /var/lib/clickhouse/user_scripts/histogramQuantile",
        ],
        cwd=".",
        location="histogram-helper inspection",
        require=require,
    )
    histogram = payload.get("histogram_helper")
    require(
        isinstance(histogram, dict),
        "retained histogram-helper observation is missing",
    )
    histogram_lines = str(histogram_command["stdout"]).splitlines()
    require(len(histogram_lines) == 2, "histogram-helper output changed")
    observed_digest = histogram_lines[0].split(maxsplit=1)[0]
    observed_bytes = int(histogram_lines[1].strip())
    histogram_document = read_json(PROJECT_ROOT / "deploy" / "HISTOGRAM-PINS.json")
    artifacts = histogram_document.get("artifacts")
    require(isinstance(artifacts, dict), "authoritative histogram pins are missing")
    amd64 = artifacts.get("linux_amd64")
    require(isinstance(amd64, dict), "authoritative amd64 histogram pin is missing")
    require(
        observed_digest == amd64.get("binary_sha256")
        and observed_bytes == amd64.get("binary_bytes")
        and histogram
        == {
            "binary_bytes": observed_bytes,
            "binary_sha256": observed_digest,
            "container_path": "/var/lib/clickhouse/user_scripts/histogramQuantile",
            "platform": "linux_amd64",
        },
        "retained histogram-helper proof differs from its authoritative pin",
    )
    return {
        "services": len(inventory),
        "running_healthy": sum(
            1
            for value in inventory.values()
            if isinstance(value, dict) and value.get("lifecycle") == "running_healthy"
        ),
        "verdict": "PASS",
    }


def validate_integration(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    product_path = referenced_path(
        payload.get("product_snapshot"), location="integration.product_snapshot"
    )
    product = validate_document(read_json(product_path))
    require(product.get("schemaVersion") == 2, "integration product is not v2")
    ground_truth = product.get("groundTruth")
    require(
        isinstance(ground_truth, dict)
        and ground_truth.get("providerAttempts") == 1
        and ground_truth.get("businessExecutions") == 1,
        "integration provider/durable-refund counts changed",
    )
    exports = validate_export_snapshots(payload, product)
    stack = validate_stack_evidence(payload)
    require(
        payload.get("health") == {"collector": 200, "signoz": 200},
        "retained health result changed",
    )
    require(
        payload.get("container_health")
        == {
            "relay-otel-signoz-ingester-1": "healthy",
            "relay-otel-signoz-signoz-0": "healthy",
            "relay-otel-signoz-telemetrykeeper-clickhousekeeper-0": "healthy",
            "relay-otel-signoz-telemetrystore-clickhouse-0-0": "healthy",
        },
        "retained Docker container health result changed",
    )
    service_command = validate_command(
        payload.get("service_account_command"),
        expected=[
            displayed([str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")])[0],
            "scripts/bootstrap_signoz_access.py",
        ],
        cwd=".",
        location="service-account validation",
        require=require,
    )
    require(
        service_command["stdout"].strip()
        in {
            "Reused and validated the host-local SigNoz service-account key.",
            "Created and validated a host-local SigNoz viewer service-account key.",
        },
        "service-account validation output changed",
    )
    return product, {
        "product_run_id": product["runId"],
        "exports": exports,
        "stack": stack,
        "container_health": payload["container_health"],
        "verdict": "PASS",
    }


def validate_publication(
    payload: dict[str, Any], integration_product: dict[str, Any]
) -> dict[str, Any]:
    featured_path = referenced_path(
        payload.get("featured"), location="publication.featured"
    )
    featured = validate_document(read_json(featured_path))
    require(
        featured == integration_product,
        "featured proof differs from integration product",
    )
    node = validate_command(
        payload.get("node_version"),
        expected=["node", "--version"],
        cwd=".",
        location="Node version",
        require=require,
    )
    pnpm = validate_command(
        payload.get("pnpm_version"),
        expected=["corepack", "pnpm", "--version"],
        cwd=".",
        location="pnpm version",
        require=require,
    )
    validate_command(
        payload.get("web_install"),
        expected=["corepack", "pnpm", "install", "--frozen-lockfile"],
        cwd="web",
        location="frozen web install",
        require=require,
    )
    pnpm_audit = validate_command(
        payload.get("pnpm_audit"),
        expected=["corepack", "pnpm", "audit", "--prod", "--json"],
        cwd="web",
        location="pnpm production audit",
        require=require,
    )
    expected_node = (PROJECT_ROOT / ".node-version").read_text(encoding="utf-8").strip()
    require(
        node["stdout"].strip() == f"v{expected_node}"
        and pnpm["stdout"].strip() == "11.16.0",
        "retained web toolchain versions changed",
    )
    validate_pnpm_audit(
        json_stdout(pnpm_audit, location="pnpm audit", require=require),
        require=require,
    )
    web_checks_value = payload.get("web_verify")
    require(
        isinstance(web_checks_value, dict)
        and set(web_checks_value) == {"build", "format", "lint", "typecheck"},
        "registered web check set changed",
    )
    web_commands = {
        "typecheck": validate_command(
            web_checks_value["typecheck"],
            expected=["corepack", "pnpm", "typecheck"],
            cwd="web",
            location="web typecheck",
            require=require,
        ),
        "lint": validate_command(
            web_checks_value["lint"],
            expected=[
                "corepack",
                "pnpm",
                "exec",
                "eslint",
                "--format",
                "json",
                ".",
            ],
            cwd="web",
            location="web lint",
            require=require,
        ),
        "format": validate_command(
            web_checks_value["format"],
            expected=["corepack", "pnpm", "format:check"],
            cwd="web",
            location="web format",
            require=require,
        ),
        "build": validate_command(
            web_checks_value["build"],
            expected=["corepack", "pnpm", "build"],
            cwd="web",
            location="web build",
            require=require,
        ),
    }
    validate_eslint(
        json_stdout(web_commands["lint"], location="ESLint", require=require),
        require=require,
    )
    validate_web_text_outputs(web_commands, require=require)

    browser_path = referenced_path(
        payload.get("browser"), location="publication.browser"
    )
    browser = read_json(browser_path)
    require(
        browser == payload.get("browser_summary"),
        "browser summary differs from artifact",
    )
    require(
        payload.get("browser_assurance")
        == {
            "kind": "hash-bound-operator-observation",
            "release_audit_replays_browser": False,
            "release_audit_revalidates": [
                "artifact-hashes-and-sizes",
                "featured-proof-binding",
                "registered-observation-schema",
                "screenshot-references",
            ],
        },
        "browser assurance boundary changed",
    )
    browser_featured = ArtifactReference.from_document(
        browser.get("featured"), location="publication.browser.featured"
    )
    publication_featured = ArtifactReference.from_document(
        payload.get("featured"), location="publication.featured"
    )
    require(
        browser_featured == publication_featured,
        "browser observation is bound to another featured proof",
    )
    require(
        browser.get("verdict") == "PASS"
        and browser.get("console_errors") == 0
        and browser.get("page_overflow") is False
        and browser.get("keyboard_navigation") is True
        and browser.get("reduced_motion") is True,
        "retained browser evidence is incomplete",
    )
    require(
        browser.get("origin_security")
        == {
            "cross_site_status": 403,
            "https_origin_status": 403,
            "missing_origin_status": 403,
            "same_origin_unavailable_status": 503,
        }
        and browser.get("status_surfaces")
        == {
            "evidence": True,
            "lineage": True,
            "runner": True,
            "signoz": True,
        }
        and browser.get("keyboard")
        == {
            "disabled_state": True,
            "error_state": True,
            "internal_table_scroll": True,
            "skip_link": True,
            "visible_focus": True,
        },
        "retained browser security/status/keyboard evidence changed",
    )
    viewports = browser.get("viewports")
    require(
        isinstance(viewports, list)
        and {entry.get("width") for entry in viewports if isinstance(entry, dict)}
        == {375, 768, 1024, 1440}
        and all(
            isinstance(entry, dict) and entry.get("page_overflow") is False
            for entry in viewports
        ),
        "retained browser viewport evidence changed",
    )
    routes = browser.get("routes")
    require(
        isinstance(routes, list)
        and {
            "/",
            "/architecture",
            "/contract",
            "/runs",
            "/spec",
            f"/runs/{featured['runId']}",
        }.issubset(set(routes)),
        "retained browser route evidence changed",
    )
    screenshot_values = browser.get("screenshots")
    require(
        isinstance(screenshot_values, list) and len(screenshot_values) >= 4,
        "retained browser screenshot set is incomplete",
    )
    for index, screenshot in enumerate(screenshot_values):
        referenced_path(
            screenshot, location=f"publication.browser.screenshots[{index}]"
        )

    export_command_value = payload.get("python_dependency_export")
    require(isinstance(export_command_value, dict), "dependency export is missing")
    export_vector = export_command_value.get("command")
    require(
        isinstance(export_vector, list)
        and len(export_vector) == 10
        and export_vector[:9]
        == [
            "uvx",
            "uv@0.12.5",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements.txt",
            "--output-file",
        ]
        and isinstance(export_vector[9], str),
        "dependency export command vector changed",
    )
    requirements_display = export_vector[9]
    require(
        export_vector[10:] == []
        and requirements_display.replace("\\", "/").startswith(
            "./receipts/work/relay-otel-audit-"
        )
        and requirements_display.replace("\\", "/").endswith(
            "/runtime-requirements.txt"
        ),
        "dependency export target escaped its registered temporary directory",
    )
    validate_command(
        export_command_value,
        expected=export_vector,
        cwd=".",
        location="Python dependency export",
        require=require,
    )
    dependency_audit = validate_command(
        payload.get("python_dependency_audit"),
        expected=[
            displayed([str(PROJECT_ROOT / ".venv" / "Scripts" / "pip-audit.exe")])[0],
            "--strict",
            "--requirement",
            requirements_display,
            "--format",
            "json",
        ],
        cwd=".",
        location="Python dependency audit",
        require=require,
    )
    validate_pip_audit(
        json_stdout(dependency_audit, location="pip-audit", require=require),
        require=require,
    )
    bandit = validate_command(
        payload.get("python_source_security_scan"),
        expected=[
            displayed([str(PROJECT_ROOT / ".venv" / "Scripts" / "bandit.exe")])[0],
            "-r",
            "src",
            "scripts",
            "-lll",
            "-iii",
            "-f",
            "json",
        ],
        cwd=".",
        location="Python source security scan",
        require=require,
    )
    validate_bandit(
        json_stdout(bandit, location="Bandit", require=require), require=require
    )
    package = payload.get("package")
    require(isinstance(package, dict), "retained package evidence is missing")
    distributions = package.get("distributions")
    stored_contents = package.get("contents")
    require(
        isinstance(distributions, dict)
        and isinstance(stored_contents, dict)
        and len(distributions) == 2,
        "retained package distribution set is invalid",
    )
    recomputed_contents: dict[str, list[str]] = {}
    wheel_path: Path | None = None
    for name, reference in distributions.items():
        require(isinstance(name, str), "package distribution name is invalid")
        distribution_path = referenced_path(
            reference, location=f"publication.package.distributions.{name}"
        )
        require(distribution_path.name == name, "package distribution name changed")
        recomputed_contents[name] = sorted(package_contents(distribution_path))
        if name.endswith(".whl"):
            wheel_path = distribution_path
    require(
        recomputed_contents == stored_contents and wheel_path is not None,
        "retained package content manifest differs from its distributions",
    )
    sdist_entries = next(
        entries
        for name, entries in recomputed_contents.items()
        if name.endswith(".tar.gz")
    )
    wheel_entries = recomputed_contents[wheel_path.name]
    require(
        any(name.endswith("/README.md") for name in sdist_entries)
        and any(name.endswith("/LICENSE") for name in sdist_entries)
        and any("licenses/LICENSE" in name for name in wheel_entries)
        and any("/src/relay/__init__.py" in name for name in sdist_entries)
        and any("/src/relay_otel/__init__.py" in name for name in sdist_entries)
        and "relay/__init__.py" in wheel_entries
        and "relay_otel/__init__.py" in wheel_entries,
        "retained package omitted README, LICENSE, or registered source packages",
    )
    with zipfile.ZipFile(wheel_path) as wheel_archive:
        entry_points_name = next(
            name
            for name in wheel_archive.namelist()
            if name.endswith(".dist-info/entry_points.txt")
        )
        actual_entry_points = wheel_archive.read(entry_points_name).decode("utf-8")
    require(
        package.get("entry_points") == actual_entry_points
        and "relay-otel = relay_otel.cli:main" in actual_entry_points,
        "retained wheel console entry point changed",
    )
    build_record_value = package.get("command")
    require(isinstance(build_record_value, dict), "package build command is missing")
    build_vector = build_record_value.get("command")
    require(
        isinstance(build_vector, list)
        and len(build_vector) == 5
        and build_vector[:4] == ["uvx", "uv@0.12.5", "build", "--out-dir"]
        and isinstance(build_vector[4], str),
        "package build command vector changed",
    )
    build_directory = build_vector[4]
    require(
        build_directory.replace("\\", "/").startswith(
            "./receipts/work/relay-otel-build-"
        ),
        "package build directory escaped its registered temporary root",
    )
    validate_command(
        build_record_value,
        expected=build_vector,
        cwd=".",
        location="package build",
        require=require,
    )
    wheel_name = wheel_path.name
    path_separator = "\\" if "\\" in build_directory else "/"
    installed_target = f"{build_directory}{path_separator}installed-wheel"
    built_wheel = f"{build_directory}{path_separator}{wheel_name}"
    validate_command(
        package.get("install_command"),
        expected=[
            "uvx",
            "uv@0.12.5",
            "pip",
            "install",
            "--target",
            installed_target,
            "--no-deps",
            built_wheel,
        ],
        cwd=".",
        location="built-wheel install",
        require=require,
    )
    smoke_site = installed_target
    if smoke_site.startswith(".\\") or smoke_site.startswith("./"):
        smoke_site = smoke_site[2:]
    smoke_program = (
        "import json,pathlib,sys;"
        f"site=pathlib.Path({json.dumps(smoke_site)}).resolve();"
        "sys.path.insert(0,str(site));"
        "import relay_otel.cli as cli;"
        "root=cli.resolve_project_root(pathlib.Path('.'));"
        "module=pathlib.Path(cli.__file__).resolve();"
        "assert module.is_relative_to(site);"
        "assert root == pathlib.Path('.').resolve();"
        "print(json.dumps({'module_under_installed_target':True,"
        "'project_root_verified':True},sort_keys=True))"
    )
    smoke_record = validate_command(
        package.get("installed_smoke_command"),
        expected=[
            displayed([str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")])[0],
            "-I",
            "-c",
            smoke_program,
        ],
        cwd=".",
        location="installed-wheel smoke",
        require=require,
    )
    smoke = json_stdout(smoke_record, location="installed-wheel smoke", require=require)
    require(
        smoke
        == {
            "module_under_installed_target": True,
            "project_root_verified": True,
        },
        "installed-wheel smoke observation changed",
    )
    return {
        "featured_run_id": featured["runId"],
        "package_distributions": len(distributions),
        "browser_assurance": "hash-bound-operator-observation",
        "verdict": "PASS",
    }


def semantic_audit(validated: ValidatedLineage) -> dict[str, Any]:
    runtime = validate_runtime(phase_evidence(validated, "01-runtime"))
    product, integration = validate_integration(
        phase_evidence(validated, "02-integration")
    )
    publication = validate_publication(
        phase_evidence(validated, "03-publication"), product
    )
    return {
        "runtime": runtime,
        "integration": integration,
        "publication": publication,
    }


def failure_receipt(error: Exception) -> dict[str, Any]:
    if isinstance(error, LineageValidationError):
        reason_code = error.code.lower()
        details: object = error.details
    else:
        reason_code = "semantic_evidence_invalid"
        details = {"type": type(error).__name__}
    return {
        "schema_version": 2,
        "generated_at": utc_now(),
        "verdict": "FAIL",
        "criteria": {
            "current_lineage_validated": False,
            "all_current_artifact_hashes_recomputed": False,
            "ordered_phase_chain_recomputed": False,
            "retained_phase_semantics_re_evaluated": False,
        },
        "lineage": {
            "ready": False,
            "reason_code": reason_code,
            "detail": str(error),
            "reason_details": details,
        },
        "source_files": {
            "verify/release_audit.py": sha256_file(Path(__file__).resolve())
        },
    }


def main() -> int:
    try:
        validated = validate_current_lineage(PROJECT_ROOT)
        semantics = semantic_audit(validated)
    except (
        LineageValidationError,
        SemanticEvidenceError,
        KeyError,
        ValueError,
    ) as error:
        receipt = failure_receipt(error)
        write_json_atomic(OUTPUT, receipt, allow_replace=True)
        print(json.dumps({"receipt": str(OUTPUT), "verdict": "FAIL"}, indent=2))
        return 1

    receipt = {
        "schema_version": 2,
        "generated_at": utc_now(),
        "verdict": "PASS",
        "criteria": {
            "current_lineage_validated": True,
            "all_current_artifact_hashes_recomputed": True,
            "ordered_phase_chain_recomputed": True,
            "retained_phase_semantics_re_evaluated": True,
        },
        "lineage": {"ready": True, **validated.provenance()},
        "semantic_evidence": semantics,
        "source_files": {
            "verify/release_audit.py": sha256_file(Path(__file__).resolve())
        },
    }
    write_json_atomic(OUTPUT, receipt, allow_replace=True)
    print(json.dumps({"receipt": str(OUTPUT), "verdict": "PASS"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
