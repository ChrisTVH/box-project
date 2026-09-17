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
    rekey_cached_icon,
)
from box_gui.core.library import (  # noqa: E402
    LibraryEntry,
    LibraryError,
    LibraryRepository,
    is_ghost,
)
from box_gui.core.sessions import (  # noqa: E402
    is_session_running,
    live_session_names,
)
from box_gui.gtk.icons import FOLDER_ICON_NAME, WARNING_ICON_NAME  # noqa: E402
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

# Seconds between live-session checks while the detail page is visible.
_SESSION_POLL_INTERVAL_S = 5


def _ghost_reason() -> str:
    """Return the shared explanation for disabled ghost actions."""
    return _("The game folder is missing. Use Locate folder… to point at it again.")


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
        self._busy = False
        self._running = False
        self._missing = False
        self._initial_focus_cleared = False
        self._session_poll_id: int | None = None
        self._title_label = Gtk.Label(label=entry.display_name)
        self._launch_button = Gtk.Button.new_from_icon_name("box-rpg-rocket-symbolic")
        self._launch_button.set_tooltip_text(_("Launch"))
        self._launch_button.add_css_class("suggested-action")
        self._launch_button.set_sensitive(False)
        self._launch_button.connect("clicked", self._on_launch_clicked)
        self._stop_button = Gtk.Button.new_from_icon_name("box-rpg-x-symbolic")
        self._stop_button.set_tooltip_text(_("Stop"))
        self._stop_button.set_visible(False)
        self._stop_button.set_sensitive(False)
        self._stop_button.connect("clicked", self._on_stop_clicked)
        self._diagnose_button = Gtk.Button(label=_("Diagnose"))
        self._diagnose_button.set_sensitive(False)
        self._diagnose_button.connect("clicked", self._on_diagnose_clicked)
        self._locate_button = Gtk.Button.new_from_icon_name(FOLDER_ICON_NAME)
        self._locate_button.set_tooltip_text(_("Locate folder…"))
        self._locate_button.set_visible(is_ghost(entry))
        self._locate_button.connect("clicked", self._on_locate_clicked)
        self._locate_file_dialog: Gtk.FileDialog | None = None
        self._spinner = Gtk.Spinner()
        self._status = Gtk.Label(label="")
        self._status.set_xalign(0.0)
        self._status.set_hexpand(True)
        # Ellipsize must stay NONE: any other mode pins the label to one
        # line and silently disables wrapping in GTK4.
        self._status.set_ellipsize(Pango.EllipsizeMode.NONE)
        self._status.set_wrap(True)
        self._status.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
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
        self._ci_mount_row = Adw.SwitchRow(
            title=_("Case-insensitive mount"),
            subtitle=_("Use a case-insensitive view for this game when available."),
        )
        self._ci_mount_row.connect("notify::active", self._on_ci_mount_toggled)
        self._ci_mount_warning: Gtk.Image | None = None
        self._gamemode_row = Adw.SwitchRow(
            title=_("GameMode"),
            subtitle=_("Boost performance with GameMode when available."),
        )
        self._gamemode_row.connect("notify::active", self._on_gamemode_toggled)
        self._gamemode_warning: Gtk.Image | None = None
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
        self._sync_gamemode_availability()
        self._sync_ci_mount_availability()
        self.connect("map", self._on_mapped)
        self.connect("unmap", self._on_unmapped)
        self.connect("destroy", self._on_unmapped)
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
        header.pack_end(self._stop_button)
        header.pack_end(self._locate_button)
        self._header_bar = header
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
        runtime_group.add(self._ci_mount_row)
        runtime_group.add(self._gamemode_row)
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
        """Re-inspect the stored path so the page never shows stale data.

        A missing folder records one advisory sighting first: ghosts
        (and any unreachable folder) show the blocked state with its
        reason instead of a doomed re-inspection and its raw backend
        dialog. Each visit therefore feeds the same missing streak
        the library page maintains.
        """
        present = self._record_sighting()
        if is_ghost(self._entry) or not present:
            self._show_ghost_blocked()
            self._ensure_session_poll()
            return
        try:
            from box_gui.gtk.workers import run_inspect
        except ImportError as exc:
            self._on_inspect_error(exc)
            return
        self._set_busy(True, _("Inspecting {path} …").format(path=self._entry.path))
        self._ensure_session_poll()
        run_inspect(self._paths, self._entry.path, self._on_inspect_done, self._on_inspect_error)

    def _record_sighting(self) -> bool:
        """Persist one advisory missing-folder sighting; True means reachable.

        Refreshes the entry snapshot from the library as a side effect
        and remembers a miss so the buttons stay blocked even below the
        ghost threshold. Library failures degrade to reachable so the
        page falls back to its legacy inspect-and-report behavior
        instead of crashing.
        """
        try:
            self._entry, present = self._library.note_missing_entry(self._entry)
        except LibraryError, OSError:
            return True
        self._missing = not present
        return present

    def _show_ghost_blocked(self) -> None:
        """Show the blocked state for an unreachable folder without inspecting."""
        self._inspection = None
        self._running = False
        self._set_busy(False, _ghost_reason())

    def _update_action_states(self) -> None:
        """Derive button sensitivity, tooltips, and visibility from one state.

        Launch stays sensitive only while an inspection exists without
        busy, blocked, or running states; running shows the explicit
        Stop button instead of repurposing Launch. Blocked covers both
        ghosts and freshly confirmed missing folders: the folder
        shortcut shows with the shared reason while Launch and
        Diagnose stay disabled. Any insensitive Launch button shows
        the rocket-off icon; only an enabled Launch button shows
        the rocket icon.
        """
        ghost = is_ghost(self._entry)
        blocked = ghost or self._missing
        running = self._running and not blocked
        busy = self._busy
        has_inspection = self._inspection is not None
        self._locate_button.set_visible(blocked)
        self._stop_button.set_visible(running)
        self._stop_button.set_sensitive(running and not busy)
        if blocked:
            self._launch_button.set_icon_name("box-rpg-rocket-off-symbolic")
            self._launch_button.remove_css_class("suggested-action")
            self._launch_button.set_sensitive(False)
            self._launch_button.set_tooltip_text(_ghost_reason())
            self._diagnose_button.set_sensitive(False)
            self._diagnose_button.set_tooltip_text(_ghost_reason())
            return
        if running:
            self._launch_button.set_icon_name("box-rpg-rocket-off-symbolic")
            self._launch_button.remove_css_class("suggested-action")
            self._launch_button.set_sensitive(False)
            self._launch_button.set_tooltip_text(_("Game is running"))
            self._diagnose_button.set_sensitive(has_inspection and not busy)
            self._diagnose_button.set_tooltip_text(None)
            return
        sensitive = has_inspection and not busy
        self._launch_button.set_icon_name(
            "box-rpg-rocket-symbolic" if sensitive else "box-rpg-rocket-off-symbolic"
        )
        self._launch_button.add_css_class("suggested-action")
        self._launch_button.set_sensitive(sensitive)
        self._launch_button.set_tooltip_text(_("Launch"))
        self._diagnose_button.set_sensitive(has_inspection and not busy)
        self._diagnose_button.set_tooltip_text(None)

    def _set_busy(self, busy: bool, message: str) -> None:
        """Toggle the spinner and button sensitivity with a status message."""
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._busy = busy
        self._status.set_text(message)
        self._status.set_tooltip_text(message or None)
        self._status_box.set_visible(bool(message))
        self._update_action_states()

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
        self._missing = False
        self._sync_gamemode_availability()
        self._sync_ci_mount_availability()
        self._set_busy(False, "")
        self._sync_running_state()

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
        """List candidate extra-root filenames, tolerating unreadable roots.

        Packed single-executable sources resolve against their unpacked
        profile tree (the tree sessions copy from), not the source folder
        holding only the packed executable. When unpacking fails, fall
        back to the source listing so plain folders still offer files.
        """
        try:
            names = tuple(list_root_files(game, self._paths))
        except BoxError, OSError:
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

    @staticmethod
    def _is_gamemode_available() -> bool:
        """Return True when the backend reports a usable GameMode wrapper.

        Any failure (old backend without the probe, missing gamemoderun,
        unexpected errors) means unavailable, never a crash.
        """
        try:
            from box.api import launch as launch_api
        except ImportError:
            return False
        probe = getattr(launch_api, "is_gamemode_available", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            return False

    def _sync_gamemode_availability(self) -> None:
        """Reflect GameMode support on the switch, warning when unavailable.

        Unavailable forces the switch off and persists use_gamemode=False
        so a stale True can never reach launch; available restores the
        persisted choice and drops the warning icon.
        """
        if self._is_gamemode_available():
            self._clear_gamemode_warning()
            self._gamemode_row.set_sensitive(True)
            if self._gamemode_row.get_active() != self._entry.use_gamemode:
                self._loading = True
                try:
                    self._gamemode_row.set_active(self._entry.use_gamemode)
                finally:
                    self._loading = False
            return
        self._loading = True
        try:
            self._gamemode_row.set_active(False)
        finally:
            self._loading = False
        self._gamemode_row.set_sensitive(False)
        if self._entry.use_gamemode:
            self._persist(replace(self._entry, use_gamemode=False))
        self._ensure_gamemode_warning()

    def _ensure_gamemode_warning(self) -> None:
        """Attach the unavailable-feature warning icon exactly once."""
        if self._gamemode_warning is not None:
            return
        warning = Gtk.Image.new_from_icon_name(WARNING_ICON_NAME)
        warning.set_tooltip_text(_("This feature is not available on your system."))
        self._gamemode_row.add_suffix(warning)
        self._gamemode_warning = warning

    def _clear_gamemode_warning(self) -> None:
        """Detach the warning icon now that GameMode is available."""
        if self._gamemode_warning is None:
            return
        self._gamemode_row.remove(self._gamemode_warning)
        self._gamemode_warning = None

    def _on_gamemode_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        """Persist GameMode picks without touching other fields."""
        if self._loading:
            return
        active = row.get_active()
        if active == self._entry.use_gamemode:
            return
        self._persist(replace(self._entry, use_gamemode=active))

    @staticmethod
    def _is_ci_mount_available() -> bool:
        """Return True when the backend supports a case-insensitive mount.

        Prefers the backend probe when present so libfuse availability is
        checked through the stable surface; otherwise falls back to the
        launch signature and lets the backend fail at launch time. Any
        failure means unavailable, never a crash.
        """
        try:
            from box.api import launch as launch_api
        except ImportError:
            return False
        try:
            probe = getattr(launch_api, "is_ci_mount_available", None)
            if callable(probe):
                return bool(probe())
            import inspect as stdlib_inspect

            return "ci_mount" in stdlib_inspect.signature(launch_api.launch).parameters
        except Exception:
            return False

    def _sync_ci_mount_availability(self) -> None:
        """Reflect case-insensitive mount support on the switch.

        EasyRPG games force the switch off with their own warning since
        the backend rejects the flag for them; an unavailable system
        does the same with the generic warning. Available restores the
        persisted choice and drops the warning icon.
        """
        is_easyrpg = False
        if self._inspection is not None:
            is_easyrpg = self._inspection.game.engine is EngineName.RPG_MAKER_2000_2003
        else:
            is_easyrpg = self._entry.engine == EngineName.RPG_MAKER_2000_2003.value
        if is_easyrpg:
            self._loading = True
            try:
                self._ci_mount_row.set_active(False)
            finally:
                self._loading = False
            self._ci_mount_row.set_sensitive(False)
            if self._entry.use_ci_mount:
                self._persist(replace(self._entry, use_ci_mount=False))
            self._ensure_ci_mount_warning(_("Not available for EasyRPG games."))
            return
        if self._is_ci_mount_available():
            self._clear_ci_mount_warning()
            self._ci_mount_row.set_sensitive(True)
            if self._ci_mount_row.get_active() != self._entry.use_ci_mount:
                self._loading = True
                try:
                    self._ci_mount_row.set_active(self._entry.use_ci_mount)
                finally:
                    self._loading = False
            return
        self._loading = True
        try:
            self._ci_mount_row.set_active(False)
        finally:
            self._loading = False
        self._ci_mount_row.set_sensitive(False)
        if self._entry.use_ci_mount:
            self._persist(replace(self._entry, use_ci_mount=False))
        self._ensure_ci_mount_warning(_("This feature is not available on your system."))

    def _ensure_ci_mount_warning(self, reason: str) -> None:
        """Attach the unavailable-feature warning icon exactly once."""
        if self._ci_mount_warning is not None:
            self._ci_mount_warning.set_tooltip_text(reason)
            return
        warning = Gtk.Image.new_from_icon_name(WARNING_ICON_NAME)
        warning.set_tooltip_text(reason)
        self._ci_mount_row.add_suffix(warning)
        self._ci_mount_warning = warning

    def _clear_ci_mount_warning(self) -> None:
        """Detach the warning icon now that the mount is available."""
        if self._ci_mount_warning is None:
            return
        self._ci_mount_row.remove(self._ci_mount_warning)
        self._ci_mount_warning = None

    def _on_ci_mount_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        """Persist case-insensitive mount picks without touching other fields."""
        if self._loading:
            return
        active = row.get_active()
        if active == self._entry.use_ci_mount:
            return
        self._persist(replace(self._entry, use_ci_mount=active))

    def _sync_running_state(self) -> None:
        """Reflect a live backend session with an explicit Stop button.

        Running disables Launch with the running tooltip and shows the
        separate Stop action; idle restores the Launch affordance.
        Blocked entries (ghosts or freshly confirmed missing folders)
        stay blocked with the shared reason. The status
        label stays reserved for transient busy messages: the Stop
        button and the Launch tooltip already communicate running.
        """
        if is_ghost(self._entry) or self._missing:
            self._running = False
            self._update_action_states()
            return
        self._running = bool(is_session_running(self._paths, self._entry))
        self._update_action_states()

    @staticmethod
    def _session_probe_available() -> bool:
        """Return True when the backend offers the live-session listing."""
        try:
            from box.api import launch as launch_api
        except ImportError:
            return False
        return callable(getattr(launch_api, "find_live_sessions", None))

    def _ensure_session_poll(self) -> None:
        """Poll the single-entry session every few seconds while visible.

        Pages without configured paths have inert probes, so they never
        start the timer.
        """
        if self._session_poll_id is not None:
            return
        if not self._session_probe_available():
            return
        self._session_poll_id = GLib.timeout_add_seconds(
            _SESSION_POLL_INTERVAL_S, self._on_session_poll
        )

    def _stop_session_poll(self) -> None:
        """Drop the live-session poll timer, if one is active."""
        if self._session_poll_id is not None:
            GLib.source_remove(self._session_poll_id)
            self._session_poll_id = None

    def _on_mapped(self, _widget: Gtk.Widget) -> None:
        """Restart polling and resync once the page becomes visible."""
        self._ensure_session_poll()
        self._resync_running_async()
        if not self._initial_focus_cleared:
            self._initial_focus_cleared = True
            GLib.idle_add(self._drop_initial_focus)

    def _drop_initial_focus(self) -> bool:
        """Hand back the autofocus GTK leaves in the display-name field.

        One-shot idle source: opening the page otherwise lands keyboard
        focus (plus a full text selection) in the Name row before the
        user touches anything. Only acts when the focus sits inside
        that row, so an intentional focus elsewhere is never stolen.
        Collapsing the selection hides the leftover highlight; the
        text itself is untouched, so no rename is persisted.
        """
        root = self.get_root()
        focus = root.get_focus() if isinstance(root, Gtk.Window) else None
        if focus is not None and (
            focus is self._display_row or focus.is_ancestor(self._display_row)
        ):
            root.set_focus(None)
            self._display_row.select_region(0, 0)
        return False

    def _on_unmapped(self, _widget: Gtk.Widget) -> None:
        """Stop polling once the page leaves the visible navigation stack."""
        self._stop_session_poll()

    def _resync_running_async(self) -> None:
        """Re-check the session off the main loop; ghosts just re-apply."""
        if is_ghost(self._entry):
            self._running = False
            self._update_action_states()
            return
        if not self._session_probe_available():
            return
        try:
            from box_gui.gtk.workers import run_in_thread
        except ImportError:
            return
        paths = self._paths
        entry = self._entry
        expected = entry.path
        run_in_thread(
            lambda: bool(is_session_running(paths, entry)),
            lambda running: self._on_poll_result(running, expected),
            lambda _error: None,
        )

    def _on_session_poll(self) -> bool:
        """Re-check the session off the main loop; True keeps polling."""
        if not self.get_mapped():
            self._session_poll_id = None
            return False
        if not self._session_probe_available():
            self._session_poll_id = None
            return False
        if is_ghost(self._entry):
            return True
        try:
            from box_gui.gtk.workers import run_in_thread
        except ImportError:
            return True
        paths = self._paths
        entry = self._entry
        expected = entry.path
        run_in_thread(
            lambda: bool(is_session_running(paths, entry)),
            lambda running: self._on_poll_result(running, expected),
            lambda _error: None,
        )
        return True

    def _on_poll_result(self, running: bool, expected: Path) -> None:
        """Apply a poll result for the entry probed, ignoring stale ticks."""
        if self._entry.path != expected:
            return
        if is_ghost(self._entry):
            running = False
        if running == self._running:
            return
        self._running = running
        self._update_action_states()

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
        """Launch with the persisted options, including sandbox permissions.

        Ghost entries stay blocked with their reason, and a click while
        running only resyncs the buttons: stopping needs the explicit
        Stop action, never a surprise kill from Launch. A folder that
        vanished while the page was open records a sighting and shows
        the blocked state instead of launching into a doomed backend.
        """
        if is_ghost(self._entry):
            self._show_alert(_("Folder missing"), _ghost_reason())
            return
        if not self._record_sighting() or is_ghost(self._entry):
            self._show_ghost_blocked()
            return
        if self._inspection is None:
            return
        if self._running or is_session_running(self._paths, self._entry):
            self._running = True
            self._update_action_states()
            return
        try:
            from box_gui.gtk.workers import run_launch
        except ImportError as exc:
            self._on_launch_error(exc)
            return
        version = self._entry.preferred_runtime
        sdk = self._entry.preferred_sdk
        copy_root_files = self._entry.copy_root_files
        ci_mount = bool(getattr(self._entry, "use_ci_mount", False))
        if self._inspection.game.engine is EngineName.RPG_MAKER_2000_2003:
            sdk = False
            copy_root_files = ()
            ci_mount = False
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
            gamemode=self._entry.use_gamemode,
            ci_mount=ci_mount,
        )

    def _on_diagnose_clicked(self, _button: Gtk.Button) -> None:
        """Present per-game diagnostics for the inspected root."""
        if is_ghost(self._entry):
            self._show_alert(_("Folder missing"), _ghost_reason())
            return
        if not self._record_sighting() or is_ghost(self._entry):
            self._show_ghost_blocked()
            return
        dialog = DiagnoseDialog(self._paths, self._repository, self.game_path)
        dialog.present(self)

    def _on_stop_clicked(self, _button: Gtk.Button) -> None:
        """Stop the live session through the explicit Stop action."""
        self._stop_running_session()

    def _stop_running_session(self) -> None:
        """Stop the live backend session for this entry off the main loop."""
        try:
            from box_gui.gtk.workers import run_stop
        except ImportError as exc:
            self._on_stop_error(exc)
            return
        names = live_session_names(self._paths, self._entry)
        if not names:
            # The session exited on its own; just resync instead of erroring.
            self._set_busy(False, "")
            self._sync_running_state()
            return
        game_path = self.game_path
        self._set_busy(True, _("Stopping {path} …").format(path=game_path))
        run_stop(
            self._paths,
            self._entry,
            names[0],
            self._on_stop_done,
            self._on_stop_error,
        )

    def _on_stop_done(self, _result: None) -> None:
        """Restore the launch action after the session stopped."""
        self._set_busy(False, "")
        self._sync_running_state()

    def _on_stop_error(self, error: BaseException) -> None:
        """Show stop failures without leaving the page busy."""
        self._set_busy(False, _("Stop failed."))
        message = str(error) or error.__class__.__name__
        dialog = Adw.AlertDialog(heading=launch_error_heading(error), body=message)
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present(self)

    def _on_locate_clicked(self, _button: Gtk.Button) -> None:
        """Point this entry at a new folder through the same picker as +."""
        dialog = Gtk.FileDialog(title=_("Select Game Folder"))
        self._locate_file_dialog = dialog
        parent = self.get_root()
        dialog.select_folder(
            parent if isinstance(parent, Gtk.Window) else None,
            None,
            self._on_locate_folder_chosen,
        )

    def _on_locate_folder_chosen(self, source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        """Start re-inspection for the folder chosen as the new location."""
        self._locate_file_dialog = None
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
        try:
            from box_gui.gtk.workers import run_inspect
        except ImportError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        run_inspect(
            self._paths, Path(path), self._on_locate_inspect_done, self._on_locate_inspect_error
        )

    def _on_locate_inspect_done(self, inspection: Inspection) -> None:
        """Relocate this entry onto the inspected folder, clearing its ghost streak."""
        new_icon = rekey_cached_icon(
            self._paths.config_root,
            self._entry.icon_path,
            inspection.game.root,
        )
        relocated = replace(
            self._entry,
            path=inspection.game.root,
            engine=inspection.game.engine.value,
            icon_path=new_icon,
            missing_streak=0,
        )
        self._persist(relocated)
        if is_ghost(self._entry):
            self._show_ghost_blocked()
            return
        self._refresh_icon_preview()
        self.refresh()

    def _on_locate_inspect_error(self, error: BaseException) -> None:
        """Show relocation inspection failures; the entry stays a ghost."""
        message = str(error) or error.__class__.__name__
        self._show_alert(inspection_error_heading(error), message)

    def _on_launch_done(self, _result: object) -> None:
        """Reflect the detached session after spawn, not a game exit."""
        self._set_busy(False, "")
        self._sync_running_state()

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
