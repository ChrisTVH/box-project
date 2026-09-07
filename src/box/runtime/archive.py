"""Safe extraction of NW.js tar archives."""

from __future__ import annotations

import tarfile
from pathlib import Path

from box.errors import RuntimeError


def extract_runtime(archive_path: Path, destination: Path) -> Path:
    """Extract an NW.js archive and return its single validated top-level directory."""
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(destination, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeError(f"cannot extract NW.js archive {archive_path}: {exc}") from exc
    entries = [entry for entry in destination.iterdir() if entry.is_dir()]
    if len(entries) != 1:
        raise RuntimeError("NW.js archive must contain exactly one top-level directory")
    return entries[0]
