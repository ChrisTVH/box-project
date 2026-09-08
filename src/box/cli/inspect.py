"""Inspect command implementation."""

from __future__ import annotations

from pathlib import Path

from box.games.inspector import inspect_game
from box.utils.i18n import _


def execute(path: Path) -> int:
    """Print a concise non-destructive game inspection."""
    inspection = inspect_game(path)
    print(f"{_('engine')}: {inspection.game.engine.value}")
    print(f"{_('root')}: {inspection.game.root}")
    print(f"{_('entrypoint')}: {inspection.game.entrypoint or _('(not applicable)')}")
    print(f"{_('title')}: {inspection.title or _('(unknown)')}")
    print(f"{_('plugins')}: {inspection.plugin_count}")
    return 0
