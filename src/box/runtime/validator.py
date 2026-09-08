"""NW.js version and runtime-layout validation."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from box.errors import RuntimeError
from box.utils.i18n import _

_VERSION = re.compile(r"^v\d+\.\d+\.\d+$")


def normalize_version(value: str) -> str:
    """Normalize an NW.js semantic version to its official v-prefixed form."""
    version = value if value.startswith("v") else f"v{value}"
    if not _VERSION.fullmatch(version):
        raise RuntimeError(_("invalid NW.js version: {value!r}").format(value=value))
    return version


def runtime_executable(root: Path) -> Path:
    """Validate a runtime directory and return its NW.js executable."""
    executable = root / "nw"
    try:
        executable_status = os.stat(executable, follow_symlinks=False)
    except OSError:
        executable_status = None
    if (
        root.is_symlink()
        or executable_status is None
        or not stat.S_ISREG(executable_status.st_mode)
        or not executable_status.st_mode & 0o111
    ):
        raise RuntimeError(
            _("invalid NW.js runtime; executable is missing: {executable}").format(
                executable=executable
            )
        )
    return executable


def validate_runtime_executable_at(root_descriptor: int) -> None:
    """Validate a direct executable in a pinned runtime directory descriptor."""
    try:
        executable_status = os.stat("nw", dir_fd=root_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(_("invalid NW.js runtime; executable is missing: nw")) from exc
    if not stat.S_ISREG(executable_status.st_mode) or not executable_status.st_mode & 0o111:
        raise RuntimeError(_("invalid NW.js runtime; executable is missing: nw"))
