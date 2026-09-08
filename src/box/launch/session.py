"""Creation and lifetime management of isolated launch sessions."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from box.errors import LaunchError
from box.games.identity import game_id
from box.launch.cleanup import remove_session
from box.launch.links import copy_game_root_file, descriptor_reference, link_game, open_game_root
from box.launch.manifest import write_manifest
from box.models import GameInfo
from box.paths import AppPaths


@dataclass(slots=True)
class LaunchSession:
    """A launcher-owned ephemeral directory used by one NW.js process."""

    paths: AppPaths
    game: GameInfo
    root: Path
    session_descriptor: int
    parent_descriptor: int
    name: str
    game_descriptor: int
    owns_game_descriptor: bool

    @property
    def reference(self) -> Path:
        """Return the descriptor-backed path used to launch this session."""
        return descriptor_reference(self.session_descriptor)

    @property
    def game_reference(self) -> Path:
        """Return the descriptor-backed path used as the game's working directory."""
        return descriptor_reference(self.game_descriptor)

    @property
    def process_descriptors(self) -> tuple[int, ...]:
        """Return descriptors that must remain open in the runtime process."""
        return (self.session_descriptor, self.game_descriptor)

    def cleanup(self) -> None:
        """Remove this session directory."""
        try:
            remove_session(
                self.paths,
                self.root,
                parent_descriptor=self.parent_descriptor,
                session_descriptor=self.session_descriptor,
            )
        finally:
            os.close(self.session_descriptor)
            os.close(self.parent_descriptor)
            if self.owns_game_descriptor:
                os.close(self.game_descriptor)

    def __enter__(self) -> LaunchSession:
        """Return the initialized session."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Clean the session even when launching fails."""
        self.cleanup()


def create_session(
    paths: AppPaths,
    game: GameInfo,
    copy_root_files: tuple[str, ...] = (),
    *,
    game_descriptor: int | None = None,
) -> LaunchSession:
    """Create an isolated manifest and game link under the XDG cache."""
    paths.ensure()
    owns_game_descriptor = game_descriptor is None
    if game_descriptor is None:
        game_descriptor = open_game_root(game.root)
    identifier = _game_identifier(game)
    name = uuid4().hex
    root = paths.sessions_root / identifier / name
    try:
        paths.ensure_managed_session_path(root)
    except Exception:
        if owns_game_descriptor:
            os.close(game_descriptor)
        raise
    parent_descriptor = paths.open_managed_cache_directory("sessions")
    try:
        _open_or_create_private_directory(parent_descriptor, identifier)
        game_descriptor_parent = os.open(
            identifier, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_descriptor
        )
    except Exception:
        os.close(parent_descriptor)
        if owns_game_descriptor:
            os.close(game_descriptor)
        raise
    os.close(parent_descriptor)
    parent_descriptor = game_descriptor_parent
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        created = True
        session_descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_descriptor
        )
    except OSError as exc:
        if created:
            with suppress(OSError):
                os.rmdir(name, dir_fd=parent_descriptor)
        os.close(parent_descriptor)
        if owns_game_descriptor:
            os.close(game_descriptor)
        raise LaunchError(f"cannot create launch session: {exc}") from exc
    try:
        link_game(
            root,
            session_descriptor=session_descriptor,
            game_descriptor=game_descriptor,
        )
        for filename in copy_root_files:
            copy_game_root_file(
                root,
                game.root,
                filename,
                session_descriptor=session_descriptor,
                game_descriptor=game_descriptor,
            )
        write_manifest(
            root,
            game,
            session_descriptor=session_descriptor,
            game_descriptor=game_descriptor,
        )
    except Exception:
        try:
            remove_session(
                paths,
                root,
                parent_descriptor=parent_descriptor,
                session_descriptor=session_descriptor,
            )
        finally:
            os.close(session_descriptor)
            os.close(parent_descriptor)
            if owns_game_descriptor:
                os.close(game_descriptor)
        raise
    return LaunchSession(
        paths,
        game,
        root,
        session_descriptor,
        parent_descriptor,
        name,
        game_descriptor,
        owns_game_descriptor,
    )


def _game_identifier(game: GameInfo) -> str:
    """Convert a missing or racing game path into a launch-domain error."""
    try:
        return game_id(game.root)
    except (OSError, RuntimeError) as exc:
        raise LaunchError(f"cannot identify game root {game.root}: {exc}") from exc


def _open_or_create_private_directory(parent_descriptor: int, name: str) -> None:
    """Create and validate one private session-directory component."""
    try:
        _create_private_directory(parent_descriptor, name)
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_descriptor
        )
    except OSError as exc:
        raise LaunchError(f"cannot create launch session directory: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise LaunchError("launch session directory has unsafe ownership or permissions")
    finally:
        os.close(descriptor)


def _create_private_directory(parent_descriptor: int, name: str) -> None:
    """Create one private directory without resolving a pathname."""
    with suppress(FileExistsError):
        os.mkdir(name, 0o700, dir_fd=parent_descriptor)
