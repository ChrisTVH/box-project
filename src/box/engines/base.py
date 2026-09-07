"""Engine extension contract."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from box.models import EngineName, GameInfo


class EngineAdapter(Protocol):
    """An independently testable game-engine integration."""

    name: EngineName

    def detect(self, root: Path) -> GameInfo | None:
        """Return game information when the directory matches this engine."""


def is_game_artifact(root: Path, path: Path, directory: bool = False) -> bool:
    """Return whether a required game artifact exists without traversing symlinks."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            return False
    return path.is_dir() if directory else path.is_file()
