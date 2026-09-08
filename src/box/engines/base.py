"""Engine extension contract."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from box.errors import GameValidationError
from box.models import EngineName, GameInfo
from box.paths import open_directory_without_symlinks


class EngineAdapter(Protocol):
    """An independently testable game-engine integration."""

    name: EngineName

    def detect(self, root: Path) -> GameInfo | None:
        """Return game information when the directory matches this engine."""


def is_game_artifact(root: Path, path: Path, directory: bool = False) -> bool:
    """Return whether a required game artifact exists without traversing symlinks."""
    from box.games.files import open_relative_file

    try:
        relative = path.relative_to(root)
        if ".." in relative.parts:
            return False
        descriptor = open_directory_without_symlinks(root)
        try:
            if directory:
                for component in relative.parts:
                    child = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    os.close(descriptor)
                    descriptor = child
            else:
                child = open_relative_file(descriptor, relative)
                os.close(child)
            return True
        finally:
            os.close(descriptor)
    except ValueError, OSError, GameValidationError:
        return False
