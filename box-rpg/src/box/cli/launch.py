"""Launch command implementation."""

# pyright: reportPrivateUsage=false, reportUnusedFunction=false
# NOTE: the shims below intentionally track box.api.launch privates until the
# read-based helpers are removed in a later pass (see plan step 1 follow-up).
from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from box.api.interaction import ConsoleInteraction
from box.api.launch import _setup_desktop as api_setup_desktop
from box.api.launch import authorize_game as api_authorize_game
from box.api.launch import launch as api_launch
from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.launch.sandbox import Sandbox
from box.models import GameInfo
from box.paths import AppPaths
from box.utils.terminal import abbreviate_prompt_path

_abbreviate_prompt_path = abbreviate_prompt_path


def execute(
    paths: AppPaths,
    repository: ConfigRepository,
    game_path: Path,
    version: str | None,
    sdk: bool,
    copy_root_files: tuple[str, ...] = (),
    *,
    allow_network: bool = False,
    allow_game_writes: bool = False,
    x11: bool = False,
) -> int:
    """Launch an allowed game through an isolated session."""
    # Resolve input at call time (not via the default argument) so the
    # terminal reader stays patchable exactly like the former read callable.
    interaction = ConsoleInteraction(read=input) if sys.stdin.isatty() else None
    return api_launch(
        paths,
        repository,
        game_path,
        version,
        sdk,
        copy_root_files,
        allow_network=allow_network,
        allow_game_writes=allow_game_writes,
        x11=x11,
        interaction=interaction,
    )


def authorize_game(
    game: GameInfo,
    config: AppConfig,
    repository: ConfigRepository,
    read: Callable[[str], str] | None = None,
) -> AppConfig:
    """Authorize a game or interactively ask to store its exact root."""
    interaction = ConsoleInteraction(read=read) if read is not None else None
    return api_authorize_game(game, config, repository, interaction)


def _setup_desktop(
    sandbox: Sandbox,
    read: Callable[[str], str] | None,
    *,
    extra_x11: bool = False,
    force_x11: bool = False,
) -> str:
    """Select the display backend, requiring explicit consent for X11."""
    interaction = ConsoleInteraction(read=read) if read is not None else None
    return api_setup_desktop(sandbox, interaction, extra_x11=extra_x11, force_x11=force_x11)
