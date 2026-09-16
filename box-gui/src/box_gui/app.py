"""GTK application entry point for Box RPG Maker."""

from __future__ import annotations

import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from box.api import AppPaths, ConfigRepository  # noqa: E402
from box.api.interaction import Interaction  # noqa: E402
from box.errors import ConfigurationError  # noqa: E402
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from box_gui.core.defaults import DefaultsRepository  # noqa: E402
from box_gui.core.library import LibraryEntry, LibraryRepository  # noqa: E402
from box_gui.i18n import _  # noqa: E402
from box_gui.i18n import configure as configure_app_translations  # noqa: E402
from box_gui.pages.game_detail_page import GameDetailPage  # noqa: E402
from box_gui.pages.library_page import LibraryPage  # noqa: E402

try:
    from box_gui.gtk.interaction import GtkInteraction
except ImportError:
    GtkInteraction = None  # type: ignore[assignment,misc]

__all__ = ["BoxRpgApplication", "main"]


def _configure_translations() -> None:
    """Load frontend and backend catalogs from the locale environment."""
    configure_app_translations()
    try:
        from box.utils.i18n import configure as configure_box_translations
    except ImportError:
        return
    configure_box_translations()


def _register_bundled_icons() -> None:
    """Add the checkout res/icons dir to the icon theme search path."""
    icons_dir = Path(__file__).resolve().parent.parent.parent / "res" / "icons"
    if not icons_dir.is_dir():
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    Gtk.IconTheme.get_for_display(display).add_search_path(str(icons_dir))


class BoxRpgApplication(Adw.Application):
    """GTK application presenting the library navigation view."""

    def __init__(self) -> None:
        super().__init__(
            application_id="io.gitlab.christvh.BoxRpgApp",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        _configure_translations()
        self._paths: AppPaths | None = None
        self._repository: ConfigRepository | None = None
        self._library: LibraryRepository | None = None
        self._defaults_repository: DefaultsRepository | None = None
        self._interaction: Interaction | None = None
        self._startup_error: ConfigurationError | None = None
        self._navigation: Adw.NavigationView | None = None
        self._css_provider: Gtk.CssProvider | None = None
        try:
            paths = AppPaths.from_environment()
            paths.ensure()
            self._paths = paths
            self._repository = ConfigRepository(paths)
            self._library = LibraryRepository(paths)
            self._defaults_repository = DefaultsRepository(paths)
            if GtkInteraction is not None:
                self._interaction = GtkInteraction(parent=None)
        except ConfigurationError as exc:
            self._startup_error = exc

    def _ensure_runtime_pill_style(self) -> None:
        """Load the theme-aware runtime pill background once per display."""
        if self._css_provider is None:
            provider = Gtk.CssProvider()
            provider.load_from_data(
                b".runtime-pill { background-color: alpha(currentColor, 0.07); "
                b"border-radius: 20px; padding: 5px 13px; } "
                b".missing-pill { background-color: alpha(@error_bg_color, 0.35); "
                b"border-radius: 20px; padding: 5px 13px; } "
                b".running-pill { background-color: alpha(@success_bg_color, 0.35); "
                b"border-radius: 20px; padding: 5px 13px; } "
                b".chip-remove { min-height: 22px; min-width: 22px; "
                b"padding: 0; border-radius: 9999px; } "
                b".chip-flow > flowboxchild:hover, "
                b".chip-flow > flowboxchild:active { background-color: transparent; }"
            )
            self._css_provider = provider
        display = Gdk.Display.get_default()
        if display is not None and self._css_provider is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                self._css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def do_activate(self) -> None:
        """Present the main window, creating it on first activation."""
        self._ensure_runtime_pill_style()
        _register_bundled_icons()
        startup_error = self._startup_error
        if startup_error is not None:
            window = self.props.active_window
            if window is None:
                window = Adw.ApplicationWindow(application=self, title=_("Box RPG Maker"))
                window.present()
            message = str(startup_error) or startup_error.__class__.__name__
            dialog = Adw.AlertDialog(heading=_("Configuration Error"), body=message)
            dialog.add_response("close", _("Close"))
            dialog.set_default_response("close")
            dialog.set_close_response("close")
            quit_once = False

            def _quit(_source: Adw.AlertDialog, *_args: object) -> None:
                nonlocal quit_once
                if not quit_once:
                    quit_once = True
                    self.quit()

            dialog.connect("response", _quit)
            dialog.connect("closed", _quit)
            dialog.present(window)
            return
        window = self.props.active_window
        if window is None:
            paths = self._paths
            repository = self._repository
            library = self._library
            interaction = self._interaction
            if paths is None or repository is None or library is None:
                self.quit()
                return
            window = Adw.ApplicationWindow(
                application=self,
                title=_("Box RPG Maker"),
                default_width=640,
                default_height=480,
            )
            navigation = Adw.NavigationView()
            navigation.connect("popped", self._refresh_library_on_return)
            library_page = LibraryPage(
                library=library,
                on_open_game=self._open_game,
                on_open_settings=self.present_settings,
                paths=paths,
                repository=repository,
                interaction=interaction,
                defaults_repository=self._defaults_repository,
            )
            navigation.push(library_page)
            window.set_content(navigation)
            self._navigation = navigation
            setter = getattr(interaction, "set_parent", None)
            if callable(setter):
                setter(window)
        window.present()

    @staticmethod
    def _refresh_library_on_return(
        navigation: Adw.NavigationView, _popped: Adw.NavigationPage
    ) -> None:
        """Reload the library list when navigating back to it."""
        visible = navigation.get_visible_page()
        if isinstance(visible, LibraryPage):
            visible.refresh()

    def _open_game(self, entry: LibraryEntry) -> None:
        """Push the detail page for one library entry, if possible."""
        navigation = self._navigation
        paths = self._paths
        repository = self._repository
        library = self._library
        if navigation is None or paths is None or repository is None or library is None:
            return
        page = GameDetailPage(
            entry=entry,
            paths=paths,
            repository=repository,
            library=library,
            interaction=self._interaction,
            on_open_runtimes=self.present_settings,
        )
        navigation.push(page)

    def present_settings(self, parent: Gtk.Widget) -> None:
        """Present the Settings dialog for global configuration.

        The dialog is imported lazily so startup never depends on it. The
        shared GtkInteraction is passed through so adding a game root can
        confirm via the same dialogs as launch flows.
        """
        if self._paths is None or self._repository is None:
            return
        try:
            from box_gui.pages.settings_dialog import SettingsDialog
        except ImportError:
            return
        dialog = SettingsDialog(self._paths, self._repository, self._interaction, self._library)
        dialog.present(parent)


def main(argv: list[str] | None = None) -> None:
    """Run the GTK application."""
    app = BoxRpgApplication()
    raise SystemExit(app.run(argv if argv is not None else sys.argv))
