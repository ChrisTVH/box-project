"""Diagnose command implementation."""

from __future__ import annotations

from pathlib import Path

from box.diagnostics.environment import collect_environment
from box.diagnostics.report import render_report
from box.diagnostics.versions import collect_easyrpg_versions, collect_versions
from box.engines.registry import default_registry
from box.errors import GameValidationError
from box.games.detector import detect_game
from box.models import EngineName
from box.paths import AppPaths
from box.runtime.catalog import RuntimeCatalog
from box.runtime.easyrpg import EasyRPGCatalog
from box.runtime.platform import current_architecture
from box.runtime.selector import select_runtime


def execute(paths: AppPaths, game_path: Path, version: str | None, sdk: bool) -> int:
    """Print a local diagnostic report without network transmission."""
    game = detect_game(game_path, default_registry())
    if game.engine is EngineName.RPG_MAKER_2000_2003:
        if version is not None or sdk:
            raise GameValidationError("--runtime and --sdk are only available for NW.js games")
        runtime = EasyRPGCatalog(paths).latest()
        print(render_report(collect_environment(), collect_easyrpg_versions(game, runtime)), end="")
        return 0
    runtime = select_runtime(RuntimeCatalog(paths), current_architecture(), version, sdk)
    print(render_report(collect_environment(), collect_versions(game, runtime)), end="")
    return 0
