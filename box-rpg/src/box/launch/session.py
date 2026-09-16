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
from box.launch.links import copy_game_root_file, descriptor_path, link_game, open_game_root
from box.launch.manifest import write_manifest
from box.launch.profiles import ProfileCatalog
from box.models import GameInfo
from box.paths import AppPaths
from box.utils.i18n import _


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
    profile_root: Path
    owns_game_descriptor: bool
    detached: bool = False

    @property
    def reference(self) -> Path:
        """Return the stable session pathname used to launch this session."""
        return self.root

    @property
    def identifier(self) -> str:
        """Return the per-game directory name owning this session."""
        return self.root.parent.name

    @property
    def process_descriptors(self) -> tuple[int, ...]:
        """Return descriptors retained by the launcher while its session is active."""
        return ()

    def detach(self) -> None:
        """Transfer lifetime to the detached supervisor; skip removal on exit."""
        self.detached = True

    def release(self) -> None:
        """Close launcher descriptors without removing the supervised directory."""
        with suppress(OSError):
            os.close(self.session_descriptor)
        with suppress(OSError):
            os.close(self.parent_descriptor)
        if self.owns_game_descriptor:
            with suppress(OSError):
                os.close(self.game_descriptor)

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
        if self.detached:
            self.release()
            return
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
    try:
        game_reference_path = descriptor_path(game_descriptor)
    except Exception:
        if owns_game_descriptor:
            os.close(game_descriptor)
        raise
    try:
        identifier = _game_identifier(game)
        profile_root = ProfileCatalog(paths).create_for_game(game, game_reference_path)
    except Exception:
        if owns_game_descriptor:
            os.close(game_descriptor)
        raise
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
        raise LaunchError(_("cannot create launch session: {error}").format(error=exc)) from exc
    try:
        os.mkdir("save", 0o700, dir_fd=session_descriptor)
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
        profile_root,
        owns_game_descriptor,
    )


def _game_identifier(game: GameInfo) -> str:
    """Convert a missing or racing game path into a launch-domain error."""
    try:
        return game_id(game.root)
    except (OSError, RuntimeError) as exc:
        raise LaunchError(
            _("cannot identify game root {root}: {error}").format(root=game.root, error=exc)
        ) from exc


def _open_or_create_private_directory(parent_descriptor: int, name: str) -> None:
    """Create and validate one private session-directory component."""
    try:
        _create_private_directory(parent_descriptor, name)
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_descriptor
        )
    except OSError as exc:
        raise LaunchError(
            _("cannot create launch session directory: {error}").format(error=exc)
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid():
            raise LaunchError(_("launch session directory has unsafe ownership or permissions"))
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
            metadata = os.fstat(descriptor)
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise LaunchError(_("launch session directory has unsafe ownership or permissions"))
    finally:
        os.close(descriptor)


def _create_private_directory(parent_descriptor: int, name: str) -> None:
    """Create one private directory without resolving a pathname."""
    with suppress(FileExistsError):
        os.mkdir(name, 0o700, dir_fd=parent_descriptor)
