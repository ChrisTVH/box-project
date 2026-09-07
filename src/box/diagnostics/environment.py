"""Local runtime environment details."""

from __future__ import annotations

import platform
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Environment:
    """Non-sensitive platform details useful for local diagnostics."""

    system: str
    release: str
    machine: str


def collect_environment() -> Environment:
    """Return local operating-system information without network access."""
    return Environment(platform.system(), platform.release(), platform.machine())
