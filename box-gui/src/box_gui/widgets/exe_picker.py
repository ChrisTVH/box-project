"""Single-choice game executable picker dialog.

Mirrors the `choose_runtime` dialog shape: an `Adw.AlertDialog` with a
drop-down of candidates plus Cancel/Select responses. The first row
always offers the engine default icon so a custom icon can be reverted.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from box_gui.i18n import _  # noqa: E402

__all__ = ["build_exe_picker", "present_exe_picker", "resolve_exe_choice"]

ExesChoice = Path | Literal["default"] | None
"""Picker outcome: an executable, the engine default icon, or cancel."""


def build_exe_picker(exes: tuple[Path, ...]) -> tuple[Adw.AlertDialog, Gtk.DropDown]:
    """Build the executable picker; call only on the main thread."""
    dialog = Adw.AlertDialog(
        heading=_("Select the game executable."),
        body=_("Choose which executable provides the library icon."),
    )
    names = [_("Engine default"), *(exe.name for exe in exes)]
    selector = Gtk.DropDown.new_from_strings(names)
    selector.set_selected(0)
    selector.set_hexpand(True)
    dialog.set_extra_child(selector)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("select", _("Select"))
    dialog.set_response_appearance("select", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("select")
    dialog.set_close_response("cancel")
    return dialog, selector


def resolve_exe_choice(selector: Gtk.DropDown, exes: tuple[Path, ...], response: str) -> ExesChoice:
    """Map a dialog response plus selector state to the picker outcome."""
    if response != "select":
        return None
    selected = selector.get_selected()
    if selected == 0:
        return "default"
    index = int(selected) - 1
    if 0 <= index < len(exes):
        return exes[index]
    return None


def present_exe_picker(
    parent: Gtk.Widget,
    exes: tuple[Path, ...],
    on_chosen: Callable[[ExesChoice], None],
) -> None:
    """Present the picker; report the executable, default, or cancel."""

    def _on_response(dialog: Adw.AlertDialog, response: str) -> None:
        on_chosen(resolve_exe_choice(selector, exes, response))

    dialog, selector = build_exe_picker(exes)
    dialog.connect("response", _on_response)
    dialog.present(parent)
