"""Fetch the pinned official Foundry CLI into ignored local work tooling."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.17"
EXPECTED_SHA256 = "625c7985b8ac6f3e4a99576c1dceaa4fa46fa4a54b2c53f515dff7f63da8dd4a"
URL = (
    "https://github.com/SigNoz/foundry/releases/download/"
    f"v{VERSION}/foundry_windows_amd64.tar.gz"
)


def main() -> int:
    output = PROJECT_ROOT / "receipts" / "work" / "tools" / f"foundry-{VERSION}"
    executable = output / "foundry_windows_amd64" / "bin" / "foundryctl.exe"
    if executable.is_file():
        print(
            json.dumps(
                {"path": str(executable), "version": VERSION, "reused": True}, indent=2
            )
        )
        return 0
    output.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(URL, headers={"User-Agent": "relay-otel-build"})
    with urllib.request.urlopen(request, timeout=120) as response:
        archive = response.read()
    actual = hashlib.sha256(archive).hexdigest()
    if actual != EXPECTED_SHA256:
        raise ValueError(f"Foundry archive digest mismatch: {actual}")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        for member in bundle.getmembers():
            destination = (output / member.name).resolve()
            if (
                output.resolve() not in destination.parents
                and destination != output.resolve()
            ):
                raise ValueError(f"unsafe archive member: {member.name}")
        bundle.extractall(output, filter="data")
    if not executable.is_file():
        raise FileNotFoundError(
            f"Foundry executable missing after extraction: {executable}"
        )
    print(
        json.dumps(
            {
                "path": str(executable),
                "version": VERSION,
                "archive_sha256": actual,
                "reused": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
