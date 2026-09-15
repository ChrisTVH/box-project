"""Descriptor-relative, bounded access to untrusted game files."""

from __future__ import annotations

import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from box.errors import GameValidationError
from box.models import GameInfo
from box.paths import open_directory_without_symlinks
from box.utils.i18n import _

MAX_GAME_FILE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True, kw_only=True)
class DetectedGameInfo(GameInfo):
    """Game information bound to the directory inspected during detection."""

    root_identity: tuple[int, int]


def directory_identity(descriptor: int) -> tuple[int, int]:
    """Return the device and inode of an open directory."""
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


def validate_game_descriptor(game: GameInfo, descriptor: int) -> None:
    """Reject replacement or relocation of the directory being authorized."""
    try:
        current = open_directory_without_symlinks(game.root)
        try:
            identity = directory_identity(descriptor)
            if directory_identity(current) != identity:
                raise GameValidationError(_("game root changed since detection"))
            if isinstance(game, DetectedGameInfo) and game.root_identity != identity:
                raise GameValidationError(_("game root changed since detection"))
            if Path(os.readlink(f"/proc/self/fd/{descriptor}")) != game.root:
                raise GameValidationError(_("game root moved since detection"))
        finally:
            os.close(current)
    except OSError as exc:
        raise GameValidationError(_("game root is missing or contains a symlink")) from exc


@contextmanager
def open_game_directory(game: GameInfo) -> Generator[int]:
    """Open and verify a detected root without following any symlink component."""
    try:
        descriptor = open_directory_without_symlinks(game.root)
    except OSError as exc:
        raise GameValidationError(_("game root is missing or contains a symlink")) from exc
    try:
        validate_game_descriptor(game, descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


def open_relative_file(directory_descriptor: int, relative: Path) -> int:
    """Open a regular file beneath a pinned directory; reject all symlinks."""
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise GameValidationError(
            _("invalid relative game file path: {path}").format(path=relative)
        )
    parent = os.dup(directory_descriptor)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise GameValidationError(
                    _("game file is not a regular file: {path}").format(path=relative)
                )
            return descriptor
        except Exception:
            os.close(descriptor)
            raise
    finally:
        os.close(parent)


def read_game_file(
    directory_descriptor: int, relative: Path, *, max_bytes: int = MAX_GAME_FILE_BYTES
) -> bytes:
    """Read at most max_bytes, including when a file grows after fstat."""
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    try:
        descriptor = open_relative_file(directory_descriptor, relative)
        try:
            if os.fstat(descriptor).st_size > max_bytes:
                raise GameValidationError(
                    _("game file exceeds byte limit: {path}").format(path=relative)
                )
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                data = source.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise GameValidationError(
                    _("game file exceeds byte limit: {path}").format(path=relative)
                )
            return data
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise GameValidationError(
            _("cannot safely read game file {path}: {error}").format(path=relative, error=exc)
        ) from exc
