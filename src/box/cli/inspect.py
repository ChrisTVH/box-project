"""Inspect command implementation."""

from __future__ import annotations

from pathlib import Path

from box.games.inspector import inspect_game


def execute(path: Path) -> int:
    """Print a concise non-destructive game inspection."""
    inspection = inspect_game(path)
    print(f"engine: {inspection.game.engine.value}")
    print(f"root: {inspection.game.root}")
    print(f"entrypoint: {inspection.game.entrypoint}")
    print(f"title: {inspection.title or '(unknown)'}")
    print(f"plugins: {inspection.plugin_count}")
    return 0
