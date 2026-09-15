"""Library navigation page listing remembered games."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from box.api import AppPaths, ConfigRepository  # noqa: E402
from box.api.inspect import Inspection  # noqa: E402
from box.api.interaction import Interaction  # noqa: E402
from box.errors import BoxError  # noqa: E402
from box.models import EngineName  # noqa: E402
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from box_gui.core.defaults import DefaultsRepository  # noqa: E402
from box_gui.core.library import (  # noqa: E402
    LibraryEntry,
    LibraryError,
    LibraryRepository,
    ReorderDirection,
)
from box_gui.gtk.interaction import AllowX11Interaction  # noqa: E402
from box_gui.i18n import _  # noqa: E402
from box_gui.widgets.icon_widget import build_game_icon  # noqa: E402

__all__ = ["LibraryPage", "default_display_name", "inspection_error_heading"]

# Short runtime names shown in library pills. Proper names stay untranslated.
_ENGINE_PILL_NAMES: dict[str, str] = {
    "rpg-maker-2000-2003": "EasyRPG",
    "rpg-maker-mv": "NW.js",
    "rpg-maker-mz": "NW.js",
}

_ROW_MENU_ICON = "box-rpg-dots-symbolic"


def default_display_name(inspection: Inspection) -> str:
    """Derive the library display-name default from an inspection."""
    if inspection.title:
        return inspection.title
    return inspection.game.root.name


def inspection_error_heading(error: BaseException) -> str:
    """Return the alert heading for an inspection failure."""
    if isinstance(error, BoxError):
        return _("Inspection Failed")
    return _("Unexpected Error")


def _pill_text(entry: LibraryEntry) -> str:
    """Return the runtime pill text, never inventing a version."""
    engine_name = _ENGINE_PILL_NAMES.get(entry.engine or "")
    if engine_name is not None:
        if entry.preferred_runtime:
            return f"{engine_name} {entry.preferred_runtime}"
        return engine_name
    return entry.preferred_runtime or _("Latest")


def _row_menu_model() -> Gio.Menu:
    """Build the shared reorder/remove menu model for library rows."""
    menu = Gio.Menu()
    menu.append(_("Move up"), "row.move-up")
    menu.append(_("Move down"), "row.move-down")
    menu.append(_("Move to top"), "row.move-to-top")
    menu.append(_("Move to bottom"), "row.move-to-bottom")
    menu.append(_("Remove from list"), "row.remove")
    return menu


class LibraryPage(Adw.NavigationPage):
    """Root navigation page rendering the frontend-owned game library."""

    def __init__(
        self,
        library: LibraryRepository,
        on_open_game: Callable[[LibraryEntry], None] | None = None,
        on_open_settings: Callable[[Gtk.Widget], None] | None = None,
        paths: AppPaths | None = None,
        repository: ConfigRepository | None = None,
        interaction: Interaction | None = None,
        defaults_repository: DefaultsRepository | None = None,
    ) -> None:
        super().__init__()
        self.set_title(_("Library"))
        self.set_tag("library")
        self._library = library
        self._on_open_game = on_open_game
        self._on_open_settings = on_open_settings
        self._paths = paths
        self._repository = repository
        self._defaults_repository = defaults_repository
        self._interaction = interaction
        self._entries: tuple[LibraryEntry, ...] = ()
        self._add_button = Gtk.Button.new_from_icon_name("box-rpg-plus-symbolic")
        self._add_button.set_tooltip_text(_("Add game"))
        self._add_button.connect("clicked", self._on_add_clicked)
        self._settings_button = Gtk.Button.new_from_icon_name("box-rpg-settings-symbolic")
        self._settings_button.set_tooltip_text(_("Settings"))
        self._settings_button.connect("clicked", self._on_settings_clicked)
        self._list_box = Gtk.ListBox()
        self._list_box.add_css_class("boxed-list-separate")
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._hint = Gtk.Label(label=_("Your library is empty. Click + to add your first game."))
        self._hint.set_wrap(True)
        self._hint.add_css_class("dim")
        self._file_dialog: Gtk.FileDialog | None = None
        self.set_child(self._build_view())
        self.refresh()

    def _build_view(self) -> Adw.ToolbarView:
        """Assemble the header bar, game list, and footer hint."""
        view = Adw.ToolbarView()
        view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        header = Adw.HeaderBar()
        header.pack_start(self._add_button)
        header.pack_end(self._settings_button)
        self._header_bar = header
        view.add_top_bar(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.append(self._list_box)
        content.append(self._hint)
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

    def refresh(self) -> None:
        """Reload rows from the repository, alerting on a corrupt file."""
        try:
            entries = self._library.load()
        except LibraryError as exc:
            self._show_alert(_("Library Error"), str(exc) or exc.__class__.__name__)
            return
        self._entries = entries
        self._hint.set_visible(len(entries) == 0)
        child = self._list_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._list_box.remove(child)
            child = following
        for entry in entries:
            self._list_box.append(self._build_row(entry, show_reorder=len(entries) > 1))

    def inspect_and_add(self, path: Path) -> None:
        """Inspect a picked folder, add it, then open its detail page.

        A newly added game pushes straight into its detail page so the
        user lands where display name, runtime, and files are configured.
        """
        try:
            from box_gui.gtk.workers import run_inspect
        except ImportError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        run_inspect(path, self._on_inspect_done, self._on_inspect_error)

    def _build_row(self, entry: LibraryEntry, *, show_reorder: bool = True) -> Adw.ActionRow:
        """Build one title-only row with an icon, runtime pill, menu, and launch button."""
        # ActionRow renders the title as Pango markup, so escape display
        # names like "Fear & Hunger". There is no subtitle and no tooltip.
        row = Adw.ActionRow(title=GLib.markup_escape_text(entry.display_name, -1))
        row.set_activatable(True)
        icon = build_game_icon(entry)
        if icon is not None:
            row.add_prefix(icon)
        pill = Gtk.Label(label=_pill_text(entry))
        pill.add_css_class("caption")
        pill.add_css_class("dim")
        pill.add_css_class("runtime-pill")
        pill.set_valign(Gtk.Align.CENTER)
        menu = _row_menu_model()
        reorder_menu = Gtk.MenuButton()
        reorder_menu.set_icon_name(_ROW_MENU_ICON)
        reorder_menu.set_valign(Gtk.Align.CENTER)
        reorder_menu.add_css_class("flat")
        reorder_menu.set_tooltip_text(_("Reorder or remove"))
        reorder_menu.set_popover(Gtk.PopoverMenu.new_from_model(menu))
        # A single row cannot be reordered, so its menu stays hidden.
        reorder_menu.set_visible(show_reorder)
        # MenuButton consumes its own clicks, so pressing it never
        # activates the row underneath.
        launch_button = Gtk.Button.new_from_icon_name("box-rpg-rocket-symbolic")
        launch_button.set_tooltip_text(_("Launch"))
        launch_button.set_valign(Gtk.Align.CENTER)
        launch_button.add_css_class("flat")
        # The button consumes its own clicks, so pressing it never
        # activates the row underneath.
        if self._paths is None or self._repository is None:
            launch_button.set_sensitive(False)
        else:
            launch_button.connect("clicked", self._on_launch_clicked, entry)
        row.add_suffix(pill)
        row.add_suffix(reorder_menu)
        row.add_suffix(launch_button)
        row.connect("activated", self._on_row_activated, entry)
        row.add_controller(self._context_controller(entry, row, menu))
        return row

    def _context_controller(
        self,
        entry: LibraryEntry,
        row: Adw.ActionRow,
        menu: Gio.Menu | None = None,
    ) -> Gtk.GestureClick:
        """Build the right-click reorder/remove menu for one row."""
        model = menu if menu is not None else _row_menu_model()
        actions = Gio.SimpleActionGroup()
        moves: tuple[tuple[str, ReorderDirection], ...] = (
            ("move-up", "up"),
            ("move-down", "down"),
            ("move-to-top", "top"),
            ("move-to-bottom", "bottom"),
        )
        for action_name, direction in moves:
            action = Gio.SimpleAction.new(action_name, None)
            action.connect("activate", self._on_move_action, entry, direction)
            actions.add_action(action)
        remove_action = Gio.SimpleAction.new("remove", None)
        remove_action.connect("activate", self._on_remove_action, entry)
        actions.add_action(remove_action)
        row.insert_action_group("row", actions)
        popover = Gtk.PopoverMenu.new_from_model(model)
        popover.set_parent(row)
        popover.set_has_arrow(False)
        gesture = Gtk.GestureClick.new()
        gesture.set_button(3)
        gesture.connect("pressed", self._on_row_pressed, popover)
        return gesture

    def _on_row_pressed(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
        popover: Gtk.PopoverMenu,
    ) -> None:
        """Show the row context menu on right-click."""
        popover.popup()

    def _on_row_activated(self, _row: Adw.ActionRow, entry: LibraryEntry) -> None:
        """Push the detail page for the activated row."""
        if self._on_open_game is not None:
            self._on_open_game(entry)

    def _on_launch_clicked(self, button: Gtk.Button, entry: LibraryEntry) -> None:
        """Launch one entry directly with its persisted options."""
        if self._paths is None or self._repository is None:
            return
        try:
            from box_gui.gtk.workers import run_inspect
        except ImportError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        button.set_sensitive(False)
        run_inspect(
            entry.path,
            lambda inspection: self._on_launch_inspect_done(inspection, entry, button),
            lambda error: self._on_launch_inspect_error(error, button),
        )

    def _on_launch_inspect_done(
        self, inspection: Inspection, entry: LibraryEntry, button: Gtk.Button
    ) -> None:
        """Start the launch once the pre-launch inspection succeeds."""
        try:
            from box_gui.gtk.workers import run_launch
        except ImportError as exc:
            button.set_sensitive(True)
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        paths = self._paths
        repository = self._repository
        if paths is None or repository is None:
            button.set_sensitive(True)
            return
        fresh = self._fresh_entry(entry)
        version = fresh.preferred_runtime
        sdk = fresh.preferred_sdk
        copy_root_files = fresh.copy_root_files
        allow_network = fresh.allow_network
        allow_game_writes = fresh.allow_game_writes
        use_x11 = fresh.allow_x11
        if inspection.game.engine is EngineName.RPG_MAKER_2000_2003:
            sdk = False
            copy_root_files = ()
            if version is None:
                version = self._global_easyrpg_runtime()
        interaction = self._interaction
        if use_x11:
            interaction = AllowX11Interaction(self._interaction)
        run_launch(
            paths,
            repository,
            inspection.game.root,
            interaction,
            lambda code: self._on_launch_done(button, code),
            lambda error: self._on_launch_error(error, button),
            version=version,
            sdk=sdk,
            copy_root_files=copy_root_files,
            allow_network=allow_network,
            allow_game_writes=allow_game_writes,
            x11=use_x11,
        )

    def _global_nwjs_runtime(self) -> str | None:
        """Return the global NW.js preferred runtime, if configured."""
        repository = self._repository
        if repository is None:
            return None
        try:
            return repository.load().preferred_runtime
        except Exception:
            return None

    def _global_easyrpg_runtime(self) -> str | None:
        """Return the global EasyRPG preferred runtime, if configured."""
        defaults = self._defaults_repository
        if defaults is None:
            if self._paths is None:
                return None
            try:
                defaults = DefaultsRepository(self._paths)
            except Exception:
                return None
        try:
            return defaults.load().preferred_easyrpg_runtime
        except Exception:
            return None

    def _preferred_runtime_for_engine(self, engine: EngineName) -> str | None:
        """Return the global preferred runtime matching one engine."""
        if engine is EngineName.RPG_MAKER_2000_2003:
            return self._global_easyrpg_runtime()
        if engine is EngineName.RPG_MAKER_MV or engine is EngineName.RPG_MAKER_MZ:
            return self._global_nwjs_runtime()
        return None

    def _fresh_entry(self, entry: LibraryEntry) -> LibraryEntry:
        """Reload one entry so quick-launch uses options edited in detail."""
        try:
            stored_entries = self._library.load()
        except LibraryError, OSError:
            return entry
        for stored in stored_entries:
            if stored.path == entry.path:
                return stored
        return entry

    def _on_launch_inspect_error(self, error: BaseException, button: Gtk.Button) -> None:
        """Show pre-launch inspection failures without starting a launch."""
        button.set_sensitive(True)
        message = str(error) or error.__class__.__name__
        self._show_alert(inspection_error_heading(error), message)

    def _on_launch_done(self, button: Gtk.Button, _code: int) -> None:
        """Reactivate the launch button after a clean launch exit."""
        button.set_sensitive(True)

    def _on_launch_error(self, error: BaseException, button: Gtk.Button) -> None:
        """Show quick-launch failures and reactivate the launch button."""
        button.set_sensitive(True)

        def _heading(exc: BaseException) -> str:
            if isinstance(exc, BoxError):
                return _("Launch Failed")
            return _("Unexpected Error")

        message = str(error) or error.__class__.__name__
        self._show_alert(_heading(error), message)

    def _on_move_action(
        self,
        _action: Gio.SimpleAction,
        _parameter: object,
        entry: LibraryEntry,
        direction: ReorderDirection,
    ) -> None:
        """Reorder one entry, then refresh the visible list."""
        try:
            self._library.reorder(entry, direction)
        except LibraryError as exc:
            self._show_alert(_("Library Error"), str(exc) or exc.__class__.__name__)
            return
        except OSError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        self.refresh()

    def _on_remove_action(
        self, _action: Gio.SimpleAction, _parameter: object, entry: LibraryEntry
    ) -> None:
        """Remove one entry, then refresh the visible list."""
        try:
            self._library.remove(entry)
        except LibraryError as exc:
            self._show_alert(_("Library Error"), str(exc) or exc.__class__.__name__)
            return
        except OSError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        self.refresh()

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        """Open an async folder picker for a game directory."""
        dialog = Gtk.FileDialog(title=_("Select Game Folder"))
        self._file_dialog = dialog
        parent = self.get_root()
        dialog.select_folder(
            parent if isinstance(parent, Gtk.Window) else None,
            None,
            self._on_folder_chosen,
        )

    def _on_folder_chosen(self, source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        """Start inspection for the folder chosen in the async callback."""
        self._file_dialog = None
        try:
            folder = source.select_folder_finish(result)
        except GLib.Error as exc:
            if exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                return
            self._show_alert(_("Unexpected Error"), exc.message)
            return
        path = folder.get_path()
        if path is None:
            self._show_alert(
                _("Unexpected Error"),
                _("Cannot resolve a local path for the selection."),
            )
            return
        self.inspect_and_add(Path(path))

    def _on_inspect_done(self, inspection: Inspection) -> None:
        """Add the inspected game, then open its detail page."""
        try:
            preferred_runtime = self._preferred_runtime_for_engine(inspection.game.engine)
        except Exception:
            preferred_runtime = None
        try:
            created = self._library.add(
                inspection.game.root,
                default_display_name(inspection),
                inspection.game.engine.value,
                preferred_runtime=preferred_runtime,
            )
        except LibraryError as exc:
            self._show_alert(_("Library Error"), str(exc) or exc.__class__.__name__)
            return
        except OSError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        self.refresh()
        if self._on_open_game is not None:
            self._on_open_game(created)

    def _on_inspect_error(self, error: BaseException) -> None:
        """Show inspection failures with an Adw.AlertDialog."""
        message = str(error) or error.__class__.__name__
        self._show_alert(inspection_error_heading(error), message)

    def _on_settings_clicked(self, _button: Gtk.Button) -> None:
        """Present the Settings dialog through the wired callback."""
        if self._on_open_settings is not None:
            self._on_open_settings(self)

    def _show_alert(self, heading: str, body: str) -> None:
        """Present a single-close alert over this page."""
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present(self)
