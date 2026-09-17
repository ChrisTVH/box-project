"""Backend-free threading primitive for GTK pages.

``run_in_thread`` only needs ``threading`` plus ``GLib.idle_add``, so it
lives here instead of ``workers.py`` (whose backend imports would make it
unusable before box-rpg is installed). The backend setup page drives
dependency detection and the install flow through this module; ``workers``
re-exports it so every other caller keeps working unchanged.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from gi.repository import GLib

__all__ = ["run_in_thread"]


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
