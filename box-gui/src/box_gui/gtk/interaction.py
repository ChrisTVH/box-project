"""GTK implementation of the stable box-rpg Interaction protocol."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from box.api.interaction import Interaction  # noqa: E402
from box.errors import GameValidationError  # noqa: E402
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from box_gui.i18n import _  # noqa: E402

__all__ = ["DIALOG_RESPONSE_TIMEOUT_S", "AllowX11Interaction", "GtkInteraction"]

DIALOG_RESPONSE_TIMEOUT_S: float = 300.0
"""Deadlock safeguard for worker-thread dialogs, in seconds.

Five minutes; only fires when the main loop never delivers a response or
close. It resolves to denial (False/None) and is never a user-visible time
limit. Monkeypatchable in tests for a fast nobody-responds case.
"""


class AllowX11Interaction:
    """Delegate Interaction that pre-answers the X11 consent prompt.

    The game persists "Allow X11 or XWayland sessions" per entry, so a
    launch with that flag on passes ``x11=True`` (explicit consent, which
    the backend honors without prompting) together with this wrapper. The
    wrapper answers ``confirm_x11`` affirmatively so no second dialog can
    fire on any residual prompt path; every other prompt delegates to the
    wrapped interaction, denying when there is none.
    """

    def __init__(self, wrapped: Interaction | None) -> None:
        self._wrapped = wrapped

    def confirm_x11(self, display: str) -> bool:
        """Pre-answer X11 consent affirmatively for this launch."""
        return True

    def confirm_add_root(self, path: Path) -> bool:
        """Delegate game-root consent to the wrapped interaction."""
        if self._wrapped is None:
            return False
        return self._wrapped.confirm_add_root(path)

    def choose_runtime(self, kind: str, candidates: tuple[str, ...], title: str) -> int | None:
        """Delegate runtime selection to the wrapped interaction."""
        if self._wrapped is None:
            return None
        return self._wrapped.choose_runtime(kind, candidates, title)


class GtkInteraction(Interaction):
    """Present Interaction prompts with real Adw.AlertDialog windows.

    Thread-safe bridge for blocking launch flows: each method detects the
    calling thread. On the main thread it presents with a nested MainLoop.
    On a worker thread it marshals dialog construction, signal connection,
    and present entirely inside a GLib.idle_add callback on the main loop,
    then blocks the caller on a threading.Event until response, close, or
    timeout wakes it. Close and timeout resolve to denial (False/None).

    The choose_runtime title is used verbatim as the dialog heading and the
    selector value is read on the main thread before waking the worker;
    Gtk.INVALID_LIST_POSITION or out-of-range means None.

    Call set_parent on the main thread before launch; its value is captured
    for the main-loop callback. These dialogs need a display and were left
    without manual visual testing in this pass.
    """

    def __init__(self, parent: Gtk.Window | None = None) -> None:
        self._parent = parent

    def set_parent(self, parent: Gtk.Window | None) -> None:
        """Update the window parenting future dialogs.

        Must be called on the main thread before launch starts.
        """
        self._parent = parent

    def confirm_x11(self, display: str) -> bool:
        """Ask for consent to expose an insecure X11 display."""
        if threading.current_thread() is threading.main_thread():
            dialog = self._make_confirm_x11_dialog(display)
            return self._present_blocking(dialog) == "continue"

        def _build() -> tuple[Adw.AlertDialog, Callable[[str | None], bool]]:
            dialog = self._make_confirm_x11_dialog(display)

            def _resolve(response: str | None) -> bool:
                return response == "continue"

            return dialog, _resolve

        return self._ask_worker(False, _build)

    def confirm_add_root(self, path: Path) -> bool:
        """Ask to store a game folder as an allowed game root."""
        if threading.current_thread() is threading.main_thread():
            dialog = self._make_confirm_add_root_dialog(path)
            return self._present_blocking(dialog) == "add"

        def _build() -> tuple[Adw.AlertDialog, Callable[[str | None], bool]]:
            dialog = self._make_confirm_add_root_dialog(path)

            def _resolve(response: str | None) -> bool:
                return response == "add"

            return dialog, _resolve

        return self._ask_worker(False, _build)

    def choose_runtime(self, kind: str, candidates: tuple[str, ...], title: str) -> int | None:
        """Return the selected runtime index, or None to cancel.

        The title is shown verbatim as the dialog heading and the
        candidates appear in an extra-child drop-down selector.
        """
        if kind not in {"nwjs", "easyrpg"}:
            raise GameValidationError("runtime selection was cancelled")
        if not candidates:
            raise GameValidationError("runtime selection was cancelled")
        if threading.current_thread() is threading.main_thread():
            dialog, selector = self._make_choose_runtime_dialog(kind, candidates, title)
            if self._present_blocking(dialog) != "select":
                return None
            return self._read_runtime_selection(selector, candidates)

        def _build() -> tuple[Adw.AlertDialog, Callable[[str | None], int | None]]:
            dialog, selector = self._make_choose_runtime_dialog(kind, candidates, title)

            def _resolve(response: str | None) -> int | None:
                if response != "select":
                    return None
                return self._read_runtime_selection(selector, candidates)

            return dialog, _resolve

        return self._ask_worker(None, _build)

    def _make_confirm_x11_dialog(self, display: str) -> Adw.AlertDialog:
        """Build the X11 consent dialog; call only on the main thread."""
        dialog = Adw.AlertDialog(
            heading=_("Continue with X11?"),
            body=_(
                "Display {display} is insecure; X11 clients can capture "
                "input and screen contents (keylogging)."
            ).format(display=display),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("continue", _("Continue"))
        dialog.set_response_appearance("continue", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        return dialog

    def _make_confirm_add_root_dialog(self, path: Path) -> Adw.AlertDialog:
        """Build the add-root dialog; call only on the main thread."""
        dialog = Adw.AlertDialog(
            heading=_("Add game root?"),
            body=_("Add {path} to allowed game roots?").format(path=path),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("add", _("Add"))
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        return dialog

    def _make_choose_runtime_dialog(
        self, kind: str, candidates: tuple[str, ...], title: str
    ) -> tuple[Adw.AlertDialog, Gtk.DropDown]:
        """Build the runtime picker; call only on the main thread."""
        if kind == "easyrpg":
            body = _("Select an EasyRPG Player runtime.")
        else:
            body = _("Select an NW.js runtime.")
        dialog = Adw.AlertDialog(heading=title, body=body)
        selector = Gtk.DropDown.new_from_strings(list(candidates))
        selector.set_selected(0)
        selector.set_hexpand(True)
        dialog.set_extra_child(selector)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("select", _("Select"))
        dialog.set_response_appearance("select", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("select")
        dialog.set_close_response("cancel")
        return dialog, selector

    @staticmethod
    def _read_runtime_selection(selector: Gtk.DropDown, candidates: tuple[str, ...]) -> int | None:
        """Read the selector on the main thread; invalid means None."""
        selected = selector.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected < 0 or selected >= len(candidates):
            return None
        return int(selected)

    def _present_blocking(self, dialog: Adw.AlertDialog) -> str | None:
        """Present a dialog and block until it emits a response."""
        chosen: list[str | None] = [None]
        finished = False
        loop = GLib.MainLoop()

        def _quit_once() -> None:
            nonlocal finished
            if not finished:
                finished = True
                loop.quit()

        def _on_response(source: Adw.AlertDialog, response: str) -> None:
            if finished:
                return
            chosen[0] = response
            _quit_once()

        def _on_closed(source: Adw.AlertDialog) -> None:
            # Adw may emit closed before response for an activated button;
            # defer the quit so a synchronously-following response still wins.
            GLib.idle_add(_quit_once)

        dialog.connect("response", _on_response)
        dialog.connect("closed", _on_closed)
        dialog.present(self._parent)
        loop.run()
        return chosen[0]

    def _ask_worker[T](
        self, deny: T, build: Callable[[], tuple[Adw.AlertDialog, Callable[[str | None], T]]]
    ) -> T:
        """Marshal dialog work to the main loop and block the worker.

        The build callback runs on the main thread inside GLib.idle_add, so
        dialog construction happens there. Signal connection and present run
        in the same main-loop callback. The worker blocks on a threading
        event until response, close, or the deadlock safeguard timeout wakes
        it; all three resolve through the same denial-aware path.
        """
        parent = self._parent
        timeout_s = DIALOG_RESPONSE_TIMEOUT_S
        timeout_ms = max(1, int(timeout_s * 1000))
        result: list[T] = [deny]
        done = threading.Event()
        keep: dict[str, object] = {}

        def _on_main() -> bool:
            try:
                dialog, resolve = build()
            except Exception:
                result[0] = deny
                done.set()
                return False
            keep["dialog"] = dialog
            keep["resolve"] = resolve
            settled: list[bool] = [False]
            timeout_id: list[int] = []
            timeout_armed: list[bool] = [True]

            def _finish(value: T) -> None:
                if not settled[0]:
                    settled[0] = True
                    result[0] = value
                    done.set()

            def _disarm_timeout() -> None:
                if timeout_armed[0]:
                    timeout_armed[0] = False
                    GLib.source_remove(timeout_id[0])

            def _on_response(source: Adw.AlertDialog, response: str) -> None:
                if settled[0]:
                    return
                try:
                    value = resolve(response)
                except Exception:
                    value = deny
                _disarm_timeout()
                _finish(value)

            def _deny_if_unsettled() -> bool:
                _finish(deny)
                return False

            def _on_closed(source: Adw.AlertDialog) -> None:
                # Adw may emit closed before response for an activated
                # button; defer denial so a synchronously-following
                # response still wins. The settled guard drops this when
                # a response already resolved the dialog.
                GLib.idle_add(_deny_if_unsettled)

            def _on_timeout() -> bool:
                if settled[0]:
                    return False
                try:
                    dialog.force_close()
                finally:
                    _finish(deny)
                return False

            dialog.connect("response", _on_response)
            dialog.connect("closed", _on_closed)
            keep["on_response"] = _on_response
            keep["on_closed"] = _on_closed
            keep["on_timeout"] = _on_timeout
            keep["deny_if_unsettled"] = _deny_if_unsettled
            source_id = GLib.timeout_add(timeout_ms, _on_timeout)
            timeout_id.append(int(source_id))
            keep["timeout_id"] = int(source_id)
            try:
                dialog.present(parent)
            except Exception:
                _disarm_timeout()
                _finish(deny)
            return False

        GLib.idle_add(_on_main)
        done.wait(timeout=timeout_s + 5.0)
        return result[0]
