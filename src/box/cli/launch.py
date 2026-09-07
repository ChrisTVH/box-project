"""Launch command implementation."""

from __future__ import annotations

from pathlib import Path

from box.config.repository import ConfigRepository
from box.engines.registry import default_registry
from box.games.detector import detect_game, ensure_allowed_root
from box.launch.command import build_command
from box.launch.process import run_process
from box.launch.session import create_session
from box.paths import AppPaths
from box.runtime.catalog import RuntimeCatalog
from box.runtime.platform import current_architecture
from box.runtime.selector import select_runtime


def execute(
    paths: AppPaths,
    repository: ConfigRepository,
    game_path: Path,
    version: str | None,
    sdk: bool,
) -> int:
    """Launch an allowed game through an isolated session."""
    config = repository.load()
    game = detect_game(game_path, default_registry())
    ensure_allowed_root(game, config.allowed_game_roots)
    runtime = select_runtime(
        RuntimeCatalog(paths),
        current_architecture(),
        config.preferred_runtime if version is None else version,
        sdk or config.prefer_sdk,
    )
    with create_session(paths, game) as session:
        return run_process(build_command(runtime, session.root))
