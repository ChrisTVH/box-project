"""Non-interactive launch orchestration for graphical front ends."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

from box.api.interaction import Interaction
from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.engines.registry import default_registry
from box.errors import GameValidationError, LaunchError
from box.games.detector import detect_game, ensure_allowed_root
from box.games.files import open_game_directory, validate_game_descriptor
from box.games.identity import game_id
from box.launch import gamemode as _gamemode
from box.launch.cleanup import remove_session
from box.launch.command import build_command
from box.launch.links import list_root_files as _list_root_files
from box.launch.links import open_game_root
from box.launch.sandbox import Sandbox, validate_tree
from box.launch.session import create_session
from box.launch.supervisor import (
    LaunchedSession,
    check_no_live_session,
    create_supervisor_session,
    is_session_running,
    poll_launch_status,
    spawn_detached,
    stop_session,
)
from box.launch.supervisor import (
    find_live_sessions as _find_live_sessions,
)
from box.models import EngineName, GameInfo, RuntimeInfo
from box.paths import AppPaths
from box.runtime.catalog import RuntimeCatalog
from box.runtime.easyrpg import EasyRPGCatalog, EasyRPGRuntime
from box.runtime.easyrpg import executable as easyrpg_executable
from box.runtime.platform import current_architecture
from box.runtime.selector import matching_runtimes, select_runtime
from box.utils.i18n import _
from box.utils.terminal import safe_terminal_text

__all__ = [
    "LaunchedSession",
    "authorize_game",
    "find_live_sessions",
    "is_gamemode_available",
    "is_session_running",
    "launch",
    "list_root_files",
    "poll_launch_status",
    "stop_session",
]


def find_live_sessions(paths: AppPaths, identifier: str) -> list[str]:
    """List live detached session names for one game identifier."""
    return _find_live_sessions(paths, identifier)


def is_gamemode_available() -> bool:
    """Report whether the GameMode wrapper is usable for a sandboxed launch."""
    return _gamemode.is_available()


def _confirm_x11(sandbox: Sandbox, interaction: Interaction | None) -> None:
    """Require explicit GUI consent before exposing the X11 socket."""
    if interaction is None:
        raise GameValidationError(
            _("explicit consent is required for X11; run interactively to continue")
        )
    display = safe_terminal_text(os.environ.get("DISPLAY", ""))
    try:
        confirmed = interaction.confirm_x11(display)
    except EOFError as exc:
        # Defensive for custom interactions raising EOFError directly;
        # ConsoleInteraction already converts EOF to GameValidationError.
        raise GameValidationError(_("X11 launch was not confirmed")) from exc
    if not confirmed:
        raise GameValidationError(_("X11 launch was not confirmed"))
    sandbox.x11()


def _setup_desktop(
    sandbox: Sandbox,
    interaction: Interaction | None,
    *,
    extra_x11: bool = False,
    force_x11: bool = False,
) -> str:
    """Select the display backend, requiring explicit consent for X11.

    Mirrors box.cli.launch._setup_desktop without any terminal I/O. An
    explicit x11 flag is itself the consent, so no callback runs then.
    """
    if force_x11:
        sandbox.x11()
        return "x11"
    probe = sandbox.display_probe()
    if probe == "wayland":
        sandbox.desktop()
        if extra_x11 and os.environ.get("DISPLAY"):
            _confirm_x11(sandbox, interaction)
        return "wayland"
    if probe == "x11":
        _confirm_x11(sandbox, interaction)
        return "x11"
    sandbox.desktop()
    return "wayland"


def launch(
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
    use_gamemode: bool = False,
    interaction: Interaction | None = None,
) -> LaunchedSession:
    """Launch an allowed game through a detached supervisor without terminal I/O.

    Validates, asks consent (authorize_game, X11 confirm, runtime selection),
    builds the session and Sandbox options, then double-forks a supervisor
    that parents the exact Bubblewrap command. The child never re-prompts.
    Returns once the supervisor confirms startup with a LaunchedSession
    handle (identifier/name/root); it does not wait for game exit. Poll
    poll_launch_status for the exit code, is_session_running for flock
    liveness, and stop_session to terminate. One session per game entry is
    firm: a live session under the same game identifier raises LaunchError
    with "game already running".
    """
    if use_gamemode:
        _gamemode.require_gamemode()
    game = detect_game(game_path, default_registry())
    with _game_root_descriptor(game.root) as game_descriptor:
        validate_game_descriptor(game, game_descriptor)
        config = repository.load()
        if game.engine is EngineName.RPG_MAKER_2000_2003:
            if sdk or copy_root_files:
                raise GameValidationError(
                    _("{sdk} and {copy_root_file} are only available for NW.js games").format(
                        sdk="--sdk", copy_root_file="--copy-root-file"
                    )
                )
            runtime = _select_easyrpg_runtime(EasyRPGCatalog(paths), version, interaction)
            authorize_game(game, config, repository, interaction)
            validate_game_descriptor(game, game_descriptor)
            identifier = _launch_identifier(game)
            check_no_live_session(paths, identifier)
            parent_descriptor = -1
            session_descriptor = -1
            session_name = ""
            session_root: Path | None = None
            try:
                session_name, session_root, parent_descriptor, session_descriptor = (
                    create_supervisor_session(paths, identifier)
                )
                with Sandbox(
                    allow_network=allow_network, allow_game_writes=allow_game_writes
                ) as sandbox:
                    executable = sandbox.runtime(easyrpg_executable(runtime))
                    _setup_desktop(sandbox, interaction, extra_x11=True, force_x11=x11)
                    sandbox.devices()
                    sandbox.audio()
                    if use_gamemode:
                        sandbox.gamemode(session_root / _gamemode.GAMEMODE_PROXY_SOCKET_NAME)
                    sandbox.persistence(paths, game)
                    saves = sandbox.game_saves(game, game_descriptor)
                    validate_tree(game_descriptor)
                    if allow_game_writes:
                        sandbox.game_writable(os.dup(game_descriptor))
                    else:
                        sandbox.bind(sandbox.keep(os.dup(game_descriptor)), "/game")
                    sandbox.bind(saves, "/game/save", writable=True)
                    validate_game_descriptor(game, game_descriptor)
                    payload = [
                        executable,
                        "--project-path",
                        "/game",
                        "--fullscreen",
                        "--save-path",
                        "/game/save",
                    ]
                    if use_gamemode:
                        payload = [str(_gamemode.GAMEMODERUN), *payload]
                    command = sandbox.command(payload, cwd="/game")
                    pass_fds = sandbox.pass_fds
                    handle = spawn_detached(
                        paths,
                        identifier,
                        session_name,
                        command,
                        pass_fds,
                        parent_descriptor=parent_descriptor,
                        session_descriptor=session_descriptor,
                        use_gamemode=use_gamemode,
                        gamemode_proxy=(
                            session_root / _gamemode.GAMEMODE_PROXY_SOCKET_NAME
                            if use_gamemode
                            else None
                        ),
                    )
                return handle
            except BaseException:
                if session_name and session_root is not None and parent_descriptor >= 0:
                    with suppress(OSError, LaunchError):
                        remove_session(
                            paths,
                            session_root,
                            parent_descriptor=parent_descriptor,
                            session_descriptor=session_descriptor
                            if session_descriptor >= 0
                            else None,
                        )
                raise
            finally:
                if parent_descriptor >= 0:
                    with suppress(OSError):
                        os.close(parent_descriptor)
                if session_descriptor >= 0:
                    with suppress(OSError):
                        os.close(session_descriptor)
        runtime_nw = _select_launch_runtime(
            RuntimeCatalog(paths),
            current_architecture(),
            version,
            sdk or config.prefer_sdk,
            config.preferred_runtime,
            interaction,
        )
        authorize_game(game, config, repository, interaction)
        validate_game_descriptor(game, game_descriptor)
        check_no_live_session(paths, _launch_identifier(game))
        with create_session(
            paths, game, copy_root_files, game_descriptor=game_descriptor
        ) as session:
            validate_game_descriptor(game, game_descriptor)
            with Sandbox(
                allow_network=allow_network, allow_game_writes=allow_game_writes
            ) as sandbox:
                executable = sandbox.runtime(runtime_nw.executable)
                display = _setup_desktop(sandbox, interaction, force_x11=x11)
                sandbox.devices()
                sandbox.audio()
                if use_gamemode:
                    sandbox.gamemode(session.root / _gamemode.GAMEMODE_PROXY_SOCKET_NAME)
                sandbox.persistence(paths, game)
                saves = sandbox.game_saves(game, game_descriptor)
                sandbox.nw_game(game, game_descriptor, saves)
                sandbox.bind(sandbox.keep(os.dup(session.session_descriptor)), "/session")
                sandbox.bind(saves, "/session/save", writable=True)
                command = build_command(runtime_nw, Path("/session"), Path("/profile"), display)
                command[0] = executable
                if use_gamemode:
                    command = [str(_gamemode.GAMEMODERUN), *command]
                validate_game_descriptor(game, game_descriptor)
                # Start inside the game view so relative asset paths (such as
                # ./www/...) resolve exactly like a stock export launched from
                # its own root; /session remains the app directory for NW.js.
                full = sandbox.command(command, cwd="/session/game")
                pass_fds = sandbox.pass_fds
                handle = spawn_detached(
                    paths,
                    session.identifier,
                    session.name,
                    full,
                    pass_fds,
                    parent_descriptor=session.parent_descriptor,
                    session_descriptor=session.session_descriptor,
                    use_gamemode=use_gamemode,
                    gamemode_proxy=(
                        session.root / _gamemode.GAMEMODE_PROXY_SOCKET_NAME
                        if use_gamemode
                        else None
                    ),
                )
                session.detach()
                return handle


def _select_easyrpg_runtime(
    catalog: EasyRPGCatalog, version: str | None, interaction: Interaction | None
) -> EasyRPGRuntime:
    """Use an explicit version, the latest runtime, or ask the GUI to choose."""
    if version is not None:
        return catalog.get(version)
    candidates = catalog.list()
    if len(candidates) < 2 or interaction is None:
        return catalog.latest()
    versions = tuple(runtime.version for runtime in candidates)
    title = _("Installed EasyRPG Player runtimes (x64):")
    try:
        selection = interaction.choose_runtime("easyrpg", versions, title)
    except EOFError as exc:
        raise GameValidationError(_("runtime selection was cancelled")) from exc
    if selection is None:
        raise GameValidationError(_("runtime selection was cancelled"))
    if not 0 <= selection < len(candidates):
        raise GameValidationError(_("runtime selection was cancelled"))
    return candidates[selection]


def _select_launch_runtime(
    catalog: RuntimeCatalog,
    architecture: str,
    version: str | None,
    sdk: bool,
    preferred: str | None,
    interaction: Interaction | None,
) -> RuntimeInfo:
    """Use an explicit choice, or ask the GUI when several runtimes qualify."""
    if version is not None or preferred is not None or interaction is None:
        return select_runtime(catalog, architecture, preferred if version is None else version, sdk)
    candidates = matching_runtimes(catalog, architecture, sdk)
    if len(candidates) < 2:
        return select_runtime(catalog, architecture, None, sdk)
    versions = tuple(runtime.spec.version for runtime in candidates)
    flavor = "SDK" if sdk else _("standard")
    title = _("Installed NW.js runtimes ({architecture}, {flavor}):").format(
        architecture=architecture, flavor=flavor
    )
    try:
        selection = interaction.choose_runtime("nwjs", versions, title)
    except EOFError as exc:
        raise GameValidationError(_("runtime selection was cancelled")) from exc
    if selection is None:
        raise GameValidationError(_("runtime selection was cancelled"))
    if not 0 <= selection < len(candidates):
        raise GameValidationError(_("runtime selection was cancelled"))
    return candidates[selection]


def authorize_game(
    game: GameInfo,
    config: AppConfig,
    repository: ConfigRepository,
    interaction: Interaction | None = None,
) -> AppConfig:
    """Authorize a game or ask the GUI to store its exact root."""
    with open_game_directory(game) as descriptor:
        return _authorize_open_game(game, repository, interaction, descriptor)


def list_root_files(game: GameInfo) -> tuple[str, ...]:
    """List game-root filenames available to pass as copy_root_files."""
    return _list_root_files(game.root)


def _authorize_open_game(
    game: GameInfo,
    repository: ConfigRepository,
    interaction: Interaction | None,
    descriptor: int,
) -> AppConfig:
    """Check the pinned identity again after configuration access or GUI input."""
    config = repository.prune_missing_allowed_roots()
    if any(game.root.is_relative_to(root) for root in config.allowed_game_roots):
        ensure_allowed_root(game, config.allowed_game_roots)
        validate_game_descriptor(game, descriptor)
        return config
    if interaction is None:
        ensure_allowed_root(game, config.allowed_game_roots)
        validate_game_descriptor(game, descriptor)
        return config
    try:
        confirmed = interaction.confirm_add_root(game.root)
    except EOFError as exc:
        raise GameValidationError(_("game root was not authorized")) from exc
    if not confirmed:
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


def _launch_identifier(game: GameInfo) -> str:
    """Return the stable per-game session identifier for single-instance checks."""
    try:
        return game_id(game.root)
    except (OSError, RuntimeError) as exc:
        raise LaunchError(
            _("cannot identify game root {root}: {error}").format(root=game.root, error=exc)
        ) from exc
