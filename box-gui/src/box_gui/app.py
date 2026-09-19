"""GTK application entry point for Box RPG Maker."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

try:
    from box.api import AppPaths, ConfigRepository
    from box.api.interaction import Interaction
    from box.errors import ConfigurationError
except ImportError:
    # Backend not installed: the setup page guides the install, so startup
    # must survive without box. Repos stay None until the restart lands.
    AppPaths = None  # type: ignore[assignment,misc]
    ConfigRepository = None  # type: ignore[assignment,misc]
    Interaction = None  # type: ignore[assignment,misc]
    ConfigurationError = None  # type: ignore[assignment,misc]
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from box_gui.core.backend_check import BackendStatus, check_backend  # noqa: E402
from box_gui.i18n import _  # noqa: E402
from box_gui.i18n import configure as configure_app_translations  # noqa: E402
from box_gui.pages.backend_setup_page import BackendSetupPage  # noqa: E402

if TYPE_CHECKING:
    from box.api import AppPaths as AppPathsT
    from box.api import ConfigRepository as ConfigRepositoryT
    from box.api.interaction import Interaction as InteractionT
    from box.errors import ConfigurationError as ConfigurationErrorT

    from box_gui.core.defaults import DefaultsRepository
    from box_gui.core.library import LibraryEntry, LibraryRepository
    from box_gui.pages.library_page import LibraryPage

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


def _restart_process() -> None:
    """Replace this process with a fresh instance of the same command."""
    os.execv(sys.executable, [sys.executable, *sys.argv])


class BoxRpgApplication(Adw.Application):
    """GTK application presenting the library navigation view."""

    def __init__(self) -> None:
        super().__init__(
            application_id="io.gitlab.christvh.BoxRpgApp",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        _configure_translations()
        self._paths: AppPathsT | None = None
        self._repository: ConfigRepositoryT | None = None
        self._library: LibraryRepository | None = None
        self._defaults_repository: DefaultsRepository | None = None
        self._interaction: InteractionT | None = None
        self._startup_error: ConfigurationErrorT | None = None
        self._navigation: Adw.NavigationView | None = None
        self._css_provider: Gtk.CssProvider | None = None
        self._backend_status: BackendStatus = check_backend()
        app_paths_cls = AppPaths
        config_repository_cls = ConfigRepository
        interaction_cls = Interaction
        error_cls = ConfigurationError
        if (
            app_paths_cls is None
            or config_repository_cls is None
            or interaction_cls is None
            or error_cls is None
        ):
            # Backend not installed: repos stay None and the setup page
            # guides the install; nothing backend-backed is touched here.
            return
        try:
            from box_gui.core.defaults import DefaultsRepository
            from box_gui.core.library import LibraryRepository

            paths = app_paths_cls.from_environment()
            paths.ensure()
            self._paths = paths
            self._repository = config_repository_cls(paths)
            self._library = LibraryRepository(paths)
            self._defaults_repository = DefaultsRepository(paths)
            if GtkInteraction is not None:
                self._interaction = GtkInteraction(parent=None)
        except Exception as exc:
            if isinstance(exc, error_cls):
                self._startup_error = exc
            else:
                raise

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
                b".chip-flow > flowboxchild:active { background-color: transparent; } "
                b".love-heart { color: @error_bg_color; } "
                b".drop-hint-veil { background-color: alpha(@view_bg_color, 0.82); "
                b"border-radius: 12px; padding: 24px; } "
                b".drop-hint-icon { min-width: 64px; min-height: 64px; }"
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
            window = Adw.ApplicationWindow(
                application=self,
                title=_("Box RPG Maker"),
                default_width=880,
                default_height=640,
            )
            navigation = Adw.NavigationView()
            navigation.connect("popped", self._refresh_library_on_return)
            library_page = self._build_library_page()
            if library_page is None:
                navigation.push(
                    BackendSetupPage(self._backend_status, on_ready=self._restart_for_backend)
                )
            else:
                navigation.push(library_page)
            window.set_content(navigation)
            self._navigation = navigation
            setter = getattr(self._interaction, "set_parent", None)
            if callable(setter):
                setter(window)
        window.present()

    @staticmethod
    def _refresh_library_on_return(
        navigation: Adw.NavigationView, _popped: Adw.NavigationPage
    ) -> None:
        """Reload the library list when navigating back to it."""
        try:
            from box_gui.pages.library_page import LibraryPage
        except ImportError:
            return
        visible = navigation.get_visible_page()
        if isinstance(visible, LibraryPage):
            visible.refresh()

    def _build_library_page(self) -> LibraryPage | None:
        """Build the library root page, or None while the backend gate holds."""
        if self._backend_status.needs_setup:
            return None
        from box_gui.pages.library_page import LibraryPage

        library = self._library
        paths = self._paths
        repository = self._repository
        if library is None or paths is None or repository is None:
            return None
        return LibraryPage(
            library=library,
            on_open_game=self._open_game,
            on_open_settings=self.present_settings,
            paths=paths,
            repository=repository,
            interaction=self._interaction,
            defaults_repository=self._defaults_repository,
        )

    def _restart_for_backend(self) -> None:
        """Restart after a verified install so the fresh backend loads.

        The setup page calls this only after confirming the installed
        version equals the expected tag, so startup lands straight in the
        library. os.execv only returns on failure; the setup page then
        surfaces the error and stays put instead of guessing.
        """
        _restart_process()

    def _open_game(self, entry: LibraryEntry) -> None:
        """Push the detail page for one library entry, if possible."""
        from box_gui.pages.game_detail_page import GameDetailPage

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
        dialog: SettingsDialog

        def _on_full_wipe() -> None:
            """Close settings and return to backend setup after a full wipe."""
            with contextlib.suppress(Exception):
                dialog.close()
            try:
                status = check_backend()
            except Exception:
                return
            navigation = self._navigation
            if navigation is None:
                return
            try:
                visible = navigation.get_visible_page()
                root = visible
                while root is not None:
                    try:
                        previous = navigation.get_previous_page(root)
                    except Exception:
                        break
                    if previous is None:
                        break
                    root = previous
                if root is not None and visible is not None and root is not visible:
                    with contextlib.suppress(Exception):
                        navigation.pop_to_page(root)
                navigation.push(BackendSetupPage(status, on_ready=self._restart_for_backend))
            except Exception:
                return

        dialog = SettingsDialog(
            self._paths,
            self._repository,
            self._interaction,
            self._library,
            on_full_wipe=_on_full_wipe,
        )
        dialog.present(parent)


def main(argv: list[str] | None = None) -> None:
    """Run the GTK application."""
    app = BoxRpgApplication()
    raise SystemExit(app.run(argv if argv is not None else sys.argv))
