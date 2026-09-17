"""Background workers keeping blocking calls off the GTK main loop."""

from __future__ import annotations

import inspect as stdlib_inspect
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from box.api import AppPaths, ConfigRepository
from box.api.diagnose import DiagnoseResult, diagnose
from box.api.inspect import Inspection, inspect
from box.api.interaction import Interaction
from box.api.launch import launch
from box.errors import LaunchError
from gi.repository import GLib

from box_gui.i18n import _

__all__ = [
    "ProgressReporter",
    "run_diagnose",
    "run_in_thread",
    "run_inspect",
    "run_launch",
    "run_stop",
]


def run_in_thread[T](
    fn: Callable[[], T],
    on_done: Callable[[T], None],
    on_error: Callable[[BaseException], None],
) -> threading.Thread:
    """Run fn on a daemon thread, marshaling the outcome via GLib.idle_add."""

    def _target() -> None:
        try:
            result = fn()
        except BaseException as exc:
            GLib.idle_add(on_error, exc)
        else:
            GLib.idle_add(on_done, result)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread


def run_diagnose(
    paths: AppPaths,
    repository: ConfigRepository,
    game_path: Path,
    on_done: Callable[[DiagnoseResult], None],
    on_error: Callable[[BaseException], None],
) -> threading.Thread:
    """Collect diagnostics off the main loop with fixed safe flags."""
    return run_in_thread(
        lambda: diagnose(paths, repository, game_path, version=None, sdk=False),
        on_done,
        on_error,
    )


def run_inspect(
    paths: AppPaths,
    path: Path,
    on_done: Callable[[Inspection], None],
    on_error: Callable[[BaseException], None],
) -> threading.Thread:
    """Inspect a game folder off the main loop, unpacking packed sources first.

    Packed single-executable directories unpack into their source-keyed
    profile on first use (a cache hit afterwards); the inspection root
    stays the source folder so launching it agrees with session tracking.
    """
    return run_in_thread(lambda: inspect(paths, path), on_done, on_error)


def run_launch(
    paths: AppPaths,
    repository: ConfigRepository,
    game_path: Path,
    interaction: Interaction | None,
    on_done: Callable[[Any], None],
    on_error: Callable[[BaseException], None],
    *,
    version: str | None = None,
    sdk: bool = False,
    copy_root_files: tuple[str, ...] = (),
    allow_network: bool = False,
    allow_game_writes: bool = False,
    x11: bool = False,
    gamemode: bool = False,
    ci_mount: bool = False,
) -> threading.Thread:
    """Launch a game off the main loop, passing the launch flags through.

    Mirrors run_inspect: blocking launch runs on a daemon thread while the
    Interaction callbacks marshal their dialogs to the main loop. The
    keyword-only flags default to the previous safe behavior, so existing
    callers keep working unchanged.
    """
    return run_in_thread(
        lambda: _launch_with_gamemode(
            paths,
            repository,
            game_path,
            interaction,
            version=version,
            sdk=sdk,
            copy_root_files=copy_root_files,
            allow_network=allow_network,
            allow_game_writes=allow_game_writes,
            x11=x11,
            gamemode=gamemode,
            ci_mount=ci_mount,
        ),
        on_done,
        on_error,
    )


def _launch_with_gamemode(
    paths: AppPaths,
    repository: ConfigRepository,
    game_path: Path,
    interaction: Interaction | None,
    *,
    version: str | None,
    sdk: bool,
    copy_root_files: tuple[str, ...],
    allow_network: bool,
    allow_game_writes: bool,
    x11: bool,
    gamemode: bool,
    ci_mount: bool = False,
) -> Any:
    """Invoke launch with GameMode and case-insensitive mount detection.

    Old box-rpg releases lack the use_gamemode and ci_mount keywords:
    proceed without them when neither was requested, but fail closed with
    LaunchError when either was, instead of silently launching without the
    requested flag. Current backends additionally reject ci_mount for
    2000/2003 games; the pages force it off for EasyRPG before calling here.
    New backends return a LaunchedSession handle (detached); old ones
    return an int exit code. Both flow through run_in_thread into on_done.
    Raising here routes through run_in_thread into on_error as usual.
    """
    try:
        launch_parameters = stdlib_inspect.signature(launch).parameters
    except TypeError, ValueError:
        launch_parameters = {}
    supports_gamemode = "use_gamemode" in launch_parameters
    supports_ci_mount = "ci_mount" in launch_parameters
    if ci_mount and not supports_ci_mount:
        raise LaunchError(_("Case-insensitive mount is not supported by the installed backend."))
    if gamemode and not supports_gamemode:
        raise LaunchError(_("GameMode is not supported by the installed backend."))
    extra_flags: dict[str, Any] = {}
    if supports_gamemode:
        extra_flags["use_gamemode"] = gamemode
    if supports_ci_mount:
        extra_flags["ci_mount"] = ci_mount
    return launch(
        paths,
        repository,
        game_path,
        version=version,
        sdk=sdk,
        copy_root_files=copy_root_files,
        allow_network=allow_network,
        allow_game_writes=allow_game_writes,
        x11=x11,
        interaction=interaction,
        **extra_flags,
    )


def run_stop(
    paths: AppPaths,
    entry: Any,
    name: str | None,
    on_done: Callable[[None], None],
    on_error: Callable[[BaseException], None],
) -> threading.Thread:
    """Stop a running session off the main loop with old-backend fallback."""
    from box_gui.core.sessions import stop_session

    return run_in_thread(lambda: stop_session(paths, entry, name), on_done, on_error)


class ProgressReporter:
    """Forward install progress to the GTK main loop.

    The call signature matches the stable box-rpg progress callback
    ``(completed, total)`` so a future install flow can pass this object
    directly as its progress reporter.
    """

    def __init__(self, on_progress: Callable[[int, int | None], None]) -> None:
        self._on_progress = on_progress

    def __call__(self, completed: int, total: int | None) -> None:
        """Schedule a progress update on the main loop."""
        GLib.idle_add(self._on_progress, completed, total)

    def report(self, completed: int, total: int | None) -> None:
        """Alias for calling this reporter directly."""
        self(completed, total)
