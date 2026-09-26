"""Library navigation page listing remembered games."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from box.api import AppPaths, ConfigRepository  # noqa: E402
from box.api.inspect import Inspection  # noqa: E402
from box.api.interaction import Interaction  # noqa: E402
from box.errors import BoxError, ConfigurationError  # noqa: E402
from box.models import EngineName  # noqa: E402
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from box_gui.core.app_info import get_app_author, get_app_version  # noqa: E402
from box_gui.core.defaults import DefaultsRepository  # noqa: E402
from box_gui.core.game_icon import rekey_cached_icon  # noqa: E402
from box_gui.core.library import (  # noqa: E402
    LibraryEntry,
    LibraryError,
    LibraryRepository,
    ReorderDirection,
    is_ghost,
)
from box_gui.core.sessions import (  # noqa: E402
    is_session_running,
    live_session_names,
    poll_session_status,
)
from box_gui.gtk.icons import DROP_ICON_NAME, FOLDER_ICON_NAME, HEART_ICON_NAME  # noqa: E402
from box_gui.gtk.interaction import AllowX11Interaction  # noqa: E402
from box_gui.i18n import _, ngettext  # noqa: E402
from box_gui.widgets.external_link import confirm_and_open_external_link  # noqa: E402
from box_gui.widgets.icon_widget import build_game_icon  # noqa: E402

__all__ = ["LibraryPage", "default_display_name", "inspection_error_heading"]

# Short runtime names shown in library pills. Proper names stay untranslated.
_ENGINE_PILL_NAMES: dict[str, str] = {
    "rpg-maker-2000-2003": "EasyRPG",
    "rpg-maker-mv": "NW.js",
    "rpg-maker-mz": "NW.js",
}

_ROW_MENU_ICON = "box-rpg-dots-symbolic"

# Project home opened from the library footer version button.
_PROJECT_URL = "https://gitlab.com/christvh/box-project"

# Seconds between live-session badge refreshes while the page is visible.
_SESSION_POLL_INTERVAL_S = 5

# Row opacity for ghost entries whose folder stayed missing past the threshold.
_GHOST_OPACITY = 0.55


def _ghost_reason() -> str:
    """Return the shared explanation for disabled ghost actions."""
    return _("The game folder is missing. Use Locate folder… to point at it again.")


def _get_heart_icon_name() -> str:
    """Return the vendored heart icon name for the footer credit."""
    return HEART_ICON_NAME


def _get_app_version() -> str:
    """Return the app version with a package fallback for dev checkouts."""
    try:
        app_version = get_app_version()
    except Exception:
        app_version = ""
    if app_version:
        return app_version
    try:
        from box_gui import __version__ as fallback_version
    except ImportError:
        return "0.0.0"
    return fallback_version


def _get_app_author() -> str:
    """Return the app author with a fallback for dev checkouts."""
    try:
        author = get_app_author()
    except Exception:
        author = ""
    if author:
        return author
    return "ChrisTVH"


def _paths_from_drop_value(value: Any) -> list[str | None]:
    """Extract local paths from a drop value without needing a display.

    Accepts a single Gio.File duck-type (get_path), a Gdk.FileList
    duck-type (get_files), or a list/tuple of files. Each entry resolves
    to its local path or None when it cannot be resolved.
    """
    files: list[Any] = []
    if value is None:
        return []
    get_files: Any = getattr(value, "get_files", None)
    if callable(get_files):
        try:
            fetched: Any = get_files()
        except Exception:
            return []
        try:
            files = list(fetched)
        except Exception:
            return []
    elif isinstance(value, (list, tuple)):
        files = list(value)
    elif callable(getattr(value, "get_path", None)):
        files = [value]
    else:
        return []
    paths: list[str | None] = []
    for item in files:
        get_path = getattr(item, "get_path", None)
        if not callable(get_path):
            paths.append(None)
            continue
        try:
            resolved = get_path()
        except Exception:
            paths.append(None)
        else:
            paths.append(resolved if resolved is None or isinstance(resolved, str) else None)
    return paths


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


def _busy_message(count: int) -> str:
    """Return the busy-veil message for the pending inspection count."""
    return ngettext(
        "Adding a game to the library…",
        "Adding {count} games to the library…",
        count,
    ).format(count=count)


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
        on_open_debugging: Callable[[Gtk.Widget], None] | None = None,
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
        self._on_open_debugging = on_open_debugging
        self._paths = paths
        self._repository = repository
        self._defaults_repository = defaults_repository
        self._interaction = interaction
        self._entries: tuple[LibraryEntry, ...] = ()
        self._running: set[Path] = set()
        self._live_sessions: set[Path] = set()
        self._session_poll_id: int | None = None
        self._row_launch_buttons: dict[Path, Gtk.Button] = {}
        self._row_running_badges: dict[Path, Gtk.Label] = {}
        self._locate_entry: LibraryEntry | None = None
        self._locate_dialog: Gtk.FileDialog | None = None
        self._add_button = Gtk.Button.new_from_icon_name("box-rpg-plus-symbolic")
        self._add_button.set_tooltip_text(_("Add game"))
        self._add_button.add_css_class("header-action")
        self._add_button.connect("clicked", self._on_add_clicked)
        self._settings_button = Gtk.Button.new_from_icon_name("box-rpg-settings-symbolic")
        self._settings_button.set_tooltip_text(_("Settings"))
        self._settings_button.add_css_class("header-action")
        self._settings_button.connect("clicked", self._on_settings_clicked)
        self._list_box = Gtk.ListBox()
        self._list_box.add_css_class("boxed-list-separate")
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._hint = Gtk.Label(
            label=_("Your library is empty. Click + or drop a game folder to add your first game.")
        )
        self._hint.set_wrap(True)
        self._hint.add_css_class("dim")
        self._file_dialog: Gtk.FileDialog | None = None
        self._footer: Gtk.Widget | None = None
        self._version_button: Gtk.Button | None = None
        self._debug_button: Gtk.Button | None = None
        self._drop_target: Gtk.DropTarget | None = None
        self._drop_target_list: Gtk.DropTarget | None = None
        self._drop_targets: list[Gtk.DropTarget] = []
        self._drop_enter_count: int = 0
        self._drop_overlay: Gtk.Overlay | None = None
        self._drop_hint: Gtk.Widget | None = None
        self._drop_hint_icon: Gtk.Image | None = None
        self._pending_inspects: int = 0
        self._busy_veil: Gtk.Widget | None = None
        self._busy_spinner: Gtk.Spinner | None = None
        self._busy_label: Gtk.Label | None = None
        self.set_child(self._build_view())
        self.connect("map", self._on_mapped)
        self.connect("unmap", self._on_unmapped)
        self.connect("destroy", self._on_unmapped)
        self.refresh()

    def _build_footer(self) -> Gtk.Widget:
        """Build the fixed footer: debug left, centered credit, version right."""
        version = _get_app_version()
        author = _get_app_author()
        heart_name = _get_heart_icon_name()
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        footer.append(separator)
        bar = Gtk.CenterBox()
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        bar.set_margin_start(12)
        bar.set_margin_end(6)
        # Translators: the credit reads as one sentence
        # "Created with <heart> by {author}"; the heart is an
        # inline icon, so the text stays split across two labels.
        message = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        message.set_halign(Gtk.Align.CENTER)
        message.set_valign(Gtk.Align.CENTER)
        message.append(Gtk.Label(label=_("Created with")))
        heart = Gtk.Image.new_from_icon_name(heart_name)
        heart.set_pixel_size(16)
        heart.add_css_class("love-heart")
        message.append(heart)
        message.append(Gtk.Label(label=_("by {author}").format(author=author)))
        version_button = Gtk.Button(label=f"v{version}")
        version_button.add_css_class("flat")
        version_button.add_css_class("link")
        version_button.set_valign(Gtk.Align.CENTER)
        version_button.set_tooltip_text(_("Open project repository"))
        version_button.connect("clicked", self._on_version_clicked)
        # The debugging switch is hosted in its own window, not in Settings,
        # so the footer is the single entry point to it. It docks left to keep
        # the centered credit in place, and shares the header-action CSS
        # measurements so its icon matches the header buttons.
        debug_button = Gtk.Button.new_from_icon_name("box-rpg-bug-symbolic")
        debug_button.add_css_class("flat")
        debug_button.add_css_class("footer-action")
        debug_button.set_valign(Gtk.Align.CENTER)
        debug_button.set_tooltip_text(_("Debugging"))
        debug_button.connect("clicked", self._on_debug_clicked)
        bar.set_start_widget(debug_button)
        bar.set_center_widget(message)
        bar.set_end_widget(version_button)
        footer.append(bar)
        self._footer = footer
        self._version_button = version_button
        self._debug_button = debug_button
        return footer

    def _show_drop_hint(self) -> None:
        """Show the drag-over hint when a drag enters the library view."""
        hint = self._drop_hint
        if hint is None:
            return
        try:
            hint.set_visible(True)
        except Exception:
            return

    def _hide_drop_hint(self) -> None:
        """Hide the drag-over hint, tolerating headless test setups."""
        hint = self._drop_hint
        if hint is None:
            return
        try:
            hint.set_visible(False)
        except Exception:
            return

    def _drop_accepts_files(self, target: Gtk.DropTarget | None) -> bool:
        """Return True when the dragged formats can carry game folders.

        Unknown or headless drag targets default to True so unit tests
        calling the handlers directly keep showing the hint; only an
        explicit formats object lacking both file types declines.
        """
        try:
            drop = target.get_drop() if target is not None else None
        except Exception:
            return True
        if drop is None:
            return True
        try:
            formats = drop.get_formats()
        except Exception:
            return True
        if formats is None:
            return True
        contain = getattr(formats, "contain_gtype", None)
        if not callable(contain):
            return True
        try:
            if bool(contain(Gio.File)):
                return True
        except Exception:
            return True
        try:
            from gi.repository import Gdk
        except ImportError:
            return True
        file_list_type = getattr(Gdk, "FileList", None)
        if file_list_type is None:
            return False
        try:
            return bool(contain(file_list_type))
        except Exception:
            return True

    def _on_drop_enter(self, target: Gtk.DropTarget, _x: float, _y: float) -> int:
        """Show the hint when a file drag enters, keeping the COPY action."""
        if not self._drop_accepts_files(target):
            return 0
        self._drop_enter_count += 1
        self._show_drop_hint()
        try:
            from gi.repository import Gdk
        except ImportError:
            return 0
        try:
            return int(Gdk.DragAction.COPY)
        except Exception:
            return 0

    def _on_drop_motion(self, target: Gtk.DropTarget, _x: float, _y: float) -> int:
        """Keep the hint visible while a file drag moves over the view."""
        if not self._drop_accepts_files(target):
            return 0
        if self._drop_enter_count <= 0:
            self._drop_enter_count = 1
        self._show_drop_hint()
        try:
            from gi.repository import Gdk
        except ImportError:
            return 0
        try:
            return int(Gdk.DragAction.COPY)
        except Exception:
            return 0

    def _on_drop_leave(self, _target: Gtk.DropTarget) -> None:
        """Hide the hint once all tracked drags leave the view."""
        self._drop_enter_count = max(0, self._drop_enter_count - 1)
        if self._drop_enter_count == 0:
            self._hide_drop_hint()
        return None

    def _attach_drop_target(self, widget: Gtk.Widget) -> None:
        """Attach file drop targets, tolerating a missing Gdk in tests."""
        try:
            from gi.repository import Gdk
        except ImportError:
            return None
        try:
            file_target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        except Exception:
            return None
        file_target.connect("drop", self._on_drop)
        try:
            file_target.connect("enter", self._on_drop_enter)
            file_target.connect("motion", self._on_drop_motion)
            file_target.connect("leave", self._on_drop_leave)
        except Exception:
            pass
        widget.add_controller(file_target)
        self._drop_target = file_target
        self._drop_targets.append(file_target)
        try:
            file_list_type = getattr(Gdk, "FileList", None)
            if file_list_type is not None:
                list_target = Gtk.DropTarget.new(file_list_type, Gdk.DragAction.COPY)
                list_target.connect("drop", self._on_drop)
                try:
                    list_target.connect("enter", self._on_drop_enter)
                    list_target.connect("motion", self._on_drop_motion)
                    list_target.connect("leave", self._on_drop_leave)
                except Exception:
                    pass
                widget.add_controller(list_target)
                self._drop_target_list = list_target
                self._drop_targets.append(list_target)
        except Exception:
            pass
        return None

    def _on_drop(self, _target: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
        """Add dropped game folders through the shared inspect flow."""
        self._drop_enter_count = 0
        self._hide_drop_hint()
        paths = _paths_from_drop_value(value)
        if not paths:
            return False
        if any(path is None for path in paths):
            self._show_alert(
                _("Unexpected Error"),
                _("Cannot resolve a local path for the selection."),
            )
        valid = [path for path in paths if path is not None]
        if not valid:
            return True
        if len(paths) == 1:
            self.inspect_and_add(Path(valid[0]), open_detail=True)
        else:
            for path in valid:
                self.inspect_and_add(Path(path), open_detail=False)
        return True

    def _on_version_clicked(self, _button: Gtk.Button) -> None:
        """Confirm, then open the project repository URL."""
        confirm_and_open_external_link(self, _PROJECT_URL)

    def _build_drop_hint(self) -> Gtk.Widget:
        """Build the hidden drag-over veil with its centered hint content."""
        veil = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        veil.set_halign(Gtk.Align.FILL)
        veil.set_valign(Gtk.Align.FILL)
        veil.set_hexpand(True)
        veil.set_vexpand(True)
        veil.add_css_class("drop-hint-veil")
        veil.set_can_target(False)
        veil.set_visible(False)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.set_halign(Gtk.Align.CENTER)
        inner.set_valign(Gtk.Align.CENTER)
        inner.set_can_target(False)
        icon = Gtk.Image.new_from_icon_name(DROP_ICON_NAME)
        icon.set_pixel_size(64)
        icon.add_css_class("drop-hint-icon")
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_can_target(False)
        title = Gtk.Label(label=_("Drop your game folder to add it to the library."))
        title.set_wrap(True)
        title.set_halign(Gtk.Align.CENTER)
        title.set_can_target(False)
        inner.append(icon)
        inner.append(title)
        veil.append(inner)
        self._drop_hint = veil
        self._drop_hint_icon = icon
        return veil

    def _build_busy_veil(self) -> Gtk.Widget:
        """Build the hidden busy veil with a spinner and a pending-count message.

        It reuses the drop-hint veil class so the dimming is visually
        identical, and never targets events, so the drop controllers under
        the overlay keep working while an inspection runs.
        """
        veil = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        veil.set_halign(Gtk.Align.FILL)
        veil.set_valign(Gtk.Align.FILL)
        veil.set_hexpand(True)
        veil.set_vexpand(True)
        veil.add_css_class("drop-hint-veil")
        veil.set_can_target(False)
        veil.set_visible(False)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.set_halign(Gtk.Align.CENTER)
        inner.set_valign(Gtk.Align.CENTER)
        inner.set_can_target(False)
        spinner = Gtk.Spinner()
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_can_target(False)
        # The veil is always shown with a pending inspection, so seed the
        # label with the singular message: the plural form would render
        # "Adding 0 games to the library…" until the first _set_busy call.
        message = Gtk.Label(label=_busy_message(1))
        message.set_wrap(True)
        message.set_halign(Gtk.Align.CENTER)
        message.set_can_target(False)
        inner.append(spinner)
        inner.append(message)
        veil.append(inner)
        self._busy_veil = veil
        self._busy_spinner = spinner
        self._busy_label = message
        return veil

    def _set_busy(self, busy: bool) -> None:
        """Dim the page while at least one inspection is in flight.

        The veil only covers the page content: the header bar and the
        footer version and debug buttons live outside the overlay, so
        they are dimmed by toggling their sensitivity, as are the
        library rows.
        The message always names the pending count, so a multi-folder
        drop reports how much is left.
        """
        veil = self._busy_veil
        if veil is not None:
            veil.set_visible(busy)
        spinner = self._busy_spinner
        if spinner is not None:
            if busy:
                spinner.start()
            else:
                spinner.stop()
        label = self._busy_label
        if label is not None:
            # The message names a game being added, so the plural form only
            # applies above one: a settled counter must never leave the
            # nonsensical "Adding 0 games" behind on a hidden veil.
            label.set_text(_busy_message(max(1, self._pending_inspects)))
        self._add_button.set_sensitive(not busy)
        self._settings_button.set_sensitive(not busy)
        if self._version_button is not None:
            self._version_button.set_sensitive(not busy)
        if self._debug_button is not None:
            self._debug_button.set_sensitive(not busy)
        self._list_box.set_sensitive(not busy)

    def _begin_inspect(self) -> None:
        """Count a dispatched inspection and show the busy state."""
        self._pending_inspects += 1
        self._set_busy(True)

    def _end_inspect(self) -> None:
        """Drop a settled inspection, hiding the veil once none are left."""
        self._pending_inspects = max(0, self._pending_inspects - 1)
        self._set_busy(self._pending_inspects > 0)

    def _build_view(self) -> Adw.ToolbarView:
        """Assemble the header bar, game list, footer, and drop target."""
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
        overlay = Gtk.Overlay()
        overlay.set_child(scrolled)
        overlay.add_overlay(self._build_drop_hint())
        overlay.add_overlay(self._build_busy_veil())
        self._drop_overlay = overlay
        self._attach_drop_target(overlay)
        # Keep one file target on the scrolled window so the legacy
        # drop-target lookup still finds a controller under the page.
        self._attach_drop_target(scrolled)
        view.set_content(overlay)
        view.add_bottom_bar(self._build_footer())
        return view

    def refresh(self) -> None:
        """Reload rows from the repository, alerting on a corrupt file.

        Missing game directories only feed the advisory missing streak:
        below the threshold rows render normally and silently, at or
        above it they render as ghosts. Only the library remembers the
        outcome, game files are never touched and nothing is deleted.
        """
        try:
            entries = self._library.note_missing_presentation_state()
        except LibraryError as exc:
            self._show_alert(_("Library Error"), str(exc) or exc.__class__.__name__)
            return
        self._render_rows(entries)
        self._refresh_running_now()
        self._ensure_session_poll()

    def _render_rows(self, entries: tuple[LibraryEntry, ...]) -> None:
        """Rebuild the visible rows for already-loaded entries."""
        self._entries = entries
        self._row_launch_buttons = {}
        self._row_running_badges = {}
        known = {entry.path for entry in entries}
        self._live_sessions = {path for path in self._live_sessions if path in known}
        self._hint.set_visible(len(entries) == 0)
        child = self._list_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._list_box.remove(child)
            child = following
        for entry in entries:
            self._list_box.append(self._build_row(entry, show_reorder=len(entries) > 1))

    def inspect_and_add(self, path: Path, *, open_detail: bool = True) -> None:
        """Inspect a picked folder, add it, then open its detail page.

        A newly added game pushes straight into its detail page so the
        user lands where display name, runtime, and files are configured.
        Multi-drops pass ``open_detail=False`` so only the list refreshes
        instead of stacking one detail page per folder.

        The busy state is counted, not toggled: a multi-folder drop
        dispatches one inspection per path, so the veil stays up until
        the last callback settles. Both early returns above happen before
        ``_begin_inspect``, and a dispatch that raises synchronously
        settles the count again below, so the counter cannot leak.
        """
        try:
            from box_gui.gtk.workers import run_inspect
        except ImportError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        paths = self._paths
        if paths is None:
            return

        def _on_done(inspection: Inspection) -> None:
            self._end_inspect()
            self._on_inspect_done(inspection, open_detail=open_detail)

        def _on_error(error: BaseException) -> None:
            self._end_inspect()
            self._on_inspect_error(error)

        self._begin_inspect()
        try:
            run_inspect(paths, path, _on_done, _on_error)
        except Exception as exc:
            # Dispatch can fail before any callback is scheduled (a worker
            # thread that cannot start, or an interpreter shutting down).
            # Settle the count here or the veil would never come down.
            self._end_inspect()
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)

    def _build_row(self, entry: LibraryEntry, *, show_reorder: bool = True) -> Adw.ActionRow:
        """Build one title-only row with an icon, pills, menu, and launch button.

        Ghost entries (missing past the threshold) render dimmed with a
        Missing badge, an explanatory tooltip, a sensitive folder button
        shortcutting to the locate flow, and an insensitive launch
        button showing the rocket-off icon. Running entries carry a
        Running badge with Launch disabled and the rocket-off icon.
        Normal rows keep no subtitle and no tooltip.
        """
        # ActionRow renders the title as Pango markup, so escape display
        # names like "Fear & Hunger".
        row = Adw.ActionRow(title=GLib.markup_escape_text(entry.display_name, -1))
        row.set_activatable(True)
        ghost = is_ghost(entry)
        running = not ghost and self._is_entry_running(entry)
        if ghost:
            row.set_opacity(_GHOST_OPACITY)
            row.add_css_class("ghost-row")
            row.set_tooltip_text(_ghost_reason())
        icon = build_game_icon(entry)
        if icon is not None:
            row.add_prefix(icon)
        pill = Gtk.Label(label=_pill_text(entry))
        pill.add_css_class("caption")
        pill.add_css_class("dim")
        pill.add_css_class("runtime-pill")
        pill.set_valign(Gtk.Align.CENTER)
        row.add_suffix(pill)
        if ghost:
            missing_badge = Gtk.Label(label=_("Missing"))
            missing_badge.add_css_class("caption")
            missing_badge.add_css_class("missing-pill")
            missing_badge.set_valign(Gtk.Align.CENTER)
            row.add_suffix(missing_badge)
        running_badge = Gtk.Label(label=_("Running"))
        running_badge.add_css_class("caption")
        running_badge.add_css_class("running-pill")
        running_badge.set_valign(Gtk.Align.CENTER)
        running_badge.set_visible(running)
        row.add_suffix(running_badge)
        self._row_running_badges[entry.path] = running_badge
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
        launch_button.set_valign(Gtk.Align.CENTER)
        launch_button.add_css_class("flat")
        # The button consumes its own clicks, so pressing it never
        # activates the row underneath.
        if ghost:
            launch_button.set_icon_name("box-rpg-rocket-off-symbolic")
            launch_button.set_sensitive(False)
            launch_button.set_tooltip_text(_ghost_reason())
        elif self._paths is None or self._repository is None:
            launch_button.set_icon_name("box-rpg-rocket-off-symbolic")
            launch_button.set_sensitive(False)
            launch_button.set_tooltip_text(_("Launch"))
        elif running:
            launch_button.set_icon_name("box-rpg-rocket-off-symbolic")
            launch_button.set_sensitive(False)
            launch_button.set_tooltip_text(_("Game is running"))
        else:
            launch_button.set_tooltip_text(_("Launch"))
            launch_button.connect("clicked", self._on_launch_clicked, entry)
        self._row_launch_buttons[entry.path] = launch_button
        row.add_suffix(reorder_menu)
        if ghost:
            # Shortcut to the same picker as the row menu Locate action,
            # so ghosts offer relocation without opening the menu first.
            locate_button = Gtk.Button.new_from_icon_name(FOLDER_ICON_NAME)
            locate_button.set_valign(Gtk.Align.CENTER)
            locate_button.add_css_class("flat")
            locate_button.set_tooltip_text(_("Locate folder…"))
            locate_button.connect("clicked", self._on_locate_clicked, entry)
            row.add_suffix(locate_button)
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

    def _is_entry_running(self, entry: LibraryEntry) -> bool:
        """Return True when one entry has a live session, probed or launched here."""
        if entry.path in self._running or entry.path in self._live_sessions:
            return True
        return is_session_running(self._paths, entry)

    @staticmethod
    def _session_probe_available() -> bool:
        """Return True when the backend offers the live-session listing.

        The poll tick consumes the listing plus the status poll; other
        single-key probes are still tried directly per entry.
        """
        try:
            from box.api import launch as launch_api
        except ImportError:
            return False
        return callable(getattr(launch_api, "find_live_sessions", None))

    def _refresh_running_now(self) -> None:
        """Sync Running badges once from direct advisory probes at load."""
        states = {entry.path: self._is_entry_running(entry) for entry in self._entries}
        self._apply_running_states(states)

    def _apply_running_states(self, states: dict[Path, bool]) -> None:
        """Update Running badges and launch buttons in place on the main loop.

        States merge into the known live set so single-path updates (a
        launch starting or finishing) never wipe the other rows; render
        prunes entries that left the library. Disabled launch buttons
        show the rocket-off icon, enabled ones the rocket icon.
        """
        for path, running in states.items():
            if running:
                self._live_sessions.add(path)
            else:
                self._live_sessions.discard(path)
        by_path = {entry.path: entry for entry in self._entries}
        for path, running in states.items():
            entry = by_path.get(path)
            button = self._row_launch_buttons.get(path)
            badge = self._row_running_badges.get(path)
            if entry is None or button is None or badge is None:
                continue
            if is_ghost(entry):
                continue
            badge.set_visible(running)
            if self._paths is None or self._repository is None:
                continue
            if running or path in self._running:
                button.set_icon_name("box-rpg-rocket-off-symbolic")
                button.set_sensitive(False)
                button.set_tooltip_text(_("Game is running"))
            else:
                button.set_icon_name("box-rpg-rocket-symbolic")
                button.set_sensitive(True)
                button.set_tooltip_text(_("Launch"))

    def _ensure_session_poll(self) -> None:
        """Poll live sessions every few seconds while the page is visible.

        Pages without configured paths have inert probes (no backend
        session can exist for them), so they never start the timer.
        """
        if self._session_poll_id is not None:
            return
        if self._paths is None:
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
        """Restart live-session polling once the page is visible again."""
        self._ensure_session_poll()

    def _on_unmapped(self, _widget: Gtk.Widget) -> None:
        """Stop polling once the page leaves the visible navigation stack."""
        self._stop_session_poll()

    def _on_session_poll(self) -> bool:
        """Re-check live sessions off the main loop; True keeps polling."""
        if not self.get_mapped():
            self._session_poll_id = None
            return False
        if not self._session_probe_available():
            self._session_poll_id = None
            return False
        entries = list(self._entries)
        if not entries:
            return True
        try:
            from box_gui.gtk.workers import run_in_thread
        except ImportError:
            return True
        paths = self._paths
        run_in_thread(
            lambda: {entry.path: self._poll_entry_live(paths, entry) for entry in entries},
            self._apply_running_states,
            lambda _error: None,
        )
        return True

    @staticmethod
    def _poll_entry_live(paths: AppPaths | None, entry: LibraryEntry) -> bool:
        """Return True while any backend session for one entry stays live.

        Runs on a worker thread: the listing filters supervisors in
        post-exit grace, and the status poll reaps freshly exited ones so
        their badges clear promptly.
        """
        names = live_session_names(paths, entry)
        return any(poll_session_status(paths, entry, name) is None for name in names)

    def _on_launch_clicked(self, button: Gtk.Button, entry: LibraryEntry) -> None:
        """Launch one entry directly with its persisted options."""
        if self._paths is None or self._repository is None:
            return
        paths = self._paths
        fresh = self._fresh_entry(entry)
        if is_ghost(fresh):
            self._show_alert(_("Folder missing"), _ghost_reason())
            return
        if self._is_entry_running(fresh):
            # The Running badge and button state already communicate this;
            # silently resync them in case the probe raced the poll tick.
            self._refresh_running_now()
            return
        try:
            from box_gui.gtk.workers import run_inspect
        except ImportError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        button.set_icon_name("box-rpg-rocket-off-symbolic")
        button.set_sensitive(False)
        run_inspect(
            paths,
            fresh.path,
            lambda inspection: self._on_launch_inspect_done(inspection, fresh, button),
            lambda error: self._on_launch_inspect_error(error, button),
        )

    def _on_launch_inspect_done(
        self, inspection: Inspection, entry: LibraryEntry, button: Gtk.Button
    ) -> None:
        """Start the launch once the pre-launch inspection succeeds."""
        try:
            from box_gui.gtk.workers import run_launch
        except ImportError as exc:
            button.set_icon_name("box-rpg-rocket-symbolic")
            button.set_sensitive(True)
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        paths = self._paths
        repository = self._repository
        if paths is None or repository is None:
            button.set_icon_name("box-rpg-rocket-symbolic")
            button.set_sensitive(True)
            return
        fresh = self._fresh_entry(entry)
        version = fresh.preferred_runtime
        sdk = fresh.preferred_sdk
        copy_root_files = fresh.copy_root_files
        allow_network = fresh.allow_network
        allow_game_writes = fresh.allow_game_writes
        use_x11 = fresh.allow_x11
        gamemode = fresh.use_gamemode
        ci_mount = getattr(fresh, "use_ci_mount", False)
        if inspection.game.engine is EngineName.RPG_MAKER_2000_2003:
            sdk = False
            copy_root_files = ()
            ci_mount = False
            if version is None:
                version = self._global_easyrpg_runtime()
        interaction = self._interaction
        if use_x11:
            interaction = AllowX11Interaction(self._interaction)
        self._running.add(fresh.path)
        self._apply_running_states({fresh.path: True})
        run_launch(
            paths,
            repository,
            inspection.game.root,
            interaction,
            lambda code: self._on_launch_done(button, code, fresh.path),
            lambda error: self._on_launch_error(error, button, fresh.path),
            version=version,
            sdk=sdk,
            copy_root_files=copy_root_files,
            allow_network=allow_network,
            allow_game_writes=allow_game_writes,
            x11=use_x11,
            gamemode=gamemode,
            ci_mount=ci_mount,
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
        button.set_icon_name("box-rpg-rocket-symbolic")
        button.set_sensitive(True)
        message = str(error) or error.__class__.__name__
        self._show_alert(inspection_error_heading(error), message)

    def _on_launch_done(
        self, button: Gtk.Button, _result: object, game_path: Path | None = None
    ) -> None:
        """Reflect the detached session after spawn, keeping Running visible."""
        if game_path is None:
            button.set_icon_name("box-rpg-rocket-symbolic")
            button.set_sensitive(True)
            return
        self._running.discard(game_path)
        still_running = self._is_entry_running_by_path(game_path)
        self._apply_running_states({game_path: still_running})
        if not still_running:
            button.set_icon_name("box-rpg-rocket-symbolic")
            button.set_sensitive(True)

    def _is_entry_running_by_path(self, game_path: Path) -> bool:
        """Probe one path without an entry at hand, tolerating old backends."""
        for entry in self._entries:
            if entry.path == game_path:
                return is_session_running(self._paths, entry)
        return False

    def _on_launch_error(
        self, error: BaseException, button: Gtk.Button, game_path: Path | None = None
    ) -> None:
        """Show quick-launch failures and reactivate the launch button."""
        still_running = False
        if game_path is not None:
            self._running.discard(game_path)
            still_running = self._is_entry_running_by_path(game_path)
            self._apply_running_states({game_path: still_running})
        if not still_running:
            button.set_icon_name("box-rpg-rocket-symbolic")
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
        """Remove one entry, then refresh the visible list.

        The entry's exact allowed game root is unregistered as well, so a
        removed game does not keep a stale authorization behind. Only that
        exact root is touched: unrelated roots (which CLI users may rely
        on) are never pruned or cleared. A failing unregistration is
        reported but never rolls back the library removal.
        """
        try:
            self._library.remove(entry)
        except LibraryError as exc:
            self._show_alert(_("Library Error"), str(exc) or exc.__class__.__name__)
            return
        except OSError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        repository = self._repository
        if repository is not None:
            remove_allowed_root = getattr(repository, "remove_allowed_root", None)
            if remove_allowed_root is not None:
                try:
                    remove_allowed_root(entry.path)
                except ConfigurationError as exc:
                    self._show_alert(_("Configuration Error"), str(exc) or exc.__class__.__name__)
                except OSError as exc:
                    self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
        self.refresh()

    def _on_locate_clicked(self, _button: Gtk.Button, entry: LibraryEntry) -> None:
        """Point one ghost entry at a new folder via its row shortcut button."""
        self._begin_locate(entry)

    def _begin_locate(self, entry: LibraryEntry) -> None:
        """Open the folder picker to relocate one entry."""
        dialog = Gtk.FileDialog(title=_("Select Game Folder"))
        self._locate_dialog = dialog
        self._locate_entry = entry
        parent = self.get_root()
        dialog.select_folder(
            parent if isinstance(parent, Gtk.Window) else None,
            None,
            self._on_locate_folder_chosen,
        )

    def _on_locate_folder_chosen(self, source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        """Start re-inspection for the folder chosen as the new location."""
        entry = self._locate_entry
        self._locate_entry = None
        self._locate_dialog = None
        if entry is None:
            return
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
        paths = self._paths
        if paths is None:
            return
        run_inspect(
            paths,
            Path(path),
            lambda inspection: self._on_locate_inspect_done(inspection, entry),
            self._on_locate_inspect_error,
        )

    def _on_locate_inspect_done(self, inspection: Inspection, old_entry: LibraryEntry) -> None:
        """Relocate one entry onto the inspected folder, clearing its ghost streak."""
        new_icon = rekey_cached_icon(
            self._library.library_file.parent,
            old_entry.icon_path,
            inspection.game.root,
        )
        relocated = replace(
            old_entry,
            path=inspection.game.root,
            engine=inspection.game.engine.value,
            icon_path=new_icon,
            missing_streak=0,
        )
        try:
            self._library.update(relocated)
        except LibraryError as exc:
            self._show_alert(_("Library Error"), str(exc) or exc.__class__.__name__)
            return
        except OSError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)
            return
        self.refresh()

    def _on_locate_inspect_error(self, error: BaseException) -> None:
        """Show relocation inspection failures; the entry stays a ghost."""
        message = str(error) or error.__class__.__name__
        self._show_alert(inspection_error_heading(error), message)

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

    def _on_inspect_done(self, inspection: Inspection, *, open_detail: bool = True) -> None:
        """Add the inspected game, then open its detail page unless suppressed.

        The new folder is also registered as an allowed game root, so the
        first launch does not ask the user to authorize a root they just
        pointed at. A failure there is reported but never rolled back:
        the game stays in the library and only the later launch may need
        consent.
        """
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
        self._register_allowed_root(inspection.game.root)
        self.refresh()
        if open_detail and self._on_open_game is not None:
            self._on_open_game(created)

    def _register_allowed_root(self, root: Path) -> None:
        """Remember one freshly added game root as an allowed game root.

        Root resolution, traversal rejection, and the file format stay
        with the backend: this only calls the blessed ``add_allowed_root``
        API, never the internal verbatim-consent path, and pages built
        without a configuration repository silently skip it.

        Security trade-off: dropping or picking a folder now authorizes
        that root without a consent dialog. That is intentional, because
        the user explicitly pointed at the folder, and it is the same
        folder the backend will validate again on every launch. A
        failing write (unreadable or unwritable config) is reported and
        then ignored: the game is already in the library, so removing it
        would lose more than the missing authorization.
        """
        repository = self._repository
        if repository is None:
            return
        try:
            repository.add_allowed_root(root)
        except ConfigurationError as exc:
            self._show_alert(_("Configuration Error"), str(exc) or exc.__class__.__name__)
        except OSError as exc:
            self._show_alert(_("Unexpected Error"), str(exc) or exc.__class__.__name__)

    def _on_inspect_error(self, error: BaseException) -> None:
        """Show inspection failures with an Adw.AlertDialog."""
        message = str(error) or error.__class__.__name__
        self._show_alert(inspection_error_heading(error), message)

    def _on_settings_clicked(self, _button: Gtk.Button) -> None:
        """Present the Settings dialog through the wired callback."""
        if self._on_open_settings is not None:
            self._on_open_settings(self)

    def _on_debug_clicked(self, _button: Gtk.Button) -> None:
        """Present the Debugging window through the wired callback."""
        if self._on_open_debugging is not None:
            self._on_open_debugging(self)

    def _show_alert(self, heading: str, body: str) -> None:
        """Present a single-close alert over this page."""
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present(self)
