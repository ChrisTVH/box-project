"""Non-destructive game inspection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from box.engines.registry import EngineRegistry, default_registry
from box.errors import GameValidationError
from box.games.detector import detect_game
from box.models import GameInfo


@dataclass(frozen=True, slots=True)
class Inspection:
    """User-facing details collected without changing a game."""

    game: GameInfo
    title: str | None
    plugin_count: int


def inspect_game(path: Path, registry: EngineRegistry | None = None) -> Inspection:
    """Inspect a supported MV/MZ export without launching or modifying it."""
    game = detect_game(path, default_registry() if registry is None else registry)
    manifest = _read_json(game.manifest)
    title_value: object = manifest.get("name")
    title = title_value if isinstance(title_value, str) else None
    plugins_path = game.entrypoint.parent / "js" / "plugins.js"
    return Inspection(game=game, title=title, plugin_count=_plugin_count(plugins_path))


def _read_json(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GameValidationError(f"invalid game manifest {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise GameValidationError(f"game manifest is not a JSON object: {path}")
    return cast(dict[str, object], raw)


def _plugin_count(path: Path) -> int:
    try:
        return path.read_text(encoding="utf-8", errors="replace").count('"name"')
    except OSError as exc:
        raise GameValidationError(f"cannot read plugin registry {path}: {exc}") from exc
