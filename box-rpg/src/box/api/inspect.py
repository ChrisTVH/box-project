"""Pure game inspection for graphical front ends."""

from __future__ import annotations

from pathlib import Path

from box.engines.registry import EngineRegistry
from box.errors import GameValidationError
from box.games.detector import resolve_game_root
from box.games.inspector import Inspection, inspect_game
from box.models import GameInfo
from box.paths import AppPaths
from box.runtime import evb as _evb

__all__ = ["Inspection", "inspect"]


def inspect(paths: AppPaths, path: Path, registry: EngineRegistry | None = None) -> Inspection:
    """Return inspection details without any console output.

    A packed single-executable directory is unpacked into its source-keyed
    profile first (a cache hit after the first run); detection, title, and
    plugin counts read the unpacked tree, while the returned game stays
    rooted at the source directory so consent, library paths, and launch
    identifiers agree. Entrypoint and manifest stay under the unpacked
    tree so they always exist; extra-root file candidates must resolve
    against that unpacked tree, not the source root. Launching the
    returned root re-unpacks through the same cache hit.
    """
    try:
        source_root = resolve_game_root(path)
    except GameValidationError:
        return inspect_game(path, registry)
    candidate = _evb.find_packed_executable(source_root)
    if candidate is None:
        return inspect_game(path, registry)
    unpacked = _evb.ensure_unpacked(paths, candidate)
    return _reroot_at_source(inspect_game(unpacked, registry), source_root)


def _reroot_at_source(inspection: Inspection, source: Path) -> Inspection:
    """Keep consent root at the source while entrypoint stays unpacked.

    Engine adapters build entrypoint and manifest paths below the
    detected (unpacked) root; re-rooting them below the packed source
    would point at files that do not exist. Only the game root moves to
    the source for consent and identity.
    """
    game = inspection.game
    return Inspection(
        game=GameInfo(
            game.engine,
            source,
            game.entrypoint,
            game.manifest,
        ),
        title=inspection.title,
        plugin_count=inspection.plugin_count,
    )
