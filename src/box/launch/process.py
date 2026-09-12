"""NW.js process execution."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from box.errors import LaunchError
from box.launch.sandbox import BWRAP, clean_environment
from box.utils.i18n import _


def runtime_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a fixed allowlist; no host environment values are inherited."""
    return clean_environment()


def run_process(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
    """Run a game runtime and return its exit status without invoking a shell."""
    if not command or command[0] != str(BWRAP):
        raise LaunchError("refusing to execute a runtime without Bubblewrap")
    try:
        return subprocess.run(
            command,
            check=False,
            cwd=cwd,
            pass_fds=pass_fds,
            env=runtime_environment(),
            stdin=subprocess.DEVNULL,
        ).returncode
    except OSError as exc:
        raise LaunchError(_("cannot start game runtime: {error}").format(error=exc)) from exc
