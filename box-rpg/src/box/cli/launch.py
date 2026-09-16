"""Launch command implementation."""

# pyright: reportPrivateUsage=false, reportUnusedFunction=false
# NOTE: the shims below intentionally track box.api.launch privates until the
# read-based helpers are removed in a later pass (see plan step 1 follow-up).
from __future__ import annotations

import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from box.api.interaction import ConsoleInteraction
from box.api.launch import _setup_desktop as api_setup_desktop
from box.api.launch import authorize_game as api_authorize_game
from box.api.launch import is_session_running as api_is_running
from box.api.launch import launch as api_launch
from box.api.launch import poll_launch_status as api_poll_status
from box.api.launch import stop_session as api_stop_session
from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.errors import LaunchError
from box.launch.sandbox import Sandbox
from box.models import GameInfo
from box.paths import AppPaths
from box.utils.terminal import abbreviate_prompt_path

_abbreviate_prompt_path = abbreviate_prompt_path

_POLL_INTERVAL = 0.05


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
    gamemode: bool = False,
) -> int:
    """Launch detached, then block in the foreground until the game exits."""
    # Resolve input at call time (not via the default argument) so the
    # terminal reader stays patchable exactly like the former read callable.
    interaction = ConsoleInteraction(read=input) if sys.stdin.isatty() else None
    handle = api_launch(
        paths,
        repository,
        game_path,
        version,
        sdk,
        copy_root_files,
        allow_network=allow_network,
        allow_game_writes=allow_game_writes,
        x11=x11,
        use_gamemode=gamemode,
        interaction=interaction,
    )
    try:
        while True:
            exit_code = api_poll_status(paths, handle.identifier, handle.name)
            if exit_code is not None:
                return exit_code
            if not api_is_running(paths, handle.identifier, handle.name):
                if not handle.root.exists():
                    raise LaunchError("launch session ended without an exit status")
                # Supervisor died without a final status (crash/SIGKILL path);
                # the session directory lingers stale. Report failure closed.
                raise LaunchError("launch session ended without an exit status")
            time.sleep(_POLL_INTERVAL)
    except KeyboardInterrupt:
        with suppress(LaunchError, OSError):
            api_stop_session(paths, handle.identifier, handle.name)
        return 130


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
