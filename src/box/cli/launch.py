"""Launch command implementation."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.engines.registry import default_registry
from box.errors import GameValidationError
from box.games.detector import detect_game, ensure_allowed_root
from box.games.files import open_game_directory, validate_game_descriptor
from box.launch.command import build_command
from box.launch.links import descriptor_path, open_game_root
from box.launch.process import run_process
from box.launch.session import create_session
from box.models import EngineName, GameInfo
from box.paths import AppPaths
from box.runtime.catalog import RuntimeCatalog
from box.runtime.easyrpg import EasyRPGCatalog
from box.runtime.easyrpg import executable as easyrpg_executable
from box.runtime.platform import current_architecture
from box.runtime.selector import select_runtime
from box.utils.i18n import _
from box.utils.terminal import safe_terminal_text


def execute(
    paths: AppPaths,
    repository: ConfigRepository,
    game_path: Path,
    version: str | None,
    sdk: bool,
    copy_root_files: tuple[str, ...] = (),
) -> int:
    """Launch an allowed game through an isolated session."""
    game = detect_game(game_path, default_registry())
    with _game_root_descriptor(game.root) as game_descriptor:
        validate_game_descriptor(game, game_descriptor)
        config = repository.load()
        read = input if sys.stdin.isatty() else None
        if game.engine is EngineName.RPG_MAKER_2000_2003:
            if version is not None or sdk or copy_root_files:
                raise GameValidationError(
                    _(
                        "{runtime}, {sdk}, and {copy_root_file} are only available for NW.js games"
                    ).format(runtime="--runtime", sdk="--sdk", copy_root_file="--copy-root-file")
                )
            runtime = EasyRPGCatalog(paths).latest()
            authorize_game(game, config, repository, read)
            validate_game_descriptor(game, game_descriptor)
            game_reference = descriptor_path(game_descriptor)
            return run_process(
                [
                    str(easyrpg_executable(runtime)),
                    "--project-path",
                    str(game_reference),
                    "--fullscreen",
                ],
                cwd=game_reference,
            )
        runtime = select_runtime(
            RuntimeCatalog(paths),
            current_architecture(),
            config.preferred_runtime if version is None else version,
            sdk or config.prefer_sdk,
        )
        authorize_game(game, config, repository, read)
        validate_game_descriptor(game, game_descriptor)
        with create_session(
            paths, game, copy_root_files, game_descriptor=game_descriptor
        ) as session:
            validate_game_descriptor(game, game_descriptor)
            return run_process(
                build_command(runtime, session.reference, session.profile_root),
                cwd=session.reference,
            )


def authorize_game(
    game: GameInfo,
    config: AppConfig,
    repository: ConfigRepository,
    read: Callable[[str], str] | None = None,
) -> AppConfig:
    """Authorize a game or interactively ask to store its exact root."""
    with open_game_directory(game) as descriptor:
        return _authorize_open_game(game, repository, read, descriptor)


def _authorize_open_game(
    game: GameInfo,
    repository: ConfigRepository,
    read: Callable[[str], str] | None,
    descriptor: int,
) -> AppConfig:
    """Check the pinned identity again after configuration access or user input."""
    config = repository.prune_missing_allowed_roots()
    if any(game.root.is_relative_to(root) for root in config.allowed_game_roots):
        ensure_allowed_root(game, config.allowed_game_roots)
        validate_game_descriptor(game, descriptor)
        return config
    if read is None:
        ensure_allowed_root(game, config.allowed_game_roots)
        validate_game_descriptor(game, descriptor)
        return config
    try:
        answer = (
            read(
                _("Add {path} to allowed game roots? [y/N] ").format(
                    path=safe_terminal_text(game.root)
                )
            )
            .strip()
            .lower()
        )
    except EOFError as exc:
        raise GameValidationError(_("game root was not authorized")) from exc
    if answer not in {"y", "yes"}:
        raise GameValidationError(_("game root was not authorized"))
    validate_game_descriptor(game, descriptor)
    config = repository.add_confirmed_allowed_root(
        game.root, validate=lambda: validate_game_descriptor(game, descriptor)
    )
    ensure_allowed_root(game, config.allowed_game_roots)
    validate_game_descriptor(game, descriptor)
    return config


@contextmanager
def _game_root_descriptor(game_root: Path) -> Generator[int]:
    """Keep a game directory descriptor open throughout authorization and launch."""
    descriptor = open_game_root(game_root)
    try:
        yield descriptor
    finally:
        os.close(descriptor)
