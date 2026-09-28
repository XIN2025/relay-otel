from __future__ import annotations

import json
import os
import secrets
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from relay_otel.runtime_paths import (
    prepare_private_secret_parent,
    secure_secret_file,
    signoz_secret_path,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRET_PATH = signoz_secret_path()
RECEIPT_PATH = PROJECT_ROOT / "receipts" / "work" / "signoz-access.json"
BASE_URL = "http://127.0.0.1:8080"
ROOT_EMAIL = "relay-otel@local.invalid"
SERVICE_ACCOUNT_NAME = "relay-otel-query"
ROLE_NAME = "signoz-viewer"


def read_values() -> dict[str, str]:
    if not SECRET_PATH.exists():
        raise FileNotFoundError(f"runtime secret file is missing: {SECRET_PATH}")
    values: dict[str, str] = {}
    for line in SECRET_PATH.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            values[key] = value
    return values


def request_json(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        BASE_URL + path,
        data=payload,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            content = response.read()
            return response.status, json.loads(content) if content else None
    except urllib.error.HTTPError as error:
        content = error.read()
        detail = content.decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(
            f"{method} {path} returned {error.code}: {detail}"
        ) from error


def write_secret_values(values: dict[str, str]) -> None:
    prepare_private_secret_parent(SECRET_PATH)
    ordered = ["SIGNOZ_ROOT_PASSWORD", "SIGNOZ_JWT_SECRET", "SIGNOZ_API_KEY"]
    contents = "".join(f"{key}={values[key]}\n" for key in ordered if key in values)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=SECRET_PATH.name + ".", suffix=".tmp", dir=SECRET_PATH.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, SECRET_PATH)
        secure_secret_file(SECRET_PATH)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_receipt(payload: dict[str, Any]) -> None:
    RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def validate_key(api_key: str) -> dict[str, Any] | None:
    try:
        status, document = request_json(
            "GET",
            "/api/v1/service_accounts/me",
            headers={"SIGNOZ-API-KEY": api_key},
        )
    except (RuntimeError, KeyError, TypeError):
        return None
    if status != 200 or not isinstance(document, dict):
        return None
    data = document.get("data")
    if not isinstance(data, dict):
        return None
    role_entries = data.get("serviceAccountRoles")
    if not isinstance(role_entries, list):
        return None
    try:
        roles = sorted({entry["role"]["name"] for entry in role_entries})
    except (KeyError, TypeError):
        return None
    if (
        data.get("name") != SERVICE_ACCOUNT_NAME
        or data.get("status") != "active"
        or roles != [ROLE_NAME]
    ):
        return None
    return {
        "identity_endpoint": "/api/v1/service_accounts/me",
        "identity_http_status": status,
        "service_account": data["name"],
        "service_account_status": data["status"],
        "roles": roles,
    }


def main() -> int:
    values = read_values()
    existing_key = values.get("SIGNOZ_API_KEY")
    existing_identity = validate_key(existing_key) if existing_key else None
    if existing_identity is not None:
        write_receipt(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "reused",
                **existing_identity,
                "role": ROLE_NAME,
                "key_validated": True,
                "key_persisted_in_host_local_env": True,
            }
        )
        print("Reused and validated the host-local SigNoz service-account key.")
        return 0

    root_password = values.get("SIGNOZ_ROOT_PASSWORD")
    if not root_password:
        raise ValueError("SIGNOZ_ROOT_PASSWORD is missing from the runtime env file")

    query = urllib.parse.urlencode({"email": ROOT_EMAIL, "ref": BASE_URL})
    _, context = request_json("GET", f"/api/v2/sessions/context?{query}")
    organizations = context["data"]["orgs"]
    if len(organizations) != 1:
        raise RuntimeError(
            f"expected one local organization, found {len(organizations)}"
        )
    org_id = organizations[0]["id"]
    _, session = request_json(
        "POST",
        "/api/v2/sessions/email_password",
        body={"email": ROOT_EMAIL, "password": root_password, "orgId": org_id},
    )
    bearer = {"Authorization": f"Bearer {session['data']['accessToken']}"}

    _, accounts_document = request_json(
        "GET", "/api/v1/service_accounts", headers=bearer
    )
    account = next(
        (
            candidate
            for candidate in accounts_document["data"]
            if candidate["name"] == SERVICE_ACCOUNT_NAME
            and candidate["status"] == "active"
        ),
        None,
    )
    account_created = account is None
    if account is None:
        _, created = request_json(
            "POST",
            "/api/v1/service_accounts",
            body={"name": SERVICE_ACCOUNT_NAME},
            headers=bearer,
        )
        account = created["data"]

    account_id = account["id"]
    _, account_document = request_json(
        "GET", f"/api/v1/service_accounts/{account_id}", headers=bearer
    )
    assigned_roles = {
        entry["role"]["name"]
        for entry in (account_document["data"].get("serviceAccountRoles") or [])
    }
    unexpected_roles = assigned_roles - {ROLE_NAME}
    if unexpected_roles:
        raise RuntimeError(
            "refusing to issue a query key for a service account with unexpected roles: "
            + ", ".join(sorted(unexpected_roles))
        )
    role_created = ROLE_NAME not in assigned_roles
    if role_created:
        _, roles_document = request_json("GET", "/api/v1/roles", headers=bearer)
        role_id = next(
            role["id"] for role in roles_document["data"] if role["name"] == ROLE_NAME
        )
        request_json(
            "POST",
            "/api/v1/service_account_roles",
            body={"serviceAccountId": account_id, "roleId": role_id},
            headers=bearer,
        )

    key_name = f"phase3-query-{secrets.token_hex(4)}"
    _, key_document = request_json(
        "POST",
        f"/api/v1/service_accounts/{account_id}/keys",
        body={"name": key_name, "expiresAt": 0},
        headers=bearer,
    )
    api_key = key_document["data"]["key"]
    created_identity = validate_key(api_key)
    if created_identity is None:
        raise RuntimeError(
            "new service-account key did not identify the exact active viewer account"
        )
    values["SIGNOZ_API_KEY"] = api_key
    write_secret_values(values)
    write_receipt(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "created",
            "organization_id": org_id,
            **created_identity,
            "service_account_id": account_id,
            "service_account_created": account_created,
            "role": ROLE_NAME,
            "role_assignment_created": role_created,
            "api_key_id": key_document["data"]["id"],
            "api_key_name": key_name,
            "key_validated": True,
            "key_persisted_in_host_local_env": True,
        }
    )
    print("Created and validated a host-local SigNoz viewer service-account key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
