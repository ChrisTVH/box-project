"""Session-local symbolic links to validated game directories."""

from __future__ import annotations

import os
import shutil
import stat
from contextlib import suppress
from pathlib import Path

from box.errors import LaunchError


def open_game_root(game_root: Path) -> int:
    """Open a game root without following a replacement symlink."""
    try:
        return os.open(game_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise LaunchError(f"game root is missing or unsafe: {game_root}") from exc


def descriptor_reference(descriptor: int) -> Path:
    """Return the Linux path that names an inherited open descriptor."""
    return Path(f"/proc/self/fd/{descriptor}")


def link_game(
    session_root: Path,
    *,
    session_descriptor: int,
    game_descriptor: int,
) -> Path:
    """Create the only game reference used by a launch session."""
    link = session_root / "game"
    try:
        os.symlink(descriptor_reference(game_descriptor), "game", dir_fd=session_descriptor)
    except OSError as exc:
        raise LaunchError(f"cannot link game into session: {exc}") from exc
    return link


def copy_game_root_file(
    session_root: Path,
    game_root: Path,
    filename: str,
    *,
    session_descriptor: int | None = None,
    game_descriptor: int | None = None,
) -> Path:
    """Copy one validated direct game-root file into an isolated launch session."""
    if Path(filename).name != filename or filename in {"", ".", "..", "game", "package.json"}:
        raise LaunchError(f"invalid game-root filename: {filename!r}")
    destination = session_root / filename
    owns_session_descriptor = session_descriptor is None
    owns_game_descriptor = game_descriptor is None
    try:
        if session_descriptor is None:
            session_descriptor = os.open(session_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if game_descriptor is None:
            game_descriptor = open_game_root(game_root)
        try:
            source_descriptor = os.open(
                filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=game_descriptor
            )
        except OSError as exc:
            raise LaunchError(f"game-root file is missing or unsafe: {filename}") from exc
        try:
            if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
                raise LaunchError(f"game-root file is missing or unsafe: {filename}")
            try:
                destination_descriptor = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=session_descriptor,
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
                    os.unlink(filename, dir_fd=session_descriptor)
                raise LaunchError(f"cannot copy game-root file into session: {exc}") from exc
            finally:
                os.close(destination_descriptor)
        finally:
            os.close(source_descriptor)
    except OSError as exc:
        raise LaunchError(f"cannot copy game-root file into session: {exc}") from exc
    finally:
        if owns_game_descriptor and game_descriptor is not None:
            os.close(game_descriptor)
        if owns_session_descriptor and session_descriptor is not None:
            os.close(session_descriptor)
    return destination
