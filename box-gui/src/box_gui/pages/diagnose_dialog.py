"""Per-game diagnostics dialog."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from box.api import AppPaths, ConfigRepository  # noqa: E402
from box.api.diagnose import DiagnoseResult  # noqa: E402
from box.errors import BoxError  # noqa: E402
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from box_gui.i18n import _  # noqa: E402

__all__ = ["DiagnoseDialog", "diagnose_error_heading"]


def diagnose_error_heading(error: BaseException) -> str:
    """Return the alert heading for a diagnostics failure."""
    if isinstance(error, BoxError):
        return _("Diagnose Failed")
    return _("Unexpected Error")


# Adw.Dialog requires libadwaita >= 1.5.
class DiagnoseDialog(Adw.Dialog):
    """Transient diagnostics view for a single game."""

    def __init__(
        self,
        paths: AppPaths,
        repository: ConfigRepository,
        game_path: Path,
    ) -> None:
        super().__init__(title=_("Diagnose"), content_width=480, content_height=480)
        self._paths = paths
        self._repository = repository
        self._game_path = game_path
        self._spinner = Gtk.Spinner()
        self._status = Gtk.Label(label=_("Collecting diagnostics …"))
        self._status.set_xalign(0.0)
        self._status.set_hexpand(True)
        self._status.set_ellipsize(Pango.EllipsizeMode.END)
        self._environment_group = Adw.PreferencesGroup(title=_("Environment"))
        self._versions_group = Adw.PreferencesGroup(title=_("Versions"))
        self._system_row = Adw.ActionRow(title=_("System"), subtitle="—")
        self._release_row = Adw.ActionRow(title=_("Release"), subtitle="—")
        self._machine_row = Adw.ActionRow(title=_("Machine"), subtitle="—")
        self._engine_row = Adw.ActionRow(title=_("Engine"), subtitle="—")
        self._version_rows: list[Adw.ActionRow] = []
        self.set_child(self._build_view())
        self._spinner.start()
        self._start_diagnose()

    def _build_view(self) -> Adw.ToolbarView:
        """Assemble the header, status row, and detail groups."""
        view = Adw.ToolbarView()
        view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_box.append(self._spinner)
        status_box.append(self._status)
        self._status_box = status_box
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.append(status_box)
        self._environment_group.add(self._system_row)
        self._environment_group.add(self._release_row)
        self._environment_group.add(self._machine_row)
        self._versions_group.add(self._engine_row)
        content.append(self._environment_group)
        content.append(self._versions_group)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        clamp = Adw.Clamp(child=content)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(clamp)
        view.set_content(scrolled)
        return view

    def _start_diagnose(self) -> None:
        """Collect diagnostics off the main loop with fixed safe flags."""
        try:
            from box_gui.gtk.workers import run_diagnose
        except ImportError as exc:
            self._on_diagnose_error(exc)
            return
        run_diagnose(
            self._paths,
            self._repository,
            self._game_path,
            self._on_diagnose_done,
            self._on_diagnose_error,
        )

    def _add_optional_row(self, title: str, value: str | None) -> None:
        """Add a version row unless the value is missing."""
        if value is None:
            return
        row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(value, -1))
        self._versions_group.add(row)
        self._version_rows.append(row)

    def _on_diagnose_done(self, result: DiagnoseResult) -> None:
        """Render diagnostics on the GTK main loop."""
        self._spinner.stop()
        self._status.set_text("")
        self._status_box.set_visible(False)
        self._system_row.set_subtitle(GLib.markup_escape_text(result.environment.system, -1))
        self._release_row.set_subtitle(GLib.markup_escape_text(result.environment.release, -1))
        self._machine_row.set_subtitle(GLib.markup_escape_text(result.environment.machine, -1))
        self._engine_row.set_subtitle(GLib.markup_escape_text(result.versions.engine, -1))
        self._add_optional_row(_("Engine Version"), result.versions.engine_version)
        self._add_optional_row(_("NW.js"), result.versions.nwjs)
        self._add_optional_row(_("EasyRPG Player"), result.versions.easyrpg_player)

    def _on_diagnose_error(self, error: BaseException) -> None:
        """Show diagnose failures with an Adw.AlertDialog."""
        self._spinner.stop()
        self._status.set_text(_("Diagnose failed."))
        message = str(error) or error.__class__.__name__
        dialog = Adw.AlertDialog(heading=diagnose_error_heading(error), body=message)
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present(self)
