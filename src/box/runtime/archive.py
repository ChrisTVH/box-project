"""Safe extraction of NW.js tar archives."""

from __future__ import annotations

import os
import stat
import tarfile
from pathlib import Path

from box.errors import RuntimeError
from box.runtime.limits import extract_bounded
from box.runtime.security import validate_private_file, validate_runtime_links
from box.utils.i18n import _


def extract_runtime(archive_path: Path, destination: Path) -> Path:
    """Extract an NW.js archive and return its single validated top-level directory."""
    try:
        descriptor = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as archive_file:
            validate_private_file(archive_file.fileno())
            extract_bounded(archive_file, destination, _validate_layout)
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise RuntimeError(
            _("cannot extract NW.js archive {archive}: {error}").format(
                archive=archive_path, error=exc
            )
        ) from exc
    entries = [entry for entry in destination.iterdir() if entry.is_dir()]
    if len(entries) != 1:
        raise RuntimeError(_("NW.js archive must contain exactly one top-level directory"))
    return entries[0]


def extract_runtime_at(archive_descriptor: int, destination_descriptor: int) -> str:
    """Extract an NW.js archive between pinned directories and return its root name."""
    try:
        with os.fdopen(os.dup(archive_descriptor), "rb") as archive_file:
            validate_private_file(archive_file.fileno())
            extract_bounded(
                archive_file, Path(f"/proc/self/fd/{destination_descriptor}"), _validate_layout
            )
        os.lseek(destination_descriptor, 0, os.SEEK_SET)
        entries = tuple(os.scandir(destination_descriptor))
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise RuntimeError(_("cannot extract NW.js archive: {error}").format(error=exc)) from exc
    directories = tuple(entry for entry in entries if entry.is_dir(follow_symlinks=False))
    if len(directories) != 1:
        raise RuntimeError(_("NW.js archive must contain exactly one top-level directory"))
    root = directories[0]
    try:
        root_status = os.stat(root.name, dir_fd=destination_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(
            _("cannot validate extracted NW.js runtime: {error}").format(error=exc)
        ) from exc
    if not stat.S_ISDIR(root_status.st_mode):
        raise RuntimeError(_("NW.js archive must contain exactly one top-level directory"))
    return root.name


def _validate_layout(destination: Path) -> None:
    """Reject invalid layouts before publishing any extracted files."""
    directories = [
        entry for entry in destination.iterdir() if entry.is_dir() and not entry.is_symlink()
    ]
    if len(directories) != 1:
        raise RuntimeError(_("NW.js archive must contain exactly one top-level directory"))
    validate_runtime_links(directories[0])
