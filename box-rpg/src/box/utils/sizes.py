"""Measure launcher-managed files and directories without following symlinks."""

from __future__ import annotations

import os
import stat
from pathlib import Path

__all__ = ["directory_size", "file_size", "format_size_decimal"]


def file_size(path: Path) -> int | None:
    """Return the lstat size of a file or symlink, or None when unavailable."""
    try:
        return path.stat(follow_symlinks=False).st_size
    except OSError:
        return None


def directory_size(path: Path) -> int | None:
    """Return the recursive size of a directory without following symlinks.

    Symlink entries contribute their own lstat size and are never traversed.
    Unreadable nested entries are skipped, while a missing or unreadable
    top-level path returns None.
    """
    try:
        top_stat = path.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISDIR(top_stat.st_mode):
        return top_stat.st_size
    try:
        with os.scandir(path) as iterator:
            entries = list(iterator)
    except OSError:
        return None
    total = 0
    for entry in entries:
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(entry_stat.st_mode):
            subtotal = directory_size(Path(entry.path))
            if subtotal is None:
                continue
            total += subtotal
        else:
            total += entry_stat.st_size
    return total


def format_size_decimal(num_bytes: int) -> str:
    """Format a byte count with base-10 units and one decimal above bytes."""
    if num_bytes < 1_000:
        return f"{num_bytes} B"
    if num_bytes < 1_000_000:
        return f"{num_bytes / 1_000:.1f} kB"
    if num_bytes < 1_000_000_000:
        return f"{num_bytes / 1_000_000:.1f} MB"
    if num_bytes < 1_000_000_000_000:
        return f"{num_bytes / 1_000_000_000:.1f} GB"
    return f"{num_bytes / 1_000_000_000_000:.1f} TB"
