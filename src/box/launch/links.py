"""Session-local symbolic links to validated game directories."""

from __future__ import annotations

import os
import shutil
import stat
from contextlib import suppress
from pathlib import Path

from box.errors import LaunchError


def link_game(session_root: Path, game_root: Path) -> Path:
    """Create the only game reference used by a launch session."""
    link = session_root / "game"
    if link.exists() or link.is_symlink():
        raise LaunchError(f"session game link already exists: {link}")
    try:
        link.symlink_to(game_root, target_is_directory=True)
    except OSError as exc:
        raise LaunchError(f"cannot link game into session: {exc}") from exc
    return link


def copy_game_root_file(session_root: Path, game_root: Path, filename: str) -> Path:
    """Copy one validated direct game-root file into an isolated launch session."""
    if Path(filename).name != filename or filename in {"", ".", "..", "game", "package.json"}:
        raise LaunchError(f"invalid game-root filename: {filename!r}")
    destination = session_root / filename
    if destination.exists() or destination.is_symlink():
        raise LaunchError(f"session game-root file already exists: {destination}")
    try:
        root_descriptor = os.open(game_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise LaunchError(f"game-root file is missing or unsafe: {filename}") from exc
    try:
        try:
            source_descriptor = os.open(
                filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_descriptor
            )
        except OSError as exc:
            raise LaunchError(f"game-root file is missing or unsafe: {filename}") from exc
        try:
            if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
                raise LaunchError(f"game-root file is missing or unsafe: {filename}")
            try:
                destination_descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
            except OSError as exc:
                raise LaunchError(f"cannot copy game-root file into session: {exc}") from exc
            try:
                with (
                    os.fdopen(source_descriptor, "rb", closefd=False) as source_file,
                    os.fdopen(destination_descriptor, "wb", closefd=False) as destination_file,
                ):
                    shutil.copyfileobj(source_file, destination_file)
            except OSError as exc:
                with suppress(OSError):
                    destination.unlink(missing_ok=True)
                raise LaunchError(f"cannot copy game-root file into session: {exc}") from exc
            finally:
                os.close(destination_descriptor)
        finally:
            os.close(source_descriptor)
    finally:
        os.close(root_descriptor)
    return destination
