"""NW.js version and runtime-layout validation."""

from __future__ import annotations

import re
from pathlib import Path

from box.errors import RuntimeError

_VERSION = re.compile(r"^v\d+\.\d+\.\d+$")


def normalize_version(value: str) -> str:
    """Normalize an NW.js semantic version to its official v-prefixed form."""
    version = value if value.startswith("v") else f"v{value}"
    if not _VERSION.fullmatch(version):
        raise RuntimeError(f"invalid NW.js version: {value!r}")
    return version


def runtime_executable(root: Path) -> Path:
    """Validate a runtime directory and return its NW.js executable."""
    executable = root / "nw"
    if root.is_symlink() or executable.is_symlink() or not executable.is_file():
        raise RuntimeError(f"invalid NW.js runtime; executable is missing: {executable}")
    return executable
