"""Configuration migration boundary for future schema versions."""

from __future__ import annotations

from typing import Any


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Return configuration data at the current schema version."""
    return data
