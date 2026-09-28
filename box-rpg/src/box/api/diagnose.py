"""Pure diagnostic collection for graphical front ends."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

from box.config.repository import ConfigRepository
from box.diagnostics.environment import Environment, collect_environment
from box.diagnostics.versions import (
    VersionReport,
    collect_easyrpg_versions,
    collect_versions,
    detect_bundled_nwjs_version,
)
from box.engines.registry import default_registry
from box.errors import GameValidationError
from box.games.detector import detect_game, resolve_game_root
from box.models import EngineName
from box.paths import AppPaths
from box.runtime import evb as _evb
from box.runtime.catalog import RuntimeCatalog
from box.runtime.easyrpg import EasyRPGCatalog
from box.runtime.platform import current_architecture
from box.runtime.selector import select_runtime
from box.utils.i18n import _

__all__ = ["DiagnoseResult", "diagnose"]


@dataclass(frozen=True, slots=True)
class DiagnoseResult:
    """Environment plus version report without rendering or printing."""

    environment: Environment
    versions: VersionReport


def diagnose(
    paths: AppPaths,
    repository: ConfigRepository,
    game_path: Path,
    version: str | None,
    sdk: bool,
) -> DiagnoseResult:
    """Collect local diagnostics without network transmission or console output."""
    effective, source_root, source_exe = _resolve_effective_game(paths, game_path)
    game = detect_game(effective, default_registry())
    config = repository.load()
    if game.engine is EngineName.RPG_MAKER_2000_2003:
        if sdk:
            raise GameValidationError(
                _("{sdk} is only available for NW.js games").format(sdk="--sdk")
            )
        catalog = EasyRPGCatalog(paths)
        runtime = catalog.get(version) if version is not None else catalog.latest()
        return DiagnoseResult(collect_environment(), collect_easyrpg_versions(game, runtime))
    runtime_nw = select_runtime(
        RuntimeCatalog(paths),
        current_architecture(),
        config.preferred_runtime if version is None else version,
        sdk or config.prefer_sdk,
    )
    report = collect_versions(game, runtime_nw)
    bundled = detect_bundled_nwjs_version(game.root, source_root, source_exe)
    if bundled is not None:
        report = dataclasses.replace(report, game_nwjs=bundled)
    return DiagnoseResult(collect_environment(), report)


def _resolve_effective_game(
    paths: AppPaths, game_path: Path
) -> tuple[Path, Path | None, Path | None]:
    """Unpack a packed single-executable directory before detection.

    Returns the effective detection path plus the packed source root and
    executable, so bundled NW.js probes can read both the source folder
    (plain Windows exports keep ``nw.dll`` beside the executable) and the
    unpacked profile tree (EVB restores packed files there). Anything else
    keeps the given path untouched with no source attached.
    """
    try:
        source_root = resolve_game_root(game_path)
    except GameValidationError:
        return game_path, None, None
    candidate = _evb.find_packed_executable(source_root)
    if candidate is None:
        return game_path, source_root, None
    return _evb.ensure_unpacked(paths, candidate), source_root, candidate
