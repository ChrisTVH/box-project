"""NW.js process execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from box.errors import LaunchError


def run_process(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
    """Run a game runtime and return its exit status without invoking a shell."""
    try:
        return subprocess.run(command, check=False, cwd=cwd, pass_fds=pass_fds).returncode
    except OSError as exc:
        raise LaunchError(f"cannot start game runtime: {exc}") from exc
