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
from box.launch.links import open_game_root
from box.launch.process import run_process
from box.launch.sandbox import Sandbox, validate_tree
from box.launch.session import create_session
from box.models import EngineName, GameInfo, RuntimeInfo
from box.paths import AppPaths
from box.runtime.catalog import RuntimeCatalog
from box.runtime.easyrpg import EasyRPGCatalog, EasyRPGRuntime
from box.runtime.easyrpg import executable as easyrpg_executable
from box.runtime.platform import current_architecture
from box.runtime.selector import matching_runtimes, select_runtime
from box.utils.i18n import _
from box.utils.terminal import safe_terminal_text


def _confirm_x11(sandbox: Sandbox, read: Callable[[str], str] | None) -> None:
    """Warn, require an explicit yes, and expose the X11 socket."""
    if read is None:
        raise GameValidationError(
            _("explicit consent is required for X11; run interactively to continue")
        )
    display = safe_terminal_text(os.environ.get("DISPLAY", ""))
    print(
        _(
            "warning: X11 display {display} is insecure; "
            "X11 clients can capture input and screen contents (keylogging)."
        ).format(display=display),
        file=sys.stderr,
    )
    try:
        answer = read(_("Continue with X11? [y/N] ")).strip().lower()
    except EOFError as exc:
        raise GameValidationError(_("X11 launch was not confirmed")) from exc
    if answer not in {"y", "yes"}:
        raise GameValidationError(_("X11 launch was not confirmed"))
    sandbox.x11()


def _setup_desktop(
    sandbox: Sandbox, read: Callable[[str], str] | None, *, extra_x11: bool = False
) -> str:
    """Select the display backend, requiring explicit consent for X11.

    Runtimes without Wayland support (such as the EasyRPG static build) pass
    extra_x11 so the local X11 socket can additionally be exposed after the
    same confirmation; declining aborts the launch either way.
    """
    probe = sandbox.display_probe()
    if probe == "wayland":
        sandbox.desktop()
        if extra_x11 and os.environ.get("DISPLAY"):
            _confirm_x11(sandbox, read)
        return "wayland"
    if probe == "x11":
        _confirm_x11(sandbox, read)
        return "x11"
    sandbox.desktop()
    return "wayland"


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
) -> int:
    """Launch an allowed game through an isolated session."""
    game = detect_game(game_path, default_registry())
    with _game_root_descriptor(game.root) as game_descriptor:
        validate_game_descriptor(game, game_descriptor)
        config = repository.load()
        read = input if sys.stdin.isatty() else None
        if game.engine is EngineName.RPG_MAKER_2000_2003:
            if sdk or copy_root_files:
                raise GameValidationError(
                    _("{sdk} and {copy_root_file} are only available for NW.js games").format(
                        sdk="--sdk", copy_root_file="--copy-root-file"
                    )
                )
            runtime = _select_easyrpg_runtime(EasyRPGCatalog(paths), version, read)
            authorize_game(game, config, repository, read)
            validate_game_descriptor(game, game_descriptor)
            with Sandbox(
                allow_network=allow_network, allow_game_writes=allow_game_writes
            ) as sandbox:
                executable = sandbox.runtime(easyrpg_executable(runtime))
                _setup_desktop(sandbox, read, extra_x11=True)
                sandbox.devices()
                sandbox.audio()
                sandbox.persistence(paths, game)
                saves = sandbox.game_saves(game, game_descriptor)
                validate_tree(game_descriptor)
                if allow_game_writes:
                    sandbox.game_writable(os.dup(game_descriptor))
                else:
                    sandbox.bind(sandbox.keep(os.dup(game_descriptor)), "/game")
                sandbox.bind(saves, "/game/save", writable=True)
                validate_game_descriptor(game, game_descriptor)
                return run_process(
                    sandbox.command(
                        [
                            executable,
                            "--project-path",
                            "/game",
                            "--fullscreen",
                            "--save-path",
                            "/game/save",
                        ],
                        cwd="/game",
                    ),
                    pass_fds=sandbox.pass_fds,
                )
        runtime = _select_launch_runtime(
            RuntimeCatalog(paths),
            current_architecture(),
            version,
            sdk or config.prefer_sdk,
            config.preferred_runtime,
            read,
        )
        authorize_game(game, config, repository, read)
        validate_game_descriptor(game, game_descriptor)
        with create_session(
            paths, game, copy_root_files, game_descriptor=game_descriptor
        ) as session:
            validate_game_descriptor(game, game_descriptor)
            with Sandbox(
                allow_network=allow_network, allow_game_writes=allow_game_writes
            ) as sandbox:
                executable = sandbox.runtime(runtime.executable)
                display = _setup_desktop(sandbox, read)
                sandbox.devices()
                sandbox.audio()
                sandbox.persistence(paths, game)
                saves = sandbox.game_saves(game, game_descriptor)
                sandbox.nw_game(game, game_descriptor, saves)
                sandbox.bind(sandbox.keep(os.dup(session.session_descriptor)), "/session")
                sandbox.bind(saves, "/session/save", writable=True)
                command = build_command(runtime, Path("/session"), Path("/profile"), display)
                command[0] = executable
                validate_game_descriptor(game, game_descriptor)
                return run_process(
                    sandbox.command(command, cwd="/session"), pass_fds=sandbox.pass_fds
                )


def _select_easyrpg_runtime(
    catalog: EasyRPGCatalog, version: str | None, read: Callable[[str], str] | None
) -> EasyRPGRuntime:
    """Use an explicit version, the latest runtime, or ask when several qualify."""
    if version is not None:
        return catalog.get(version)
    candidates = catalog.list()
    if len(candidates) < 2 or read is None:
        return catalog.latest()
    print(_("Installed EasyRPG Player runtimes (x64):"))
    for index, runtime in enumerate(candidates, start=1):
        print(f"  {index}. {runtime.version}")
    while True:
        try:
            answer = read(
                _("Select an EasyRPG Player runtime 1-{count} (default 1), or [q]uit: ").format(
                    count=len(candidates)
                )
            ).strip()
        except EOFError as exc:
            raise GameValidationError(_("runtime selection was cancelled")) from exc
        if answer == "":
            return candidates[0]
        if answer.lower() == "q":
            raise GameValidationError(_("runtime selection was cancelled"))
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print(_("Invalid selection."))


def _select_launch_runtime(
    catalog: RuntimeCatalog,
    architecture: str,
    version: str | None,
    sdk: bool,
    preferred: str | None,
    read: Callable[[str], str] | None,
) -> RuntimeInfo:
    """Use an explicit choice, or ask when several installed runtimes qualify."""
    if version is not None or preferred is not None or read is None:
        return select_runtime(catalog, architecture, preferred if version is None else version, sdk)
    candidates = matching_runtimes(catalog, architecture, sdk)
    if len(candidates) < 2:
        return select_runtime(catalog, architecture, None, sdk)
    flavor = "SDK" if sdk else _("standard")
    print(
        _("Installed NW.js runtimes ({architecture}, {flavor}):").format(
            architecture=architecture, flavor=flavor
        )
    )
    for index, runtime in enumerate(candidates, start=1):
        print(f"  {index}. {runtime.spec.version}")
    while True:
        try:
            answer = read(
                _("Select an NW.js runtime 1-{count} (default 1), or [q]uit: ").format(
                    count=len(candidates)
                )
            ).strip()
        except EOFError as exc:
            raise GameValidationError(_("runtime selection was cancelled")) from exc
        if answer == "":
            return candidates[0]
        if answer.lower() == "q":
            raise GameValidationError(_("runtime selection was cancelled"))
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print(_("Invalid selection."))


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
