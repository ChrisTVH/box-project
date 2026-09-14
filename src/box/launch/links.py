"""Session-local symbolic links to validated game directories."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from pathlib import Path

from box.errors import LaunchError
from box.games.files import MAX_GAME_FILE_BYTES
from box.paths import open_directory_without_symlinks
from box.utils.i18n import _

_RESERVED_ROOT_NAMES = frozenset({"", ".", "..", "game", "package.json"})


def open_game_root(game_root: Path) -> int:
    """Open a game root without following a replacement symlink."""
    try:
        return open_directory_without_symlinks(game_root)
    except OSError as exc:
        raise LaunchError(
            _("game root is missing or unsafe: {root}").format(root=game_root)
        ) from exc


def list_root_files(game_root: Path) -> tuple[str, ...]:
    """List copyable direct game-root filenames in sorted order.

    Uses the open_game_root descriptor so listing and copying share one
    trust model. Excludes directories, symlinks, reserved names, and files
    over MAX_GAME_FILE_BYTES. Closes the descriptor in a finally block.
    """
    descriptor = open_game_root(game_root)
    try:
        try:
            with os.scandir(descriptor) as it:
                names: list[str] = []
                for entry in it:
                    try:
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError:
                        continue
                    if not is_file:
                        continue
                    name = entry.name
                    if name in _RESERVED_ROOT_NAMES:
                        continue
                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if size > MAX_GAME_FILE_BYTES:
                        continue
                    names.append(name)
        except OSError as exc:
            raise LaunchError(
                _("game root is missing or unsafe: {root}").format(root=game_root)
            ) from exc
        return tuple(sorted(names))
    finally:
        os.close(descriptor)


def descriptor_path(descriptor: int) -> Path:
    """Resolve the current stable pathname for an open game directory descriptor."""
    try:
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
    except OSError as exc:
        raise LaunchError(_("cannot resolve the open game directory")) from exc
    if path.name.endswith(" (deleted)"):
        raise LaunchError(_("game root disappeared before launch"))
    return path


def link_game(
    session_root: Path,
    *,
    session_descriptor: int,
    game_descriptor: int,
) -> Path:
    """Create the only game reference used by a launch session."""
    link = session_root / "game"
    try:
        os.symlink("/game", "game", dir_fd=session_descriptor)
    except OSError as exc:
        raise LaunchError(_("cannot link game into session: {error}").format(error=exc)) from exc
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
    if Path(filename).name != filename or filename in _RESERVED_ROOT_NAMES:
        raise LaunchError(_("invalid game-root filename: {filename!r}").format(filename=filename))
    destination = session_root / filename
    owns_session_descriptor = session_descriptor is None
    owns_game_descriptor = game_descriptor is None
    try:
        if session_descriptor is None:
            session_descriptor = open_directory_without_symlinks(session_root)
        if game_descriptor is None:
            game_descriptor = open_game_root(game_root)
        try:
            source_descriptor = os.open(
                filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=game_descriptor
            )
        except OSError as exc:
            raise LaunchError(
                _("game-root file is missing or unsafe: {filename}").format(filename=filename)
            ) from exc
        try:
            metadata = os.fstat(source_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise LaunchError(
                    _("game-root file is missing or unsafe: {filename}").format(filename=filename)
                )
            if metadata.st_size > MAX_GAME_FILE_BYTES:
                raise LaunchError(
                    _("game-root file exceeds byte limit: {filename}").format(filename=filename)
                )
            try:
                destination_descriptor = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=session_descriptor,
                )
            except OSError as exc:
                raise LaunchError(
                    _("cannot copy game-root file into session: {error}").format(error=exc)
                ) from exc
            try:
                with (
                    os.fdopen(source_descriptor, "rb", closefd=False) as source_file,
                    os.fdopen(destination_descriptor, "wb", closefd=False) as destination_file,
                ):
                    remaining = MAX_GAME_FILE_BYTES
                    while chunk := source_file.read(min(64 * 1024, remaining + 1)):
                        if len(chunk) > remaining:
                            raise LaunchError(
                                _("game-root file exceeds byte limit: {filename}").format(
                                    filename=filename
                                )
                            )
                        destination_file.write(chunk)
                        remaining -= len(chunk)
            except (OSError, LaunchError) as exc:
                with suppress(OSError):
                    os.unlink(filename, dir_fd=session_descriptor)
                raise LaunchError(
                    _("cannot copy game-root file into session: {error}").format(error=exc)
                ) from exc
            finally:
                os.close(destination_descriptor)
        finally:
            os.close(source_descriptor)
    except OSError as exc:
        raise LaunchError(
            _("cannot copy game-root file into session: {error}").format(error=exc)
        ) from exc
    finally:
        if owns_game_descriptor and game_descriptor is not None:
            os.close(game_descriptor)
        if owns_session_descriptor and session_descriptor is not None:
            os.close(session_descriptor)
    return destination
