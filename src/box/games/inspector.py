"""Non-destructive game inspection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from box.engines.registry import EngineRegistry, default_registry
from box.errors import GameValidationError
from box.games.detector import detect_game
from box.games.files import open_game_directory, read_game_file
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
    with open_game_directory(game) as descriptor:
        if game.engine.value == "rpg-maker-2000-2003":
            return Inspection(game=game, title=_rpg_rt_title(descriptor), plugin_count=0)
        if game.manifest is None or game.entrypoint is None:
            raise GameValidationError(_("web game inspection requires a manifest and entrypoint"))
        manifest = _read_json(descriptor, game.manifest.relative_to(game.root))
        title_value: object = manifest.get("name")
        title = title_value if isinstance(title_value, str) else None
        plugins_path = game.entrypoint.parent / "js" / "plugins.js"
        return Inspection(
            game=game,
            title=title,
            plugin_count=_plugin_count(descriptor, plugins_path.relative_to(game.root)),
        )


def _rpg_rt_title(descriptor: int) -> str | None:
    """Read the title from an RPG_RT.ini file without assuming its code page."""
    for line in read_game_file(descriptor, Path("RPG_RT.ini")).decode("latin-1").splitlines():
        if line.startswith("GameTitle="):
            return line.removeprefix("GameTitle=").strip() or None
    return None


def _read_json(descriptor: int, path: Path) -> dict[str, object]:
    try:
        # utf-8-sig tolerates the BOM some editors prepend; it is identical
        # to utf-8 otherwise and byte limits still apply before decoding.
        raw = json.loads(read_game_file(descriptor, path).decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise GameValidationError(
            _("invalid game manifest {path}: {error}").format(path=path, error=exc)
        ) from exc
    if not isinstance(raw, dict):
        raise GameValidationError(
            _("game manifest is not a {format} object: {path}").format(format="JSON", path=path)
        )
    return cast(dict[str, object], raw)


def _plugin_count(descriptor: int, path: Path) -> int:
    return read_game_file(descriptor, path).decode("utf-8", errors="replace").count('"name"')
