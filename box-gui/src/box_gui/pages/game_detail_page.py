"""Game detail navigation page with launch and diagnose actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from box.api import AppPaths, ConfigRepository  # noqa: E402
from box.api.inspect import Inspection  # noqa: E402
from box.api.interaction import Interaction  # noqa: E402
from box.api.launch import list_root_files  # noqa: E402
from box.errors import BoxError  # noqa: E402
from box.errors import RuntimeError as BoxRuntimeError  # noqa: E402
from box.models import EngineName, GameInfo  # noqa: E402
from box.runtime.catalog import RuntimeCatalog  # noqa: E402
from box.runtime.easyrpg import EasyRPGCatalog  # noqa: E402
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from box_gui.core.defaults import DefaultsRepository  # noqa: E402
from box_gui.core.display import abbreviate_display_path  # noqa: E402
from box_gui.core.game_icon import (  # noqa: E402
    extract_icon_png,
    find_game_executables,
    icon_path_for_game,
    install_image_as_icon,
)
from box_gui.core.library import LibraryEntry, LibraryError, LibraryRepository  # noqa: E402
from box_gui.gtk.interaction import AllowX11Interaction as AllowX11Interaction  # noqa: E402
from box_gui.i18n import _  # noqa: E402
from box_gui.pages.diagnose_dialog import DiagnoseDialog  # noqa: E402
from box_gui.widgets.exe_picker import ExesChoice, present_exe_picker  # noqa: E402
from box_gui.widgets.icon_widget import build_game_icon  # noqa: E402

__all__ = [
    "AllowX11Interaction",
    "GameDetailPage",
    "inspection_error_heading",
    "launch_error_heading",
]

# Binary, executable, and media files are never useful as extra-root copies.
_SKIPPED_ROOT_SUFFIXES: frozenset[str] = frozenset(
    {".pak", ".bin", ".exe", ".dll", ".dat", ".html", ".png", ".jpg", ".webp"}
)


def inspection_error_heading(error: BaseException) -> str:
    """Return the alert heading for an inspection failure."""
    if isinstance(error, BoxError):
        return _("Inspection Failed")
    return _("Unexpected Error")


def launch_error_heading(error: BaseException) -> str:
    """Return the alert heading for a launch failure."""
    if isinstance(error, BoxError):
        return _("Launch Failed")
    return _("Unexpected Error")


class GameDetailPage(Adw.NavigationPage):
    """Detail page re-inspecting one library entry with launch actions."""

    def __init__(
        self,
        entry: LibraryEntry,
        paths: AppPaths,
        repository: ConfigRepository,
        library: LibraryRepository,
        interaction: Interaction | None = None,
        on_open_runtimes: Callable[[Gtk.Widget], None] | None = None,
    ) -> None:
        super().__init__()
        self.set_title(_("Game Detail"))
        self.set_tag("game-detail")
        self._entry = entry
        self._paths = paths
        self._repository = repository
        self._library = library
        self._interaction = interaction
        self._on_open_runtimes = on_open_runtimes
        self._inspection: Inspection | None = None
        self._runtime_options: tuple[str, ...] = ()
        self._shown_runtime_options: tuple[str, ...] = ()
        self._file_options: tuple[str, ...] = ()
        self._loading = True
        self._title_label = Gtk.Label(label=entry.display_name)
        self._launch_button = Gtk.Button.new_from_icon_name("box-rpg-rocket-symbolic")
        self._launch_button.set_tooltip_text(_("Launch"))
        self._launch_button.add_css_class("suggested-action")
        self._launch_button.set_sensitive(False)
        self._launch_button.connect("clicked", self._on_launch_clicked)
        self._diagnose_button = Gtk.Button(label=_("Diagnose"))
        self._diagnose_button.set_sensitive(False)
        self._diagnose_button.connect("clicked", self._on_diagnose_clicked)
        self._spinner = Gtk.Spinner()
        self._status = Gtk.Label(label="")
        self._status.set_xalign(0.0)
        self._status.set_hexpand(True)
        self._status.set_ellipsize(Pango.EllipsizeMode.END)
        self._display_row = Adw.EntryRow(title=_("Name"))
        self._display_row.set_max_length(64)
        self._display_row.set_text(entry.display_name)
        self._display_row.connect("changed", self._on_display_name_changed)
        self._icon_row = Adw.ActionRow(title=_("Icon"))
        self._icon_slot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._icon_slot.set_valign(Gtk.Align.CENTER)
        self._icon_row.add_prefix(self._icon_slot)
        self._change_icon_button = Gtk.Button(label=_("Change…"))
        self._change_icon_button.set_valign(Gtk.Align.CENTER)
        self._change_icon_button.connect("clicked", self._on_change_icon_clicked)
        self._icon_row.add_suffix(self._change_icon_button)
        self._icon_file_dialog: Gtk.FileDialog | None = None
        self._engine_row = Adw.ActionRow(title=_("Engine"), subtitle="—")
        self._root_row = Adw.ActionRow(title=_("Root"), subtitle="—")
        self._entrypoint_row = Adw.ActionRow(title=_("Entrypoint"), subtitle="—")
        self._info_title_row = Adw.ActionRow(title=_("Title"), subtitle="—")
        self._plugins_row = Adw.ActionRow(title=_("Plugins"), subtitle="—")
        self._runtime_row = Adw.ComboRow(title=_("Preferred runtime"))
        self._runtime_row.connect("notify::selected", self._on_runtime_changed)
        self._sdk_row = Adw.SwitchRow(title=_("SDK"))
        self._sdk_row.connect("notify::active", self._on_sdk_toggled)
        self._files_group = Adw.PreferencesGroup(
            title=_("Additional files"),
            description=_(
                "Some games keep language or settings files in the game root. "
                "Selected files are copied alongside the game on launch."
            ),
        )
        self._chips = Gtk.FlowBox()
        self._chips.add_css_class("chip-flow")
        self._chips.set_selection_mode(Gtk.SelectionMode.NONE)
        self._add_file_button = Gtk.Button(label=_("Add file"))
        self._add_file_button.connect("clicked", self._on_add_files_clicked)
        self._network_switch = Adw.SwitchRow(title=_("Allow network usage"))
        self._network_switch.connect("notify::active", self._on_network_toggled)
        self._writes_switch = Adw.SwitchRow(
            title=_("Allow modifying the game"),
            subtitle=_("For games with automatic updates."),
        )
        self._writes_switch.connect("notify::active", self._on_writes_toggled)
        self._x11_switch = Adw.SwitchRow(title=_("Allow X11 or XWayland sessions"))
        self._x11_switch.connect("notify::active", self._on_x11_toggled)
        self.set_child(self._build_view())
        self._sync_runtime_selection()
        self._sdk_row.set_active(entry.preferred_sdk)
        self._network_switch.set_active(entry.allow_network)
        self._writes_switch.set_active(entry.allow_game_writes)
        self._x11_switch.set_active(entry.allow_x11)
        self._loading = False
        self.refresh()

    @property
    def game_path(self) -> Path:
        """Return the inspected root, falling back to the stored path."""
        if self._inspection is not None:
            return self._inspection.game.root
        return self._entry.path

    def _build_view(self) -> Adw.ToolbarView:
        """Assemble the header, status row, and detail groups."""
        view = Adw.ToolbarView()
        view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        header = Adw.HeaderBar()
        header.set_title_widget(self._title_label)
        header.pack_end(self._launch_button)
        view.add_top_bar(header)
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_box.append(self._spinner)
        status_box.append(self._status)
        self._status_box = status_box
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.append(status_box)
        display_group = Adw.PreferencesGroup(title=_("Display name"))
        display_group.add(self._display_row)
        display_group.add(self._icon_row)
        content.append(display_group)
        self._refresh_icon_preview()
        info_group = Adw.PreferencesGroup(title=_("Game info"))
        info_group.add(self._engine_row)
        info_group.add(self._root_row)
        info_group.add(self._entrypoint_row)
        info_group.add(self._info_title_row)
        info_group.add(self._plugins_row)
        content.append(info_group)
        runtime_group = Adw.PreferencesGroup(
            title=_("Runtime"),
            description=_(
                "By default it uses the version defined in Settings; "
                "otherwise the most recently downloaded one."
            ),
        )
        runtime_group.add(self._runtime_row)
        runtime_group.add(self._sdk_row)
        content.append(runtime_group)
        files_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        files_box.append(self._chips)
        files_box.append(self._add_file_button)
        self._files_group.add(files_box)
        content.append(self._files_group)
        permissions_group = Adw.PreferencesGroup(
            title=_("Sandbox permissions"),
            description=_("Permissions apply when mounting the game in the sandbox."),
        )
        permissions_group.add(self._network_switch)
        permissions_group.add(self._writes_switch)
        permissions_group.add(self._x11_switch)
        content.append(permissions_group)
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.append(self._diagnose_button)
        content.append(footer)
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
        """Re-inspect the stored path so the page never shows stale data."""
        try:
            from box_gui.gtk.workers import run_inspect
        except ImportError as exc:
            self._on_inspect_error(exc)
            return
        self._set_busy(True, _("Inspecting {path} …").format(path=self._entry.path))
        run_inspect(self._entry.path, self._on_inspect_done, self._on_inspect_error)

    def _set_busy(self, busy: bool, message: str) -> None:
        """Toggle the spinner and button sensitivity with a status message."""
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._status.set_text(message)
        self._status_box.set_visible(bool(message))
        sensitive = self._inspection is not None and not busy
        self._launch_button.set_sensitive(sensitive)
        self._diagnose_button.set_sensitive(sensitive)

    def _on_inspect_done(self, inspection: Inspection) -> None:
        """Populate read-only rows and runtime choices without renaming."""
        self._loading = True
        try:
            self._inspection = inspection
            game = inspection.game
            entrypoint = str(game.entrypoint) if game.entrypoint is not None else "—"
            self._engine_row.set_subtitle(GLib.markup_escape_text(game.engine.value, -1))
            self._root_row.set_subtitle(
                GLib.markup_escape_text(abbreviate_display_path(game.root), -1)
            )
            self._root_row.set_tooltip_text(str(game.root))
            self._entrypoint_row.set_subtitle(GLib.markup_escape_text(entrypoint, -1))
            self._info_title_row.set_subtitle(GLib.markup_escape_text(inspection.title or "—", -1))
            self._plugins_row.set_subtitle(str(inspection.plugin_count))
            easyrpg = game.engine is EngineName.RPG_MAKER_2000_2003
            self._sdk_row.set_visible(not easyrpg)
            self._files_group.set_visible(not easyrpg)
            self._entrypoint_row.set_visible(not easyrpg)
            self._plugins_row.set_visible(not easyrpg)
            self._runtime_options = self._runtime_versions(game.engine)
            self._file_options = self._root_file_options(game) if not easyrpg else ()
            self._sync_runtime_selection()
            self._rebuild_chips()
            engine_value = game.engine.value
            if self._entry.engine != engine_value:
                self._persist(replace(self._entry, engine=engine_value))
            self._refresh_icon_preview()
        finally:
            self._loading = False
        self._set_busy(False, "")

    def _on_inspect_error(self, error: BaseException) -> None:
        """Show inspection failures with an Adw.AlertDialog."""
        self._inspection = None
        self._set_busy(False, _("Inspection failed."))
        message = str(error) or error.__class__.__name__
        self._show_alert(inspection_error_heading(error), message)

    def _runtime_versions(self, engine: EngineName) -> tuple[str, ...]:
        """List installed runtime versions for one engine, newest first."""
        if engine is EngineName.RPG_MAKER_2000_2003:
            return tuple(runtime.version for runtime in EasyRPGCatalog(self._paths).list())
        versions = [runtime.spec.version for runtime in RuntimeCatalog(self._paths).list()]
        return tuple(dict.fromkeys(versions))

    def _root_file_options(self, game: GameInfo) -> tuple[str, ...]:
        """List candidate extra-root filenames, tolerating unreadable roots."""
        try:
            names = tuple(list_root_files(game))
        except BoxError, OSError:
            return ()
        return tuple(
            name for name in names if Path(name).suffix.lower() not in _SKIPPED_ROOT_SUFFIXES
        )

    def _sync_runtime_selection(self) -> None:
        """Rebuild the version dropdown, keeping the persisted selection."""
        options = list(self._runtime_options)
        preferred = self._entry.preferred_runtime
        if preferred is not None and preferred not in options:
            options.append(preferred)
        self._shown_runtime_options = tuple(options)
        first = _("Default") if self._global_default_runtime() is not None else _("Undefined")
        names = [first, *options]
        self._runtime_row.set_model(Gtk.StringList.new(names))
        selected = 0
        if preferred in options:
            selected = options.index(preferred) + 1
        self._runtime_row.set_selected(selected)

    def _selected_runtime_version(self) -> str | None:
        """Map the dropdown position back to a version, if any."""
        selected = self._runtime_row.get_selected()
        if selected == 0 or selected == Gtk.INVALID_LIST_POSITION:
            return None
        index = int(selected) - 1
        if 0 <= index < len(self._shown_runtime_options):
            return self._shown_runtime_options[index]
        return None

    def _on_display_name_changed(self, row: Adw.EntryRow) -> None:
        """Persist free-text display-name edits without touching the order."""
        if self._loading:
            return
        text = row.get_text()
        if text == self._entry.display_name:
            return
        self._persist(replace(self._entry, display_name=text))
        self._title_label.set_text(self._entry.display_name)

    def _on_runtime_changed(self, _row: Adw.ComboRow, _pspec: object) -> None:
        """Persist runtime version picks without touching other fields."""
        if self._loading:
            return
        version = self._selected_runtime_version()
        if version == self._entry.preferred_runtime:
            return
        self._persist(replace(self._entry, preferred_runtime=version))

    def _on_sdk_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        """Persist SDK flavor picks without touching other fields."""
        if self._loading:
            return
        active = row.get_active()
        if active == self._entry.preferred_sdk:
            return
        self._persist(replace(self._entry, preferred_sdk=active))

    def _on_network_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        """Persist network permission picks without touching other fields."""
        if self._loading:
            return
        active = row.get_active()
        if active == self._entry.allow_network:
            return
        self._persist(replace(self._entry, allow_network=active))

    def _on_writes_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        """Persist game-writes permission picks without touching other fields."""
        if self._loading:
            return
        active = row.get_active()
        if active == self._entry.allow_game_writes:
            return
        self._persist(replace(self._entry, allow_game_writes=active))

    def _on_x11_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        """Persist X11 permission picks without touching other fields."""
        if self._loading:
            return
        active = row.get_active()
        if active == self._entry.allow_x11:
            return
        self._persist(replace(self._entry, allow_x11=active))

    def _persist(self, updated: LibraryEntry) -> None:
        """Store one entry edit and remember it as the current snapshot."""
        try:
            self._entry = self._library.update(updated)
        except LibraryError as exc:
            self._show_alert(_("Library Error"), str(exc) or exc.__class__.__name__)
        except OSError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)

    def _refresh_icon_preview(self) -> None:
        """Show the current game icon at the head of the Icon row."""
        child = self._icon_slot.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._icon_slot.remove(child)
            child = following
        preview = build_game_icon(self._entry)
        if preview is not None:
            self._icon_slot.append(preview)

    def _on_change_icon_clicked(self, _button: Gtk.Button) -> None:
        """Offer the executables or the engine default as the game icon."""
        inspection = self._inspection
        exes = find_game_executables(inspection.game) if inspection is not None else ()
        if exes or self._entry.icon_path is not None:
            present_exe_picker(self, exes, self._on_exe_chosen)
        else:
            self._pick_image_file()

    def _on_exe_chosen(self, chosen: ExesChoice) -> None:
        """Apply the picked executable icon, default, or cancel."""
        if chosen is None:
            return
        if chosen == "default":
            self._clear_cached_icon()
            return
        dest = icon_path_for_game(self._paths.config_root, self._entry.path)
        if extract_icon_png(chosen, dest):
            self._persist(replace(self._entry, icon_path=dest))
            self._refresh_icon_preview()

    def _clear_cached_icon(self) -> None:
        """Forget the custom icon, removing the cached file when present."""
        previous = self._entry.icon_path
        if previous is not None:
            try:
                previous.unlink(missing_ok=True)
            except OSError as exc:
                self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
                return
            self._persist(replace(self._entry, icon_path=None))
        self._refresh_icon_preview()

    def _pick_image_file(self) -> None:
        """Open an image picker used as the icon when no executable exists."""
        dialog = Gtk.FileDialog(title=_("Choose Icon Image"))
        images = Gtk.FileFilter()
        images.set_name(_("Images"))
        images.add_mime_type("image/png")
        images.add_mime_type("image/jpeg")
        images.add_mime_type("image/webp")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        dialog.set_filters(filters)
        dialog.set_default_filter(images)
        self._icon_file_dialog = dialog
        parent = self.get_root()
        dialog.open(
            parent if isinstance(parent, Gtk.Window) else None,
            None,
            self._on_image_chosen,
        )

    def _on_image_chosen(self, source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        """Install the picked image as the game icon."""
        self._icon_file_dialog = None
        try:
            chosen = source.open_finish(result)
        except GLib.Error as exc:
            if exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                return
            self._show_alert(_("Unexpected Error"), exc.message)
            return
        path = chosen.get_path()
        if path is None:
            self._show_alert(
                _("Unexpected Error"),
                _("Cannot resolve a local path for the selection."),
            )
            return
        dest = icon_path_for_game(self._paths.config_root, self._entry.path)
        if install_image_as_icon(Path(path), dest):
            self._persist(replace(self._entry, icon_path=dest))
            self._refresh_icon_preview()
        else:
            self._show_alert(
                _("Unexpected Error"),
                _("Cannot use that file as the game icon."),
            )

    def _global_easyrpg_runtime(self) -> str | None:
        """Return the global EasyRPG preferred runtime, if configured."""
        try:
            return DefaultsRepository(self._paths).load().preferred_easyrpg_runtime
        except Exception:
            return None

    def _global_default_runtime(self) -> str | None:
        """Return the global preferred runtime for the current engine, if any."""
        inspection = self._inspection
        if inspection is not None:
            engine = inspection.game.engine
        elif self._entry.engine == EngineName.RPG_MAKER_2000_2003.value:
            engine = EngineName.RPG_MAKER_2000_2003
        elif self._entry.engine in (
            EngineName.RPG_MAKER_MV.value,
            EngineName.RPG_MAKER_MZ.value,
        ):
            engine = EngineName.RPG_MAKER_MV
        else:
            return None
        if engine is EngineName.RPG_MAKER_2000_2003:
            return self._global_easyrpg_runtime()
        try:
            return self._repository.load().preferred_runtime
        except Exception:
            return None

    def _rebuild_chips(self) -> None:
        """Render one removable chip per selected extra-root file."""
        child = self._chips.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._chips.remove(child)
            child = following
        for name in self._entry.copy_root_files:
            chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            chip.set_halign(Gtk.Align.START)
            pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            pill.add_css_class("runtime-pill")
            pill.append(Gtk.Label(label=name))
            remove = Gtk.Button.new_from_icon_name("box-rpg-x-symbolic")
            remove.add_css_class("flat")
            remove.add_css_class("chip-remove")
            remove.set_valign(Gtk.Align.CENTER)
            remove.set_tooltip_text(_("Remove file"))
            remove.connect("clicked", self._on_remove_file_clicked, name)
            pill.append(remove)
            chip.append(pill)
            self._chips.append(chip)

    def _on_remove_file_clicked(self, _button: Gtk.Button, name: str) -> None:
        """Drop one extra-root file from the persisted selection."""
        remaining = tuple(item for item in self._entry.copy_root_files if item != name)
        if remaining == self._entry.copy_root_files:
            return
        self._persist(replace(self._entry, copy_root_files=remaining))
        self._rebuild_chips()

    def _on_add_files_clicked(self, _button: Gtk.Button) -> None:
        """Present the multi-select dialog over list_root_files candidates."""
        dialog = Adw.Dialog(title=_("Add file"), content_width=480, content_height=480)
        header = Adw.HeaderBar()
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", self._on_add_files_cancelled, dialog)
        header.pack_start(cancel)
        confirm = Gtk.Button(label=_("Add"))
        confirm.add_css_class("suggested-action")
        confirm.set_sensitive(bool(self._file_options))
        header.pack_end(confirm)
        group = Adw.PreferencesGroup(title=_("Additional files"))
        switches: dict[str, Adw.SwitchRow] = {}
        for name in self._file_options:
            switch = Adw.SwitchRow(title=GLib.markup_escape_text(name, -1))
            switch.set_active(name in self._entry.copy_root_files)
            group.add(switch)
            switches[name] = switch
        if not self._file_options:
            empty = Adw.ActionRow(title=_("No eligible files."))
            empty.set_sensitive(False)
            group.add(empty)
        confirm.connect("clicked", self._on_add_files_confirmed, dialog, switches)
        view = Adw.ToolbarView()
        view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        view.add_top_bar(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.append(group)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        clamp = Adw.Clamp(child=content)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(clamp)
        view.set_content(scrolled)
        dialog.set_child(view)
        dialog.present(self)

    def _on_add_files_cancelled(self, _button: Gtk.Button, dialog: Adw.Dialog) -> None:
        """Close the multi-select dialog without changing the selection."""
        dialog.close()

    def _on_add_files_confirmed(
        self,
        _button: Gtk.Button,
        dialog: Adw.Dialog,
        switches: dict[str, Adw.SwitchRow],
    ) -> None:
        """Persist the multi-select dialog choices, then close it."""
        selected = tuple(name for name, switch in switches.items() if switch.get_active())
        dialog.close()
        if selected == self._entry.copy_root_files:
            return
        self._persist(replace(self._entry, copy_root_files=selected))
        self._rebuild_chips()

    def _on_launch_clicked(self, _button: Gtk.Button) -> None:
        """Launch with the persisted options, including sandbox permissions."""
        if self._inspection is None:
            return
        try:
            from box_gui.gtk.workers import run_launch
        except ImportError as exc:
            self._on_launch_error(exc)
            return
        version = self._entry.preferred_runtime
        sdk = self._entry.preferred_sdk
        copy_root_files = self._entry.copy_root_files
        if self._inspection.game.engine is EngineName.RPG_MAKER_2000_2003:
            sdk = False
            copy_root_files = ()
            if version is None:
                version = self._global_easyrpg_runtime()
        allow_network = self._network_switch.get_active()
        allow_game_writes = self._writes_switch.get_active()
        use_x11 = self._x11_switch.get_active()
        game_path = self.game_path
        if use_x11:
            interaction: Interaction | None = AllowX11Interaction(self._interaction)
        else:
            interaction = self._interaction
        self._set_busy(True, _("Launching {path} …").format(path=game_path))
        run_launch(
            self._paths,
            self._repository,
            game_path,
            interaction,
            self._on_launch_done,
            self._on_launch_error,
            version=version,
            sdk=sdk,
            copy_root_files=copy_root_files,
            allow_network=allow_network,
            allow_game_writes=allow_game_writes,
            x11=use_x11,
        )

    def _on_diagnose_clicked(self, _button: Gtk.Button) -> None:
        """Present per-game diagnostics for the inspected root."""
        dialog = DiagnoseDialog(self._paths, self._repository, self.game_path)
        dialog.present(self)

    def _on_launch_done(self, _code: int) -> None:
        """Reactivate the page after a clean launch exit."""
        self._set_busy(False, "")

    def _on_launch_error(self, error: BaseException) -> None:
        """Show launch failures, offering runtimes for missing runtimes."""
        self._set_busy(False, _("Launch failed."))
        message = str(error) or error.__class__.__name__
        dialog = Adw.AlertDialog(heading=launch_error_heading(error), body=message)
        dialog.add_response("close", _("Close"))
        if isinstance(error, BoxRuntimeError) and self._on_open_runtimes is not None:
            dialog.add_response("open-runtimes", _("Open Runtimes"))
            dialog.set_response_appearance("open-runtimes", Adw.ResponseAppearance.SUGGESTED)
            dialog.connect("response", self._on_launch_error_response)
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present(self)

    def _on_launch_error_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        """Open the runtime manager when requested from a launch failure."""
        if response == "open-runtimes" and self._on_open_runtimes is not None:
            self._on_open_runtimes(self)

    def _show_alert(self, heading: str, body: str) -> None:
        """Present a single-close alert over this page."""
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present(self)
