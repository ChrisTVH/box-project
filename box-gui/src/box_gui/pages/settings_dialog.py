"""Settings dialog with General, runtime, and cleanup pages."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import box.api.runtime as runtime_api  # noqa: E402
from box.api import AppPaths, ConfigRepository  # noqa: E402
from box.api.cleanup import CATEGORIES, CleanupCatalog, CleanupItem  # noqa: E402
from box.api.interaction import Interaction  # noqa: E402
from box.errors import BoxError  # noqa: E402
from box.games.identity import game_id  # noqa: E402
from box.models import RuntimeInfo  # noqa: E402
from box.runtime.catalog import RuntimeCatalog  # noqa: E402
from box.runtime.easyrpg import EasyRPGCatalog, EasyRPGRuntime  # noqa: E402
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from box_gui.core.defaults import DefaultsRepository  # noqa: E402
from box_gui.core.display import abbreviate_display_path  # noqa: E402
from box_gui.core.library import LibraryError, LibraryRepository  # noqa: E402
from box_gui.gtk.workers import ProgressReporter  # noqa: E402
from box_gui.i18n import _, ngettext  # noqa: E402

__all__ = [
    "CleanupPage",
    "EasyrpgPage",
    "GeneralPage",
    "NwjsPage",
    "SettingsDialog",
]

_NWJS_ARCHITECTURES: tuple[str, ...] = ("x64", "ia32", "arm64", "arm")


def _unlisted_data_row(
    primary: str,
    secondary: str | None = None,
    wrap_lines: int | None = None,
    selectable: bool = False,
) -> tuple[Adw.ActionRow, Gtk.Label]:
    """Build a data row excluded from PreferencesDialog search.

    libadwaita indexes only titled PreferencesRows for dialog search, so
    data rows (runtime lists, cleanup items, status lines) keep an empty
    row title and render their text in plain child labels instead. Plain
    Gtk.Label text needs no markup escaping. Option rows keep real titles
    so search keeps finding actual settings; only the returned primary
    label needs keeping when the text is updated later.
    """
    row = Adw.ActionRow(title="")
    lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    lines.set_hexpand(True)
    lines.set_valign(Gtk.Align.CENTER)
    primary_label = Gtk.Label(label=primary)
    primary_label.set_xalign(0.0)
    primary_label.set_wrap(True)
    primary_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    primary_label.set_selectable(selectable)
    if wrap_lines is not None:
        primary_label.set_lines(wrap_lines)
    lines.append(primary_label)
    if secondary is not None:
        secondary_label = Gtk.Label(label=secondary)
        secondary_label.set_xalign(0.0)
        secondary_label.set_wrap(True)
        secondary_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        secondary_label.add_css_class("dim")
        secondary_label.add_css_class("caption")
        lines.append(secondary_label)
    row.add_prefix(lines)
    return row, primary_label


def _show_error(parent: Gtk.Widget, operation: str, error: BaseException) -> None:
    """Show a single-close error dialog, splitting BoxError from unexpected bugs."""
    if isinstance(error, BoxError):
        heading = _("{operation} Failed").format(operation=operation)
    else:
        heading = _("Unexpected Error")
    message = str(error) or error.__class__.__name__
    dialog = Adw.AlertDialog(heading=heading, body=message)
    dialog.add_response("close", _("Close"))
    dialog.set_default_response("close")
    dialog.set_close_response("close")
    dialog.present(parent)


def _run_in_thread[T](
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


_ProgressReporter = ProgressReporter


def _is_dismissed(error: GLib.Error) -> bool:
    """Return True when a file-chooser error is a plain user dismissal."""
    try:
        return bool(error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED))
    except Exception:
        return False


class GeneralPage(Adw.PreferencesPage):
    """New settings UI for allowed game roots and the preferred runtimes."""

    def __init__(
        self,
        paths: AppPaths,
        repository: ConfigRepository,
        interaction: Interaction | None = None,
    ) -> None:
        """Build the roots list and the preferred-runtime pickers."""
        super().__init__(name="general", title=_("General"))
        self.set_icon_name("box-rpg-settings-symbolic")
        self._paths = paths
        self._repository = repository
        self._interaction = interaction
        self._catalog = RuntimeCatalog(paths)
        self._easyrpg_catalog = EasyRPGCatalog(paths)
        self._defaults = DefaultsRepository(paths)
        self._loading_nwjs_runtime = False
        self._loading_easyrpg_runtime = False
        self._folder_dialog: Gtk.FileDialog | None = None
        self._root_rows: list[Adw.ActionRow] = []
        self._roots_group = Adw.PreferencesGroup(title=_("Allowed game roots"))
        # One summary row for dialog search; the per-root rows live inside
        # the expander, so the search index never sees individual paths.
        self._roots_expander = Adw.ExpanderRow(title=_("No game roots configured."))
        self._roots_expander.set_expanded(False)
        self._roots_group.add(self._roots_expander)
        self._add_root_button = Gtk.Button(label=_("Add root"))
        self._add_root_button.set_valign(Gtk.Align.CENTER)
        self._add_root_button.connect("clicked", self._on_add_root_clicked)
        self._roots_group.set_header_suffix(self._add_root_button)
        self._nwjs_runtime_group = Adw.PreferencesGroup(title=_("Preferred NW.js runtime"))
        self._nwjs_runtime_row = Adw.ComboRow(title=_("NW.js version"))
        self._nwjs_runtime_row.set_model(Gtk.StringList.new([_("Undefined")]))
        self._nwjs_runtime_row.connect("notify::selected", self._on_nwjs_runtime_selected)
        self._nwjs_runtime_group.add(self._nwjs_runtime_row)
        self._easyrpg_runtime_group = Adw.PreferencesGroup(title=_("Preferred EasyRPG runtime"))
        self._easyrpg_runtime_row = Adw.ComboRow(title=_("EasyRPG version"))
        self._easyrpg_runtime_row.set_model(Gtk.StringList.new([_("Undefined")]))
        self._easyrpg_runtime_row.connect("notify::selected", self._on_easyrpg_runtime_selected)
        self._easyrpg_runtime_group.add(self._easyrpg_runtime_row)
        self.add(self._roots_group)
        self.add(self._nwjs_runtime_group)
        self.add(self._easyrpg_runtime_group)
        self.refresh_roots()
        self.refresh_runtime()

    def refresh_roots(self) -> None:
        """Prune missing roots, then render the surviving configured roots."""
        try:
            config = self._repository.prune_missing_allowed_roots()
        except Exception as exc:
            _show_error(self, _("Load Roots"), exc)
            return
        self._render_roots(config.allowed_game_roots)

    def refresh_runtime(self) -> None:
        """Rebuild both preferred-runtime pickers from installed versions."""
        self.refresh_nwjs_runtime()
        self.refresh_easyrpg_runtime()

    def refresh_nwjs_runtime(self) -> None:
        """Rebuild the preferred NW.js picker from installed NW.js versions."""
        try:
            installed = runtime_api.list_nwjs(self._catalog)
        except Exception as exc:
            _show_error(self, _("Load Runtimes"), exc)
            return
        try:
            preferred = self._repository.load().preferred_runtime
        except Exception as exc:
            _show_error(self, _("Load Settings"), exc)
            return
        versions: list[str] = []
        for runtime in installed:
            if runtime.spec.version not in versions:
                versions.append(runtime.spec.version)
        entries = [_("Undefined"), *versions]
        if preferred is not None and preferred not in versions:
            entries.append(preferred)
        selected = 0 if preferred is None else entries.index(preferred)
        self._loading_nwjs_runtime = True
        try:
            self._nwjs_runtime_row.set_model(Gtk.StringList.new(entries))
            self._nwjs_runtime_row.set_selected(selected)
        finally:
            self._loading_nwjs_runtime = False

    def refresh_easyrpg_runtime(self) -> None:
        """Rebuild the preferred EasyRPG picker from installed versions."""
        try:
            installed = runtime_api.list_easyrpg(self._easyrpg_catalog)
        except Exception as exc:
            _show_error(self, _("Load Runtimes"), exc)
            return
        try:
            preferred = self._defaults.load().preferred_easyrpg_runtime
        except Exception as exc:
            _show_error(self, _("Load Settings"), exc)
            return
        versions: list[str] = []
        for runtime in installed:
            if runtime.version not in versions:
                versions.append(runtime.version)
        entries = [_("Undefined"), *versions]
        if preferred is not None and preferred not in versions:
            entries.append(preferred)
        selected = 0 if preferred is None else entries.index(preferred)
        self._loading_easyrpg_runtime = True
        try:
            self._easyrpg_runtime_row.set_model(Gtk.StringList.new(entries))
            self._easyrpg_runtime_row.set_selected(selected)
        finally:
            self._loading_easyrpg_runtime = False

    def add_root(self, root: Path) -> bool:
        """Confirm the root via the interaction, store it, and refresh the list."""
        if self._interaction is not None:
            try:
                confirmed = self._interaction.confirm_add_root(root)
            except Exception as exc:
                _show_error(self, _("Add Root"), exc)
                return False
            if not confirmed:
                return False
        try:
            config = self._repository.add_allowed_root(root)
        except Exception as exc:
            _show_error(self, _("Add Root"), exc)
            return False
        self._render_roots(config.allowed_game_roots)
        return True

    def _render_roots(self, roots: tuple[Path, ...]) -> None:
        """Replace the expander contents with one removable row per root."""
        for row in self._root_rows:
            self._roots_expander.remove(row)
        self._root_rows.clear()
        for root in roots:
            # ActionRow renders titles as Pango markup, so escape paths like
            # ".../Fear & Hunger" that would otherwise break parsing. Only
            # the display is abbreviated; the tooltip keeps the full root.
            # Nested rows are invisible to PreferencesDialog search; only
            # the expander summary title is indexed.
            short = abbreviate_display_path(root)
            row = Adw.ActionRow(title=GLib.markup_escape_text(short, -1))
            row.set_title_selectable(True)
            row.set_tooltip_text(str(root))
            button = Gtk.Button(label=_("Remove"))
            button.set_valign(Gtk.Align.CENTER)
            button.connect("clicked", self._make_root_remove_handler(root))
            row.add_suffix(button)
            self._roots_expander.add_row(row)
            self._root_rows.append(row)
        if roots:
            self._roots_expander.set_title(
                ngettext(
                    "{count} root configured.",
                    "{count} roots configured.",
                    len(roots),
                ).format(count=len(roots))
            )
        else:
            self._roots_expander.set_title(_("No game roots configured."))

    def _make_root_remove_handler(self, root: Path) -> Callable[[Gtk.Button], None]:
        """Build a per-row remove callback capturing its configured root."""

        def _handler(_button: Gtk.Button) -> None:
            try:
                config = self._repository.remove_allowed_root(root)
            except Exception as exc:
                _show_error(self, _("Remove Root"), exc)
                return
            self._render_roots(config.allowed_game_roots)

        return _handler

    def _on_nwjs_runtime_selected(self, row: Adw.ComboRow, _pspec: object) -> None:
        """Persist the picked NW.js runtime, mapping the first entry to None."""
        if self._loading_nwjs_runtime:
            return
        model = row.get_model()
        if not isinstance(model, Gtk.StringList):
            return
        selected = int(row.get_selected())
        count = model.get_n_items()
        if selected < 0 or selected >= count:
            return
        value: str | None = None if selected == 0 else model.get_string(selected)
        try:
            self._repository.set_preferred_runtime(value)
        except Exception as exc:
            _show_error(self, _("Set Runtime"), exc)
            self.refresh_nwjs_runtime()

    def _on_easyrpg_runtime_selected(self, row: Adw.ComboRow, _pspec: object) -> None:
        """Persist the picked EasyRPG runtime, mapping the first entry to None."""
        if self._loading_easyrpg_runtime:
            return
        model = row.get_model()
        if not isinstance(model, Gtk.StringList):
            return
        selected = int(row.get_selected())
        count = model.get_n_items()
        if selected < 0 or selected >= count:
            return
        value: str | None = None if selected == 0 else model.get_string(selected)
        try:
            self._defaults.set_preferred_easyrpg_runtime(value)
        except Exception as exc:
            _show_error(self, _("Set Runtime"), exc)
            self.refresh_easyrpg_runtime()

    def _on_add_root_clicked(self, _button: Gtk.Button) -> None:
        """Open an async folder picker for a new allowed game root."""
        dialog = Gtk.FileDialog(title=_("Add game root"))
        self._folder_dialog = dialog
        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None
        dialog.select_folder(parent, None, self._on_folder_chosen)

    def _on_folder_chosen(self, source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        """Store the folder chosen in the async picker callback."""
        self._folder_dialog = None
        try:
            folder = source.select_folder_finish(result)
        except GLib.Error as exc:
            if _is_dismissed(exc):
                return
            _show_error(self, _("Select Folder"), exc)
            return
        path = folder.get_path()
        if path is None:
            _show_error(
                self, _("Select Folder"), ValueError(_("Cannot resolve the selected folder."))
            )
            return
        self.add_root(Path(path))


class _RuntimePage(Adw.PreferencesPage):
    """Shared installed list, version browser, pager, and status for one engine."""

    _ALLOWS_OPTIONS: bool = False

    def __init__(self, paths: AppPaths, *, name: str, title: str) -> None:
        """Build the installed/browser/pager/status groups without loading yet."""
        super().__init__(name=name, title=title)
        self._paths = paths
        self._page = 1
        self._versions: tuple[str, ...] = ()
        self._busy = False
        self._installed_rows: list[Adw.ActionRow] = []
        self._browser_rows: list[Adw.ActionRow] = []
        self._installed_group = Adw.PreferencesGroup(title=self._installed_title())
        self._browser_group = Adw.PreferencesGroup(title=self._browser_title())
        self._empty_row, _label = _unlisted_data_row(_("No versions available."))
        self._empty_row.set_sensitive(False)
        self._browser_group.add(self._empty_row)
        self._prev_button = Gtk.Button(label=_("Previous"))
        self._next_button = Gtk.Button(label=_("Next"))
        self._prev_button.set_valign(Gtk.Align.CENTER)
        self._next_button.set_valign(Gtk.Align.CENTER)
        self._pager_row, self._pager_label = _unlisted_data_row(_("Page {page}").format(page=1))
        self._pager_row.add_suffix(self._prev_button)
        self._pager_row.add_suffix(self._next_button)
        self._pager_group = Adw.PreferencesGroup()
        self._pager_group.add(self._pager_row)
        self._status_row, self._status_label = _unlisted_data_row(_("Loading runtimes."))
        self._spinner = Gtk.Spinner()
        self._spinner.set_valign(Gtk.Align.CENTER)
        self._status_row.add_suffix(self._spinner)
        self._status_group = Adw.PreferencesGroup()
        self._status_group.add(self._status_row)
        self._installing: tuple[Gtk.Button, Gtk.Widget | None] | None = None
        self.add(self._installed_group)
        if self._ALLOWS_OPTIONS:
            self.add(self._build_options_group())
        self.add(self._browser_group)
        self.add(self._pager_group)
        self.add(self._status_group)
        self._prev_button.connect("clicked", self._on_prev_clicked)
        self._next_button.connect("clicked", self._on_next_clicked)

    def _installed_title(self) -> str:
        """Return the installed-group title for this engine."""
        raise NotImplementedError

    def _browser_title(self) -> str:
        """Return the available-versions group title for this engine."""
        raise NotImplementedError

    def _engine_noun(self) -> str:
        """Return the short engine name used inside status messages."""
        raise NotImplementedError

    def _build_options_group(self) -> Adw.PreferencesGroup:
        """Build the engine-specific options group (NW.js only)."""
        raise NotImplementedError

    def _list_installed(self) -> tuple[RuntimeInfo | EasyRPGRuntime, ...]:
        """Return installed runtimes for this engine."""
        raise NotImplementedError

    def _fetch_available(self, page: int) -> Any:
        """Fetch one page of installable versions for this engine."""
        raise NotImplementedError

    def _make_installed_row(self, runtime: RuntimeInfo | EasyRPGRuntime) -> Adw.ActionRow:
        """Build one installed row with its remove button for this engine."""
        raise NotImplementedError

    def request_install(self, version: str, button: Gtk.Button | None = None) -> None:
        """Confirm an install with a separate alert dialog."""
        raise NotImplementedError

    def _set_options_sensitive(self, sensitive: bool) -> None:
        """Toggle engine-specific option widgets (NW.js overrides this)."""

    def refresh_installed(self) -> None:
        """Reload installed runtimes on a worker thread."""
        self._spinner.start()
        self._set_status(_("Loading installed {noun} runtimes …").format(noun=self._engine_noun()))
        _run_in_thread(
            self._list_installed,
            self._on_installed_done,
            self._on_installed_error,
        )

    def load_page(self, page: int) -> None:
        """Fetch one page of versions on a worker thread, clamped at page one."""
        clamped = max(1, page)
        self._page = clamped
        self._spinner.start()
        _run_in_thread(
            lambda: self._fetch_available(clamped),
            self._on_page_done,
            self._on_page_error,
        )

    def _on_installed_done(self, runtimes: tuple[RuntimeInfo | EasyRPGRuntime, ...]) -> None:
        """Render installed runtimes on the main loop."""
        self._spinner.stop()
        self._clear_rows(self._installed_group, self._installed_rows)
        for runtime in runtimes:
            row = self._make_installed_row(runtime)
            self._installed_group.add(row)
            self._installed_rows.append(row)
        if runtimes:
            self._status_group.set_visible(False)
        else:
            self._set_status(_("No {noun} runtimes installed.").format(noun=self._engine_noun()))

    def _on_installed_error(self, error: BaseException) -> None:
        """Show installed-list failures with an alert dialog."""
        self._spinner.stop()
        self._set_status(_("Loading installed runtimes failed."))
        self._show_error(_("Load Runtimes"), error)

    def _on_page_done(self, available: Any) -> None:
        """Render one page of versions on the main loop."""
        self._spinner.stop()
        self._page = available.page
        self._versions = available.versions
        self._clear_rows(self._browser_group, self._browser_rows)
        for version in available.versions:
            row, _label = _unlisted_data_row(version)
            button = Gtk.Button(label=_("Install"))
            button.set_valign(Gtk.Align.CENTER)
            button.connect("clicked", self._make_install_handler(version))
            row.add_suffix(button)
            self._browser_group.add(row)
            self._browser_rows.append(row)
        self._empty_row.set_visible(not available.versions)
        self._pager_label.set_text(_("Page {page}").format(page=available.page))
        self._prev_button.set_sensitive(available.page > 1 and not self._busy)
        if not available.versions:
            self._set_status(_("No versions available."))

    def _on_page_error(self, error: BaseException) -> None:
        """Show version-list failures with an alert dialog."""
        self._spinner.stop()
        self._set_status(_("Loading versions failed."))
        self._show_error(_("Load Versions"), error)

    def _make_install_handler(self, version: str) -> Callable[[Gtk.Button], None]:
        """Build a per-version install callback opening the confirm dialog."""

        def _handler(button: Gtk.Button) -> None:
            self.request_install(version, button)

        return _handler

    def _begin_remove(self, button: Gtk.Button, status: str, task: Callable[[], None]) -> None:
        """Run a no-confirm removal on a worker thread, then refresh the list."""
        if self._busy:
            return
        button.set_sensitive(False)
        self._set_busy(True)
        self._set_status(status)
        _run_in_thread(task, self._on_remove_done, self._on_remove_error)

    def _on_remove_done(self, _result: None) -> None:
        """Refresh the installed list after a removal."""
        self._set_busy(False)
        self.refresh_installed()

    def _on_remove_error(self, error: BaseException) -> None:
        """Show removal failures with an alert dialog, then refresh."""
        self._set_busy(False)
        self._set_status(_("Removal failed."))
        self._show_error(_("Remove Runtime"), error)
        self.refresh_installed()

    def _begin_install(
        self,
        status: str,
        task: Callable[[_ProgressReporter], object],
        button: Gtk.Button | None = None,
    ) -> None:
        """Install one runtime on a worker thread, spinning inside its button."""
        self._set_busy(True)
        if button is not None:
            spinner = Gtk.Spinner()
            spinner.start()
            self._installing = (button, button.get_child())
            button.set_child(spinner)
            button.set_sensitive(False)
        reporter = _ProgressReporter(self._on_progress)

        def _task() -> object:
            return task(reporter)

        _run_in_thread(_task, self._on_install_done, self._on_install_error)

    def _on_progress(self, completed: int, total: int | None) -> None:
        """Swallow progress reports; the installing button shows a spinner.

        A real reporter must still reach install_* so the backend never
        falls back to terminal progress output.
        """

    def _restore_install_button(self) -> None:
        """Swap the spinner back out of the installing button, if any."""
        installing = self._installing
        self._installing = None
        if installing is None:
            return
        button, old_child = installing
        if old_child is not None:
            button.set_child(old_child)
        button.set_sensitive(True)

    def _on_install_done(self, _runtime: object) -> None:
        """Refresh the installed list after an install."""
        self._restore_install_button()
        self._set_busy(False)
        self._set_status(_("Install complete."))
        self.refresh_installed()

    def _on_install_error(self, error: BaseException) -> None:
        """Show install failures with an alert dialog."""
        self._restore_install_button()
        self._set_busy(False)
        self._set_status(_("Install failed."))
        self._show_error(_("Install Runtime"), error)

    def _set_busy(self, busy: bool) -> None:
        """Disable page controls while an install or removal runs."""
        self._busy = busy
        sensitive = not busy
        self._set_options_sensitive(sensitive)
        self._prev_button.set_sensitive(sensitive and self._page > 1)
        self._next_button.set_sensitive(sensitive)
        self._browser_group.set_sensitive(sensitive)
        self._installed_group.set_sensitive(sensitive)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()

    def _set_status(self, text: str) -> None:
        """Replace the shared status label text (kept out of dialog search)."""
        self._status_group.set_visible(True)
        self._status_label.set_text(text)

    def _show_error(self, operation: str, error: BaseException) -> None:
        """Show a single-close error dialog splitting BoxError from unexpected bugs."""
        _show_error(self, operation, error)

    @staticmethod
    def _clear_rows(group: Adw.PreferencesGroup, rows: list[Adw.ActionRow]) -> None:
        """Remove tracked rows from a preferences group."""
        for row in rows:
            group.remove(row)
        rows.clear()

    def _on_prev_clicked(self, _button: Gtk.Button) -> None:
        """Go to the previous page, clamped at page one."""
        self.load_page(self._page - 1)

    def _on_next_clicked(self, _button: Gtk.Button) -> None:
        """Go to the next available page."""
        self.load_page(self._page + 1)


class NwjsPage(_RuntimePage):
    """NW.js runtime manager re-hosted as a preferences page."""

    _ALLOWS_OPTIONS = True

    def __init__(self, paths: AppPaths) -> None:
        """Build the page, default the architecture, and load the first lists."""
        self._catalog = RuntimeCatalog(paths)
        self._arch = "x64"
        self._sdk = False
        super().__init__(paths, name="nwjs", title=_("NW.js"))
        self.set_icon_name("box-rpg-nwjs-symbolic")
        self._resolve_default_architecture()
        self.refresh_installed()
        self.load_page(1)

    def _installed_title(self) -> str:
        """Return the installed NW.js group title."""
        return _("Installed NW.js runtimes")

    def _browser_title(self) -> str:
        """Return the available NW.js versions group title."""
        return _("Available NW.js versions")

    def _engine_noun(self) -> str:
        """Return the short engine name used inside status messages."""
        return "NW.js"

    def _build_options_group(self) -> Adw.PreferencesGroup:
        """Build the architecture selector and SDK toggle rows."""
        self._arch_selector = Gtk.DropDown.new_from_strings(list(_NWJS_ARCHITECTURES))
        self._arch_selector.set_selected(_NWJS_ARCHITECTURES.index(self._arch))
        self._arch_selector.set_valign(Gtk.Align.CENTER)
        self._arch_selector.connect("notify::selected", self._on_arch_changed)
        self._arch_row = Adw.ActionRow(title=_("Architecture"))
        self._arch_row.add_suffix(self._arch_selector)
        self._sdk_toggle = Gtk.CheckButton()
        self._sdk_toggle.set_active(self._sdk)
        self._sdk_toggle.set_valign(Gtk.Align.CENTER)
        self._sdk_toggle.connect("toggled", self._on_sdk_toggled)
        self._sdk_row = Adw.ActionRow(title=_("SDK"))
        self._sdk_row.add_suffix(self._sdk_toggle)
        group = Adw.PreferencesGroup()
        group.add(self._arch_row)
        group.add(self._sdk_row)
        return group

    def _set_options_sensitive(self, sensitive: bool) -> None:
        """Toggle the architecture selector and SDK toggle."""
        self._arch_selector.set_sensitive(sensitive)
        self._sdk_toggle.set_sensitive(sensitive)

    def _list_installed(self) -> tuple[RuntimeInfo, ...]:
        """Return valid launcher-owned NW.js runtimes."""
        return runtime_api.list_nwjs(self._catalog)

    def _fetch_available(self, page: int) -> Any:
        """Fetch one page of installable NW.js versions for the picked options."""
        arch, sdk = self._arch, self._sdk
        return runtime_api.fetch_nwjs_available(page, arch, sdk)

    def _make_installed_row(self, runtime: RuntimeInfo | EasyRPGRuntime) -> Adw.ActionRow:
        """Build one installed NW.js row with its remove button."""
        assert isinstance(runtime, RuntimeInfo)
        flavor = "SDK" if runtime.spec.sdk else _("standard")
        subtitle = _("NW.js {version} for {architecture} ({flavor})").format(
            version=runtime.spec.version,
            architecture=runtime.spec.architecture,
            flavor=flavor,
        )
        row, _label = _unlisted_data_row(runtime.spec.version, subtitle)
        button = Gtk.Button(label=_("Remove"))
        button.set_valign(Gtk.Align.CENTER)
        button.connect(
            "clicked",
            self._make_remove_handler(
                runtime.spec.version, runtime.spec.architecture, runtime.spec.sdk
            ),
        )
        row.add_suffix(button)
        return row

    def _make_remove_handler(
        self, version: str, architecture: str, sdk: bool
    ) -> Callable[[Gtk.Button], None]:
        """Build a per-row remove callback capturing the runtime spec."""

        def _handler(button: Gtk.Button) -> None:
            catalog = self._catalog

            def _task() -> None:
                runtime_api.remove_nwjs(catalog, version, architecture, sdk)

            self._begin_remove(
                button,
                _("Removing NW.js {version} …").format(version=version),
                _task,
            )

        return _handler

    def _on_arch_changed(self, selector: Gtk.DropDown, _pspec: object) -> None:
        """Reload page one when the architecture selector changes."""
        if self._busy:
            return
        selected = int(selector.get_selected())
        if 0 <= selected < len(_NWJS_ARCHITECTURES):
            arch = _NWJS_ARCHITECTURES[selected]
            if arch == self._arch:
                return
            self._arch = arch
            self.load_page(1)

    def _on_sdk_toggled(self, toggle: Gtk.CheckButton) -> None:
        """Reload page one when the SDK toggle changes."""
        if self._busy:
            return
        active = bool(toggle.get_active())
        if active == self._sdk:
            return
        self._sdk = active
        self.load_page(1)

    def request_install(self, version: str, button: Gtk.Button | None = None) -> None:
        """Confirm an NW.js install with a separate alert dialog."""
        arch, sdk = self._arch, self._sdk
        dialog = Adw.AlertDialog(
            heading=_("Install Runtime"),
            body=_("Install NW.js {version} for {architecture}?").format(
                version=version, architecture=arch
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("install", _("Install"))
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._make_confirm_handler(version, arch, sdk, button))
        dialog.present(self)

    def _make_confirm_handler(
        self, version: str, architecture: str, sdk: bool, button: Gtk.Button | None = None
    ) -> Callable[[Adw.AlertDialog, str], None]:
        """Build the confirm-response callback capturing the picked spec."""

        def _handler(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "install":
                self._install(version, architecture, sdk, button)

        return _handler

    def _install(
        self, version: str, architecture: str, sdk: bool, button: Gtk.Button | None = None
    ) -> None:
        """Install an NW.js runtime with progress on a worker thread."""
        paths = self._paths

        def _task(reporter: _ProgressReporter) -> RuntimeInfo:
            return runtime_api.install_nwjs(paths, version, architecture, sdk, progress=reporter)

        self._begin_install(
            _("Installing NW.js {version} …").format(version=version), _task, button
        )

    def _resolve_default_architecture(self) -> None:
        """Default the NW.js selector to the host architecture."""
        resolve = getattr(runtime_api, "default_architecture", None)
        if resolve is None:
            return
        try:
            arch = resolve()
        except Exception as exc:
            self._show_error(_("Detect Architecture"), exc)
            return
        if arch in _NWJS_ARCHITECTURES:
            self._arch = arch
            self._arch_selector.set_selected(_NWJS_ARCHITECTURES.index(arch))


class EasyrpgPage(_RuntimePage):
    """EasyRPG Player runtime manager re-hosted as a preferences page (x64 only)."""

    def __init__(self, paths: AppPaths) -> None:
        """Build the page and load the first installed/available lists."""
        super().__init__(paths, name="easyrpg", title=_("EasyRPG"))
        self.set_icon_name("box-rpg-easyrpg-symbolic")
        self._catalog = EasyRPGCatalog(paths)
        self.refresh_installed()
        self.load_page(1)

    def _installed_title(self) -> str:
        """Return the installed EasyRPG group title."""
        return _("Installed EasyRPG runtimes")

    def _browser_title(self) -> str:
        """Return the available EasyRPG versions group title."""
        return _("Available EasyRPG versions")

    def _engine_noun(self) -> str:
        """Return the short engine name used inside status messages."""
        return "EasyRPG Player"

    def _list_installed(self) -> tuple[EasyRPGRuntime, ...]:
        """Return valid launcher-owned EasyRPG Player runtimes."""
        return runtime_api.list_easyrpg(self._catalog)

    def _fetch_available(self, page: int) -> Any:
        """Fetch one page of installable EasyRPG Player versions."""
        return runtime_api.fetch_easyrpg_available(page)

    def _make_installed_row(self, runtime: RuntimeInfo | EasyRPGRuntime) -> Adw.ActionRow:
        """Build one installed EasyRPG row with its remove button."""
        assert isinstance(runtime, EasyRPGRuntime)
        subtitle = _("EasyRPG Player {version} for x64").format(version=runtime.version)
        row, _label = _unlisted_data_row(runtime.version, subtitle)
        button = Gtk.Button(label=_("Remove"))
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", self._make_remove_handler(runtime.version))
        row.add_suffix(button)
        return row

    def _make_remove_handler(self, version: str) -> Callable[[Gtk.Button], None]:
        """Build a per-row EasyRPG remove callback capturing its version."""

        def _handler(button: Gtk.Button) -> None:
            catalog = self._catalog

            def _task() -> None:
                runtime_api.remove_easyrpg(catalog, version)

            self._begin_remove(
                button,
                _("Removing EasyRPG Player {version} …").format(version=version),
                _task,
            )

        return _handler

    def request_install(self, version: str, button: Gtk.Button | None = None) -> None:
        """Confirm an EasyRPG install with a separate alert dialog."""
        dialog = Adw.AlertDialog(
            heading=_("Install Runtime"),
            body=_("Install EasyRPG Player {version} for x64?").format(version=version),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("install", _("Install"))
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._make_confirm_handler(version, button))
        dialog.present(self)

    def _make_confirm_handler(
        self, version: str, button: Gtk.Button | None = None
    ) -> Callable[[Adw.AlertDialog, str], None]:
        """Build the EasyRPG confirm-response callback capturing its version."""

        def _handler(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "install":
                self._install(version, button)

        return _handler

    def _install(self, version: str, button: Gtk.Button | None = None) -> None:
        """Install an EasyRPG runtime with progress on a worker thread."""
        paths = self._paths

        def _task(reporter: _ProgressReporter) -> EasyRPGRuntime:
            return runtime_api.install_easyrpg(paths, version, progress=reporter)

        self._begin_install(
            _("Installing EasyRPG Player {version} …").format(version=version), _task, button
        )


# Game roots are managed in GeneralPage, so the Data page skips them.
_CLEANUP_CATEGORIES: tuple[str, ...] = tuple(c for c in CATEGORIES if c != "roots")


class CleanupPage(Adw.PreferencesPage):
    """Launcher-managed cleanup items re-hosted as a preferences page."""

    def __init__(
        self,
        paths: AppPaths,
        repository: ConfigRepository,
        library: LibraryRepository | None = None,
    ) -> None:
        """Build one summarized group per shown category and load each one."""
        super().__init__(name="datos", title=_("Data"))
        self.set_icon_name("box-rpg-trash-symbolic")
        self._paths = paths
        self._repository = repository
        self._catalog = CleanupCatalog(paths, repository)
        self._library = library
        self._busy = False
        self._groups: dict[str, Adw.PreferencesGroup] = {}
        self._expanders: dict[str, Adw.ExpanderRow] = {}
        self._rows: dict[str, list[Adw.ActionRow]] = {}
        self._empty_rows: dict[str, Adw.ActionRow] = {}
        for category in _CLEANUP_CATEGORIES:
            group = Adw.PreferencesGroup(
                title=_category_title(category),
                description=_category_description(category),
            )
            # One summary row per category for dialog search; the per-item
            # rows nest inside the expander, so the results list stays short.
            expander = Adw.ExpanderRow(title=_("Loading cleanup items …"))
            expander.set_expanded(False)
            empty, _label = _unlisted_data_row(_("No items."))
            empty.set_sensitive(False)
            expander.add_row(empty)
            group.add(expander)
            self._groups[category] = group
            self._expanders[category] = expander
            self._rows[category] = []
            self._empty_rows[category] = empty
            self.add(group)
        self.refresh_all()

    def refresh_all(self) -> None:
        """Reload every shown category on worker threads."""
        for category in _CLEANUP_CATEGORIES:
            self.refresh_category(category)

    def refresh_category(self, category: str) -> None:
        """Reload one category on a worker thread."""
        self._expanders[category].set_title(_("Loading cleanup items …"))
        catalog = self._catalog

        def _task() -> tuple[CleanupItem, ...]:
            return catalog.list(category)

        _run_in_thread(
            _task,
            self._make_list_done_handler(category),
            self._make_list_error_handler(category),
        )

    def request_remove(self, item: CleanupItem) -> None:
        """Confirm a single-item removal with a separate alert dialog."""
        if self._busy:
            return
        dialog = Adw.AlertDialog(
            heading=_("Remove Item"),
            body=_("Remove {label}?").format(label=item.label),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._make_remove_confirm_handler(item))
        dialog.present(self)

    def _make_list_done_handler(self, category: str) -> Callable[[tuple[CleanupItem, ...]], None]:
        """Build the list callback capturing its category."""

        def _handler(items: tuple[CleanupItem, ...]) -> None:
            self._on_list_done(category, items)

        return _handler

    def _make_list_error_handler(self, category: str) -> Callable[[BaseException], None]:
        """Build the per-category list-error callback."""
        page = self

        def _handler(error: BaseException) -> None:
            page._expanders[category].set_title(_("Loading cleanup items failed."))
            page._show_error(_("Load Items"), error)

        return _handler

    def _profile_game_names(self) -> dict[str, str]:
        """Map profile IDs to library display names, skipping unknown roots."""
        library = self._library
        if library is None:
            return {}
        try:
            entries = library.load()
        except LibraryError, OSError:
            return {}
        names: dict[str, str] = {}
        for entry in entries:
            try:
                names[game_id(entry.path)] = entry.display_name
            except OSError:
                continue
        return names

    def _on_list_done(self, category: str, items: tuple[CleanupItem, ...]) -> None:
        """Render one category on the main loop."""
        expander = self._expanders[category]
        rows = self._rows[category]
        self._clear_rows(expander, rows)
        game_names = self._profile_game_names() if category == "profiles" else {}
        for item in items:
            # Path-valued items (game roots) reuse the General page display:
            # abbreviated path up front, full path in the tooltip. Other
            # items keep their label. Plain child labels need no markup
            # escaping, and the empty row title keeps these data rows out
            # of PreferencesDialog search.
            if category == "profiles":
                shown = game_names.get(item.label, _("Unknown game"))
                row, _label = _unlisted_data_row(shown, item.label, wrap_lines=2)
            elif isinstance(item.value, Path) and item.label == str(item.value):
                shown = abbreviate_display_path(item.value)
                row, _label = _unlisted_data_row(shown, wrap_lines=2, selectable=True)
            else:
                shown = item.label
                row, _label = _unlisted_data_row(shown, wrap_lines=2, selectable=True)
            row.set_tooltip_text(item.label)
            button = Gtk.Button(label=_("Remove"))
            button.set_valign(Gtk.Align.CENTER)
            button.connect("clicked", self._make_remove_handler(item))
            row.add_suffix(button)
            expander.add_row(row)
            rows.append(row)
        self._empty_rows[category].set_visible(not items)
        if items:
            expander.set_title(
                ngettext("{count} item", "{count} items", len(items)).format(count=len(items))
            )
        else:
            expander.set_title(_("No items."))

    def _make_remove_handler(self, item: CleanupItem) -> Callable[[Gtk.Button], None]:
        """Build a per-row remove callback opening the confirm dialog."""

        def _handler(_button: Gtk.Button) -> None:
            if self._busy:
                return
            self.request_remove(item)

        return _handler

    def _make_remove_confirm_handler(
        self, item: CleanupItem
    ) -> Callable[[Adw.AlertDialog, str], None]:
        """Build the confirm-response callback capturing the picked item."""

        def _handler(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "remove":
                self._remove_item(item)

        return _handler

    def _remove_item(self, item: CleanupItem) -> None:
        """Remove one item on a worker thread, then refresh its category."""
        if self._busy:
            return
        self._set_busy(True)
        self._expanders[item.category].set_title(_("Removing {label} …").format(label=item.label))
        catalog = self._catalog

        def _task() -> None:
            catalog.remove(item)

        _run_in_thread(
            _task,
            self._make_remove_done_handler(item),
            self._make_remove_error_handler(item),
        )

    def _make_remove_done_handler(self, item: CleanupItem) -> Callable[[None], None]:
        """Build the removal callback refreshing the item category."""

        def _handler(_result: None) -> None:
            self._set_busy(False)
            self.refresh_category(item.category)

        return _handler

    def _make_remove_error_handler(self, item: CleanupItem) -> Callable[[BaseException], None]:
        """Build the removal-error callback showing the error then refreshing."""

        def _handler(error: BaseException) -> None:
            self._set_busy(False)
            self._expanders[item.category].set_title(_("Removal failed."))
            self._show_error(_("Remove Item"), error)
            self.refresh_category(item.category)

        return _handler

    def _set_busy(self, busy: bool) -> None:
        """Disable category groups while a removal runs."""
        self._busy = busy
        sensitive = not busy
        for group in self._groups.values():
            group.set_sensitive(sensitive)

    def _show_error(self, operation: str, error: BaseException) -> None:
        """Show a single-close error dialog splitting BoxError from unexpected bugs."""
        _show_error(self, operation, error)

    @staticmethod
    def _clear_rows(expander: Adw.ExpanderRow, rows: list[Adw.ActionRow]) -> None:
        """Remove tracked rows from a category expander."""
        for row in rows:
            expander.remove(row)
        rows.clear()


class SettingsDialog(Adw.PreferencesDialog):
    """Modal settings dialog with General, NW.js, EasyRPG, and cleanup pages.

    Callers show it with ``.present(parent_window)``. Dialog search stays
    disabled: libadwaita matches every titled row dialog-wide with no way
    to scope the filter to option sections.
    """

    def __init__(
        self,
        paths: AppPaths,
        repository: ConfigRepository,
        interaction: Interaction | None = None,
        library: LibraryRepository | None = None,
    ) -> None:
        """Build and add the four settings pages without dialog search."""
        super().__init__(title=_("Settings"))
        if hasattr(self, "set_search_enabled"):
            self.set_search_enabled(False)
        self._general_page = GeneralPage(paths, repository, interaction)
        self._nwjs_page = NwjsPage(paths)
        self._easyrpg_page = EasyrpgPage(paths)
        self._cleanup_page = CleanupPage(paths, repository, library)
        self.add(self._general_page)
        self.add(self._nwjs_page)
        self.add(self._easyrpg_page)
        self.add(self._cleanup_page)

    @property
    def pages(self) -> tuple[Adw.PreferencesPage, ...]:
        """Return the four hosted pages in dialog order."""
        return (
            self._general_page,
            self._nwjs_page,
            self._easyrpg_page,
            self._cleanup_page,
        )


def _category_title(category: str) -> str:
    """Return the human-readable group title for one cleanup category."""
    titles = {
        "roots": _("Authorized game roots"),
        "runtimes": _("Managed runtimes"),
        "downloads": _("Download archives"),
        "profiles": _("Game profiles"),
    }
    return titles.get(category, category)


def _category_description(category: str) -> str | None:
    """Return the group description for one cleanup category, if any."""
    if category == "profiles":
        return _("Additional runtime settings or cache are stored here.")
    return None
