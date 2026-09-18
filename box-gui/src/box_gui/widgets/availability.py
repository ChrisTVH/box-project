"""Reusable unavailable-feature indicator for preference rows.

Game detail rows share one pattern when a backend feature is missing:
a warning icon with an explanatory tooltip plus an insensitive row so
the option reads as unavailable instead of failing later. Switch rows
additionally force their toggle off under the page loading guard.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from box_gui.gtk.icons import WARNING_ICON_NAME  # noqa: E402

__all__ = ["RowAvailability"]


class RowAvailability:
    """Attach a warning icon to a row while its feature is unavailable.

    The indicator owns at most one ``Gtk.Image`` suffix: the first
    ``mark_unavailable`` call creates it, later calls only refresh the
    tooltip so reason updates (EasyRPG versus generic) never duplicate
    the icon. ``mark_available`` detaches the icon and re-enables the
    row. Switch-specific helpers add the forced-off/restore dance
    around the same warning handling.
    """

    def __init__(self, row: Adw.ActionRow) -> None:
        """Remember the row whose sensitivity and suffix are managed."""
        self._row = row
        self._warning: Gtk.Image | None = None

    @property
    def row(self) -> Adw.ActionRow:
        """Return the managed row."""
        return self._row

    @property
    def warning(self) -> Gtk.Image | None:
        """Return the attached warning icon, if any."""
        return self._warning

    @property
    def is_unavailable(self) -> bool:
        """Return True while the warning icon is attached."""
        return self._warning is not None

    def mark_unavailable(self, reason: str) -> None:
        """Gray the row and show the warning with one reason tooltip.

        The icon is created exactly once; repeat calls only update the
        tooltip so callers can switch reasons without stacking icons.
        """
        if self._warning is None:
            warning = Gtk.Image.new_from_icon_name(WARNING_ICON_NAME)
            warning.set_tooltip_text(reason)
            self._row.add_suffix(warning)
            self._warning = warning
        else:
            self._warning.set_tooltip_text(reason)
        self._row.set_sensitive(False)

    def mark_available(self) -> None:
        """Clear the warning and re-enable the row."""
        if self._warning is not None:
            self._row.remove(self._warning)
            self._warning = None
        self._row.set_sensitive(True)

    def disable_switch(
        self,
        reason: str,
        set_loading: Callable[[bool], None],
        persist_disabled: Callable[[], None],
    ) -> None:
        """Force the wrapped switch off, warn, and persist the False value.

        The toggle moves under the loading guard so the page ignores
        the programmatic change; the persist callback then clears any
        stale True from storage.
        """
        row = self._row
        if isinstance(row, Adw.SwitchRow):
            set_loading(True)
            try:
                row.set_active(False)
            finally:
                set_loading(False)
        self.mark_unavailable(reason)
        persist_disabled()

    def enable_switch(
        self,
        active: bool,
        set_loading: Callable[[bool], None],
    ) -> None:
        """Clear the warning, enable the row, and restore the toggle.

        The persisted value is applied under the loading guard only
        when it differs, so user toggles still persist normally.
        """
        self.mark_available()
        row = self._row
        if isinstance(row, Adw.SwitchRow) and row.get_active() != active:
            set_loading(True)
            try:
                row.set_active(active)
            finally:
                set_loading(False)
