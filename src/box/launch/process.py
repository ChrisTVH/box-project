"""NW.js process execution."""

from __future__ import annotations

import subprocess

from box.errors import LaunchError


def run_process(command: list[str]) -> int:
    """Run NW.js and return its exit status without invoking a shell."""
    try:
        return subprocess.run(command, check=False).returncode
    except OSError as exc:
        raise LaunchError(f"cannot start NW.js: {exc}") from exc
