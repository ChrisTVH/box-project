"""Inspect command implementation."""

from __future__ import annotations

from pathlib import Path

from box.api.inspect import inspect as inspect_game_api
from box.utils.i18n import _
from box.utils.terminal import safe_terminal_text


def execute(path: Path) -> int:
    """Print a concise non-destructive game inspection."""
    inspection = inspect_game_api(path)
    engine_label = _("engine")
    root_label = _("root")
    entrypoint_label = _("entrypoint")
    not_applicable = _("(not applicable)")
    title_label = _("title")
    unknown = _("(unknown)")
    plugins_label = _("plugins")
    print(f"{engine_label}: {inspection.game.engine.value}")
    print(f"{root_label}: {safe_terminal_text(inspection.game.root)}")
    print(f"{entrypoint_label}: {safe_terminal_text(inspection.game.entrypoint or not_applicable)}")
    print(f"{title_label}: {safe_terminal_text(inspection.title or unknown)}")
    print(f"{plugins_label}: {inspection.plugin_count}")
    return 0
