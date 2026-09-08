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
from box.utils.i18n import _


@dataclass(frozen=True, slots=True)
class Inspection:
    """User-facing details collected without changing a game."""

    game: GameInfo
    title: str | None
    plugin_count: int


def inspect_game(path: Path, registry: EngineRegistry | None = None) -> Inspection:
    """Inspect a supported game without launching or modifying it."""
    game = detect_game(path, default_registry() if registry is None else registry)
    if game.engine.value == "rpg-maker-2000-2003":
        return Inspection(game=game, title=_rpg_rt_title(game.root / "RPG_RT.ini"), plugin_count=0)
    if game.manifest is None or game.entrypoint is None:
        raise GameValidationError(_("web game inspection requires a manifest and entrypoint"))
    manifest = _read_json(game.manifest)
    title_value: object = manifest.get("name")
    title = title_value if isinstance(title_value, str) else None
    plugins_path = game.entrypoint.parent / "js" / "plugins.js"
    return Inspection(game=game, title=title, plugin_count=_plugin_count(plugins_path))


def _rpg_rt_title(path: Path) -> str | None:
    """Read the title from an RPG_RT.ini file without assuming its code page."""
    try:
        for line in path.read_text(encoding="latin-1").splitlines():
            if line.startswith("GameTitle="):
                return line.removeprefix("GameTitle=").strip() or None
    except OSError:
        return None
    return None


def _read_json(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GameValidationError(
            _("invalid game manifest {path}: {error}").format(path=path, error=exc)
        ) from exc
    if not isinstance(raw, dict):
        raise GameValidationError(
            _("game manifest is not a {format} object: {path}").format(format="JSON", path=path)
        )
    return cast(dict[str, object], raw)


def _plugin_count(path: Path) -> int:
    try:
        return path.read_text(encoding="utf-8", errors="replace").count('"name"')
    except OSError as exc:
        raise GameValidationError(
            _("cannot read plugin registry {path}: {error}").format(path=path, error=exc)
        ) from exc
