"""Cleanup for ephemeral launcher-owned sessions."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from box.errors import ConfigurationError, LaunchError
from box.paths import AppPaths
from box.utils.i18n import _


def remove_session(
    paths: AppPaths,
    session_root: Path,
    *,
    parent_descriptor: int | None = None,
    session_descriptor: int | None = None,
) -> None:
    """Delete only a resolved session directory inside the launcher cache."""
    try:
        paths.ensure_managed_session_path(session_root)
    except ConfigurationError as exc:
        raise LaunchError(str(exc)) from exc
    try:
        relative = session_root.absolute().relative_to(paths.sessions_root.absolute())
    except ValueError as exc:
        raise LaunchError(
            _("refusing to remove invalid session path: {path}").format(path=session_root)
        ) from exc
    if len(relative.parts) != 2:
        raise LaunchError(
            _("refusing to remove invalid session path: {path}").format(path=session_root)
        )
    owns_parent_descriptor = parent_descriptor is None
    try:
        if parent_descriptor is None:
            sessions_descriptor = paths.open_managed_cache_directory("sessions")
            try:
                parent_descriptor = os.open(
                    relative.parts[0],
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=sessions_descriptor,
                )
            finally:
                os.close(sessions_descriptor)
        try:
            metadata = os.stat(relative.parts[1], dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(metadata.st_mode):
            return
        if session_descriptor is not None:
            pinned = os.fstat(session_descriptor)
            if (metadata.st_dev, metadata.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise LaunchError(
                    _("session path changed before cleanup: {path}").format(path=session_root)
                )
        shutil.rmtree(relative.parts[1], dir_fd=parent_descriptor)
    except OSError as exc:
        raise LaunchError(
            _("cannot remove launch session {path}: {error}").format(path=session_root, error=exc)
        ) from exc
    finally:
        if owns_parent_descriptor and parent_descriptor is not None:
            os.close(parent_descriptor)
