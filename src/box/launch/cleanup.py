"""Cleanup for ephemeral launcher-owned sessions."""

from __future__ import annotations

import shutil
from pathlib import Path

from box.errors import ConfigurationError, LaunchError
from box.paths import AppPaths


def remove_session(paths: AppPaths, session_root: Path) -> None:
    """Delete only a resolved session directory inside the launcher cache."""
    try:
        paths.ensure_managed_session_path(session_root)
    except ConfigurationError as exc:
        raise LaunchError(str(exc)) from exc
    if session_root.exists() and not session_root.is_symlink():
        shutil.rmtree(session_root)
