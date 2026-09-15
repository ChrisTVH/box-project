"""Background workers keeping blocking calls off the GTK main loop."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from box.api import AppPaths, ConfigRepository
from box.api.diagnose import DiagnoseResult, diagnose
from box.api.inspect import Inspection, inspect
from box.api.interaction import Interaction
from box.api.launch import launch
from gi.repository import GLib

__all__ = ["ProgressReporter", "run_diagnose", "run_in_thread", "run_inspect", "run_launch"]


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
    path: Path,
    on_done: Callable[[Inspection], None],
    on_error: Callable[[BaseException], None],
) -> threading.Thread:
    """Inspect a game folder off the main loop, even though inspect is pure I/O."""
    return run_in_thread(lambda: inspect(path), on_done, on_error)


def run_launch(
    paths: AppPaths,
    repository: ConfigRepository,
    game_path: Path,
    interaction: Interaction | None,
    on_done: Callable[[int], None],
    on_error: Callable[[BaseException], None],
    *,
    version: str | None = None,
    sdk: bool = False,
    copy_root_files: tuple[str, ...] = (),
    allow_network: bool = False,
    allow_game_writes: bool = False,
    x11: bool = False,
) -> threading.Thread:
    """Launch a game off the main loop, passing the launch flags through.

    Mirrors run_inspect: blocking launch runs on a daemon thread while the
    Interaction callbacks marshal their dialogs to the main loop. The
    keyword-only flags default to the previous safe behavior, so existing
    callers keep working unchanged.
    """
    return run_in_thread(
        lambda: launch(
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
        ),
        on_done,
        on_error,
    )


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
