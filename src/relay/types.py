"""Shared type aliases at relay's JSON serialization boundary."""

from __future__ import annotations

from typing import Any, TypeAlias

Document: TypeAlias = dict[str, Any]
State: TypeAlias = Document
