"""Creation and lifetime management of isolated launch sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from box.games.identity import game_id
from box.launch.cleanup import remove_session
from box.launch.links import link_game
from box.launch.manifest import write_manifest
from box.models import GameInfo
from box.paths import AppPaths


@dataclass(slots=True)
class LaunchSession:
    """A launcher-owned ephemeral directory used by one NW.js process."""

    paths: AppPaths
    game: GameInfo
    root: Path

    def cleanup(self) -> None:
        """Remove this session directory."""
        remove_session(self.paths, self.root)

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


def create_session(paths: AppPaths, game: GameInfo) -> LaunchSession:
    """Create an isolated manifest and game link under the XDG cache."""
    paths.ensure()
    root = paths.sessions_root / game_id(game.root) / uuid4().hex
    root = paths.ensure_managed_session_path(root)
    root.mkdir(parents=True, mode=0o700)
    try:
        link_game(root, game.root)
        write_manifest(root, game)
    except Exception:
        remove_session(paths, root)
        raise
    return LaunchSession(paths, game, root)
