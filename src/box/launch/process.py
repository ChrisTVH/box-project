"""NW.js process execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from box.errors import LaunchError


def run_process(command: list[str], cwd: Path | None = None) -> int:
    """Run a game runtime and return its exit status without invoking a shell."""
    try:
        return subprocess.run(command, check=False, cwd=cwd).returncode
    except OSError as exc:
        raise LaunchError(f"cannot start game runtime: {exc}") from exc
