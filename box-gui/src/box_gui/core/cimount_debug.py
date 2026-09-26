"""Environment switch for the backend ci-mount daemon trace.

The ci-mount FUSE daemon (``box.launch.cimount``) appends metadata-only
lines -- lookup/readdir names, inodes, and mtimes, never file contents --
to the file named by the ``BOX_CIMOUNT_DEBUG_LOG`` environment variable,
and logs nothing at all when that variable is unset or empty. The daemon is
forked from the launcher process, so exporting the variable here is enough
for the next game launch to inherit it: no ``box.api`` surface is involved
and none is added for a diagnosis-only aid.

This module owns only the environment half of the feature. The persisted
choice lives in ``core.defaults`` (``ci_mount_debug_enabled`` and
``ci_mount_debug_log``) and the widgets live in the settings dialog.

Disabling REMOVES the variable instead of blanking it, so the daemon's
"unset means no logging" contract keeps holding exactly as it does for a
launch started from a clean shell.

Toolkit-free: nothing here imports Gtk/Adw.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from box.paths import AppPaths

__all__ = [
    "CIMOUNT_DEBUG_LOG_ENV",
    "CIMOUNT_DEBUG_LOG_FILENAME",
    "apply_debug_log",
    "default_debug_log_path",
    "disable_debug_log",
    "enable_debug_log",
]

#: Env var read by the ci-mount daemon; unset (or empty) means no logging.
CIMOUNT_DEBUG_LOG_ENV = "BOX_CIMOUNT_DEBUG_LOG"

#: File name used under the launcher cache root when no path is chosen.
CIMOUNT_DEBUG_LOG_FILENAME = "ci-mount-debug.log"


def default_debug_log_path(paths: AppPaths) -> Path:
    """Return the default trace file, inside the launcher-owned cache root.

    ``$XDG_CACHE_HOME/box-rpg/`` already exists (``AppPaths.ensure`` creates
    it) and is removed only by the Settings Data page's full wipe, so an
    unconfigured trace never litters an unrelated directory.
    """
    return paths.cache_root / CIMOUNT_DEBUG_LOG_FILENAME


def enable_debug_log(path: Path | str) -> str:
    """Export the env var so the daemon appends its trace to ``path``.

    The value is expanded (``~``) and made absolute because the daemon
    resolves a relative path against its own working directory, and the
    parent directory is created best-effort with user-only permissions: the
    daemon opens the file with ``O_CREAT`` but never creates the directory,
    so a missing parent would silently drop the whole trace. A blank path
    raises instead of exporting an empty value, which the daemon reads as
    "no logging". Returns the exported value.
    """
    value = str(path).strip()
    if not value:
        raise ValueError("the ci-mount debug log needs a file path")
    target = os.path.abspath(os.path.expanduser(value))
    with contextlib.suppress(OSError):
        Path(target).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ[CIMOUNT_DEBUG_LOG_ENV] = target
    return target


def disable_debug_log() -> None:
    """Remove the env var, restoring the daemon's no-logging state."""
    os.environ.pop(CIMOUNT_DEBUG_LOG_ENV, None)


def apply_debug_log(enabled: bool, path: Path | str | None, paths: AppPaths) -> str | None:
    """Apply one stored choice to the environment and report the outcome.

    Enabling without a path (an empty or cleared entry) falls back to
    ``default_debug_log_path(paths)``; disabling removes the variable and
    returns None. ``paths`` is only consulted for that fallback, so an
    explicit ``path`` ignores it. The result is the value the next launch
    inherits, which is also what the settings row displays back.
    """
    if not enabled:
        disable_debug_log()
        return None
    chosen = str(path).strip() if path is not None else ""
    if not chosen:
        chosen = str(default_debug_log_path(paths))
    return enable_debug_log(chosen)
