"""Shared GTK worker-thread helpers for headless-safe GUI tests."""

# pyright: reportMissingImports=false

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

import pytest

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")

    from gi.repository import Adw, GLib, Gtk

    import box_gui.gtk.interaction as interaction_module
    from box_gui.gtk.interaction import GtkInteraction

    _gi_available = True
except Exception:
    interaction_module: Any = None
    GtkInteraction: Any = None
    Adw: Any = None
    GLib: Any = None
    Gtk: Any = None
    _gi_available = False

__all__ = [
    "_gi_available",
    "_has_display",
    "_install_auto_answer",
    "_require_display",
    "_run_from_worker",
]


def _has_display() -> bool:
    """Return True when a Wayland or X11 display looks available."""
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _require_display() -> None:
    """Skip the test when no display is available for real dialogs."""
    if not _has_display():
        pytest.skip("no display for GtkInteraction threads")


def _install_auto_answer(
    monkeypatch: pytest.MonkeyPatch,
    response: str | None,
    select_index: int | None = None,
    *,
    close: bool = False,
    close_first: bool = False,
) -> None:
    """Patch present to schedule a dialog response without clicks.

    When response is None nobody answers and the deadlock safeguard timeout
    must fire. When close is True the dialog emits closed instead of a
    response. When close_first is True closed is emitted synchronously
    before response, mirroring Adw emitting closed first for an activated
    button; the response must still win. Otherwise the response is emitted
    via GLib.idle_add after present, optionally selecting select_index
    first for runtime pickers.
    """
    if not _gi_available:
        return

    def _fake_present(self: Any, parent: Any | None = None) -> None:
        if response is None and not close:
            return

        def _answer() -> bool:
            if close:
                # Emit closed directly: close()/force_close() on a never
                # presented dialog only warn and never emit the signal.
                self.emit("closed")
                return False
            if select_index is not None and isinstance(self, Adw.AlertDialog):
                child = self.get_extra_child()
                if isinstance(child, Gtk.DropDown):
                    child.set_selected(select_index)
            if close_first:
                # Mirror an activated button: closed first, then the
                # already-chosen response. Route the automatic
                # close-response through the allow id so the closed
                # emission cannot settle on denial by itself.
                self.set_close_response(response)
                self.emit("closed")
                return False
            self.emit("response", response)
            return False

        GLib.idle_add(_answer)

    monkeypatch.setattr(Adw.AlertDialog, "present", _fake_present)


def _run_from_worker(
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[Any], Any],
    response: str | None,
    *,
    select_index: int | None = None,
    timeout_s: float | None = None,
    close: bool = False,
    close_first: bool = False,
) -> Any:
    """Run call(interaction) on a worker thread with a running MainLoop.

    The MainLoop here stands in for the Adw.Application main loop. The
    worker blocks inside GtkInteraction while the main loop dispatches the
    idle-built dialog and the scheduled auto-answer. Never waits forever:
    the loop has a 10s quit safeguard and join/wait always use timeouts.
    """
    _require_display()
    if timeout_s is not None:
        monkeypatch.setattr(interaction_module, "DIALOG_RESPONSE_TIMEOUT_S", timeout_s)
    _install_auto_answer(monkeypatch, response, select_index, close=close, close_first=close_first)
    interaction = GtkInteraction(parent=None)
    loop = GLib.MainLoop()
    outcome: dict[str, Any] = {}
    finished = threading.Event()

    def _worker() -> None:
        try:
            outcome["result"] = call(interaction)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            finished.set()
            GLib.idle_add(loop.quit)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    safeguard_id = GLib.timeout_add(10000, loop.quit)
    loop.run()
    GLib.source_remove(safeguard_id)
    assert finished.wait(timeout=10.0), "worker did not finish"
    worker.join(timeout=10.0)
    assert not worker.is_alive(), "worker thread hung"
    if "error" in outcome:
        error = outcome["error"]
        assert isinstance(error, BaseException)
        raise error
    return outcome.get("result")
