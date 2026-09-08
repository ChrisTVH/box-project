"""NW.js process execution."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from box.errors import LaunchError
from box.utils.i18n import _


def runtime_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Preserve desktop/game settings, excluding explicit runtime injection hooks.

    This is not an environment sandbox: PATH, HOME, display, audio and driver
    settings remain inherited for compatibility with desktop runtimes.
    """
    blocked = {
        "NODE_OPTIONS",
        "NODE_PATH",
        "NODE_REPL_EXTERNAL_MODULE",
        "NW_PRE_ARGS",
        "NW_ARGS",
        "ELECTRON_RUN_AS_NODE",
        "PYTHONPATH",
        "PYTHONHOME",
        "RUBYOPT",
        "RUBYLIB",
        "PERL5OPT",
        "PERL5LIB",
        "BASH_ENV",
        "ENV",
        "GCONV_PATH",
        "GTK_MODULES",
        "GTK3_MODULES",
        "GTK_PATH",
        "GIO_EXTRA_MODULES",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
    }
    environment = os.environ if source is None else source
    return {
        key: value
        for key, value in environment.items()
        if key not in blocked and not key.startswith(("LD_", "DYLD_"))
    }


def run_process(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
    """Run a game runtime and return its exit status without invoking a shell."""
    try:
        return subprocess.run(
            command, check=False, cwd=cwd, pass_fds=pass_fds, env=runtime_environment()
        ).returncode
    except OSError as exc:
        raise LaunchError(_("cannot start game runtime: {error}").format(error=exc)) from exc
