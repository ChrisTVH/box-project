"""Pure game inspection for graphical front ends."""

from __future__ import annotations

from pathlib import Path

from box.engines.registry import EngineRegistry
from box.games.inspector import Inspection, inspect_game

__all__ = ["Inspection", "inspect"]


def inspect(path: Path, registry: EngineRegistry | None = None) -> Inspection:
    """Return inspection details without any console output."""
    return inspect_game(path, registry)
