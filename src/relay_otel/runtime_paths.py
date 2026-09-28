from __future__ import annotations

import os
from pathlib import Path


def signoz_secret_path() -> Path:

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        xdg_state_home = os.environ.get("XDG_STATE_HOME")
        base = (
            Path(xdg_state_home)
            if xdg_state_home and Path(xdg_state_home).is_absolute()
            else Path.home() / ".local" / "state"
        )
    return base / "relay-otel-poc" / "signoz-root.env"


def prepare_private_secret_parent(path: Path) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)


def secure_secret_file(path: Path) -> None:

    if os.name != "nt":
        path.chmod(0o600)
