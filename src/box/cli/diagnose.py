"""Diagnose command implementation."""

from __future__ import annotations

from pathlib import Path

from box.config.repository import ConfigRepository
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
from box.utils.i18n import _


def execute(
    paths: AppPaths,
    repository: ConfigRepository,
    game_path: Path,
    version: str | None,
    sdk: bool,
) -> int:
    """Print a local diagnostic report without network transmission."""
    game = detect_game(game_path, default_registry())
    config = repository.load()
    if game.engine is EngineName.RPG_MAKER_2000_2003:
        if sdk:
            raise GameValidationError(
                _("{sdk} is only available for NW.js games").format(sdk="--sdk")
            )
        catalog = EasyRPGCatalog(paths)
        runtime = catalog.get(version) if version is not None else catalog.latest()
        print(render_report(collect_environment(), collect_easyrpg_versions(game, runtime)), end="")
        return 0
    runtime = select_runtime(
        RuntimeCatalog(paths),
        current_architecture(),
        config.preferred_runtime if version is None else version,
        sdk or config.prefer_sdk,
    )
    print(render_report(collect_environment(), collect_versions(game, runtime)), end="")
    return 0
