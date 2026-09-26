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
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

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


def _apply_stored_ci_mount_debug(paths: AppPathsT, defaults: DefaultsRepository) -> None:
    """Replay the stored ci-mount trace switch into the environment.

    The preference outlives the process, so it must be re-exported before
    the first game launch or an enabled switch would show as on while the
    daemon logs nothing. A missing or malformed debug preference must never
    block startup, so the documented preference failures are caught
    explicitly instead of a bare ``Exception``, which would also swallow a
    real bug in the replay itself.
    """
    from box_gui.core.cimount_debug import apply_debug_log

    try:
        stored = defaults.load()
        apply_debug_log(stored.ci_mount_debug_enabled, stored.ci_mount_debug_log, paths)
    except OSError, ValueError:
        # DefaultsError (the frontend's own failure type, a ValueError
        # subclass) covers a malformed, unreadable, or wrongly versioned
        # defaults.json; the OSError covers a file that cannot be read and a
        # log directory that cannot be prepared. The daemon then inherits no
        # trace variable, which is exactly the state a clean shell starts
        # from, so the run continues without diagnostics.
        return


class BoxRpgApplication(Adw.Application):
    """GTK application presenting the library navigation view."""

    def __init__(self) -> None:
        super().__init__(
            application_id="io.gitlab.christvh.BoxRpgApp",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        try:
            preload_cls = AppPaths
            if preload_cls is not None:
                try:
                    from box_gui.core.defaults import DefaultsRepository as _PreloadDefaults
                except ImportError:
                    _PreloadDefaults = None  # type: ignore[assignment]
                if _PreloadDefaults is not None:
                    try:
                        preload_paths = preload_cls.from_environment()
                        try:
                            stored = _PreloadDefaults(preload_paths).load().preferred_language
                        except Exception:
                            stored = None
                        if stored is not None:
                            try:
                                from box_gui.pages.settings_dialog import (
                                    SUPPORTED_LANGUAGES as _PreloadSupported,
                                )
                            except Exception:
                                _PreloadSupported = frozenset((None, "en", "es"))
                            if stored in _PreloadSupported:
                                os.environ["LANGUAGE"] = stored
                            else:
                                os.environ.pop("LANGUAGE", None)
                    except Exception:
                        pass
        except Exception:
            pass
        _configure_translations()
        self._paths: AppPathsT | None = None
        self._repository: ConfigRepositoryT | None = None
        self._library: LibraryRepository | None = None
        self._defaults_repository: DefaultsRepository | None = None
        self._interaction: InteractionT | None = None
        self._startup_error: ConfigurationErrorT | None = None
        self._navigation: Adw.NavigationView | None = None
        self._css_provider: Gtk.CssProvider | None = None
        self._debugging_dialog: Adw.Dialog | None = None
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
            _apply_stored_ci_mount_debug(paths, self._defaults_repository)
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
                b".update-check { background-color: #326935; "
                b"color: #cffcdf; } "
                # The library footer debug button matches the header action
                # buttons, so both share one rule: their sizes cannot drift.
                b".header-action, .footer-action { min-width: 18px; min-height: 18px; } "
                b".header-action image, .footer-action image { -gtk-icon-size: 24px; } "
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
        with contextlib.suppress(Exception):
            GLib.idle_add(self._maybe_check_updates)

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
            on_open_debugging=self.present_debugging,
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

    def _maybe_check_updates(self) -> bool:
        """One-shot startup AppImage update check; never blocks the main loop.

        Skips when there is no embedded tag (dev checkout), when the
        cadence is off, when the backend gate owns the screen, or when the
        AppImage check is not due. Otherwise discovers the latest tag off
        the main loop and prompts for an AppImage update when warranted.
        Degrades silently except for showing the update page itself.
        """
        try:
            import time as _time

            from box_gui.core.app_info import get_embedded_tag
            from box_gui.core.appimage_update import (
                update_check_due,
            )

            try:
                embedded = get_embedded_tag()
            except Exception:
                return False
            if embedded is None:
                return False
            if self._backend_status.needs_setup:
                return False
            defaults_repository = self._defaults_repository
            if defaults_repository is None:
                return False
            try:
                defaults = defaults_repository.load()
            except Exception:
                return False
            try:
                now = _time.time()
                due = update_check_due(
                    now,
                    defaults.update_interval,
                    defaults.last_appimage_check_at,
                )
            except Exception:
                return False
            if not due:
                return False
            try:
                from box_gui.gtk.threads import run_in_thread
            except ImportError:
                return False

            def _work() -> tuple[str, str] | None:
                try:
                    from box_gui.core.updates import discover_latest_tag
                except ImportError:
                    return None
                try:
                    return discover_latest_tag(timeout=15.0)
                except Exception:
                    return None

            def _on_done(result: tuple[str, str] | None) -> None:
                if result is None:
                    return
                latest, source = result
                self._finish_update_discovery(embedded, latest, source)

            def _on_error(_error: BaseException) -> None:
                return

            try:
                run_in_thread(_work, _on_done, _on_error)
            except Exception:
                return False
        except Exception:
            pass
        return False

    def _finish_update_discovery(self, embedded: str, latest: str, source: str = "github") -> None:
        """Record the check time and prompt for an AppImage update when due.

        The expected backend version derives from the embedded AppImage
        tag, so a backend-newer-without-AppImage-newer outcome cannot
        happen by construction: one AppImage prompt covers the update and
        the restarted process re-enters through the normal backend
        mismatch gate. Hence no separate backend prompt exists.
        """
        try:
            import contextlib as _contextlib
            import time as _time

            from box_gui.core.appimage_update import should_offer_appimage_update
            from box_gui.core.updates import is_due

            defaults_repository = self._defaults_repository
            navigation = self._navigation
            if defaults_repository is None or navigation is None:
                return
            try:
                defaults = defaults_repository.load()
            except Exception:
                return
            now = _time.time()
            interval = defaults.update_interval
            try:
                appimage_due = is_due(now, interval, defaults.last_appimage_check_at)
            except Exception:
                return
            if not appimage_due:
                return
            # Throttle future checks even when the result is "no update".
            with _contextlib.suppress(Exception):
                defaults_repository.record_appimage_check(now)
            if not should_offer_appimage_update(
                embedded, latest, defaults.skipped_appimage_version
            ):
                return
            # Revalidate before pushing: the worker took time, so the gate,
            # the visible page, or the cadence may have changed since.
            if self._backend_status.needs_setup:
                return
            try:
                visible = navigation.get_visible_page()
            except Exception:
                return
            if visible is None:
                return
            # Never stack another setup/update page over the gate.
            if isinstance(visible, BackendSetupPage):
                return
            try:
                fresh = defaults_repository.load()
            except Exception:
                return
            if fresh.update_interval == "off":
                return
            if fresh.skipped_appimage_version == latest:
                return
            if not should_offer_appimage_update(embedded, latest, fresh.skipped_appimage_version):
                return
            page = BackendSetupPage(self._backend_status, on_ready=self._restart_for_backend)

            def _on_skip() -> None:
                with _contextlib.suppress(Exception):
                    defaults_repository.set_skipped_appimage_version(latest)
                with _contextlib.suppress(Exception):
                    defaults_repository.record_appimage_check()
                with _contextlib.suppress(Exception):
                    navigation.pop()

            def _on_update_done() -> None:
                with _contextlib.suppress(Exception):
                    defaults_repository.record_appimage_check()

            with _contextlib.suppress(Exception):
                page.start_appimage_update(
                    latest, _on_update_done, _on_skip, current_tag=embedded, source=source
                )
                navigation.push(page)
        except Exception:
            pass

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
        dialog: SettingsDialog | None = None

        def _on_update_available(latest: str, source: str) -> None:
            """Close settings and show the AppImage update page for latest.

            A manual check forces the offer: the cadence, due time, and any
            skipped version are ignored because the user explicitly asked.
            """
            current = dialog
            with contextlib.suppress(Exception):
                if current is not None:
                    current.close()
            if self._backend_status.needs_setup:
                return
            navigation = self._navigation
            if navigation is None:
                return
            try:
                page = BackendSetupPage(self._backend_status, on_ready=self._restart_for_backend)
            except Exception:
                return

            def _on_skip() -> None:
                with contextlib.suppress(Exception):
                    if self._defaults_repository is not None:
                        self._defaults_repository.set_skipped_appimage_version(latest)
                with contextlib.suppress(Exception):
                    if self._defaults_repository is not None:
                        self._defaults_repository.record_appimage_check()
                with contextlib.suppress(Exception):
                    navigation.pop()

            def _on_update_done() -> None:
                with contextlib.suppress(Exception):
                    if self._defaults_repository is not None:
                        self._defaults_repository.record_appimage_check()

            try:
                from box_gui.core.app_info import get_embedded_tag
            except ImportError:
                return
            try:
                embedded = get_embedded_tag()
            except Exception:
                embedded = None
            try:
                with contextlib.suppress(Exception):
                    page.start_appimage_update(
                        latest, _on_update_done, _on_skip, current_tag=embedded, source=source
                    )
                    navigation.push(page)
            except Exception:
                return

        def _on_full_wipe() -> None:
            """Close settings and return to backend setup after a full wipe."""
            current = dialog
            with contextlib.suppress(Exception):
                if current is not None:
                    current.close()
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

        def _on_language_changed(language: str | None) -> None:
            """Rebuild the UI live so the new language applies without restart."""
            nonlocal dialog
            navigation = self._navigation
            if navigation is None:
                return
            supported: frozenset[str | None]
            try:
                from box_gui.pages.settings_dialog import SUPPORTED_LANGUAGES

                supported = SUPPORTED_LANGUAGES
            except Exception:
                supported = frozenset((None, "en", "es"))
            if language not in supported:
                language = None
            try:
                if language is None:
                    os.environ.pop("LANGUAGE", None)
                else:
                    os.environ["LANGUAGE"] = language
            except Exception:
                pass
            with contextlib.suppress(Exception):
                _configure_translations()
            with contextlib.suppress(Exception):
                if dialog is not None:
                    dialog.close()
            try:
                fresh = self._build_library_page()
            except Exception:
                return
            if fresh is None:
                try:
                    visible = navigation.get_visible_page()
                except Exception:
                    return
                if visible is None:
                    return
                try:
                    from box_gui.pages.settings_dialog import (
                        SettingsDialog as FreshSettingsDialog,
                    )
                except ImportError:
                    return
                if self._paths is None or self._repository is None:
                    return
                try:
                    fresh_dialog = FreshSettingsDialog(
                        self._paths,
                        self._repository,
                        self._interaction,
                        self._library,
                        on_full_wipe=_on_full_wipe,
                        on_language_changed=_on_language_changed,
                        on_update_available=_on_update_available,
                    )
                except Exception:
                    return
                dialog = fresh_dialog
                try:
                    with contextlib.suppress(Exception):
                        fresh_dialog.present(visible)
                except Exception:
                    return
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
                with contextlib.suppress(Exception):
                    navigation.replace([fresh])
            except Exception:
                return
            try:
                from box_gui.pages.settings_dialog import SettingsDialog as FreshSettingsDialog
            except ImportError:
                return
            if self._paths is None or self._repository is None:
                return
            try:
                fresh_dialog = FreshSettingsDialog(
                    self._paths,
                    self._repository,
                    self._interaction,
                    self._library,
                    on_full_wipe=_on_full_wipe,
                    on_language_changed=_on_language_changed,
                    on_update_available=_on_update_available,
                )
            except Exception:
                return
            dialog = fresh_dialog
            try:
                with contextlib.suppress(Exception):
                    fresh_dialog.present(fresh)
            except Exception:
                return

        dialog = SettingsDialog(
            self._paths,
            self._repository,
            self._interaction,
            self._library,
            on_full_wipe=_on_full_wipe,
            on_language_changed=_on_language_changed,
            on_update_available=_on_update_available,
        )
        dialog.present(parent)

    def present_debugging(self, parent: Gtk.Widget) -> None:
        """Present the Debugging window opened from the library footer.

        The page is imported lazily so startup never depends on it, the
        same convention as ``present_settings``. Adw.PreferencesWindow is
        deprecated in libadwaita 1.9, so the window is an
        Adw.PreferencesDialog holding the single page: it supplies the
        title bar and close button that a bare Adw.Dialog does not, and
        with one page libadwaita shows no tab switcher at all. The dialog
        is kept on ``self`` while open: a local would let the only strong
        reference die and take the window with it.
        """
        if self._paths is None:
            return
        try:
            from box_gui.pages.settings_dialog import DebuggingPage
        except ImportError:
            return
        dialog = Adw.PreferencesDialog(title=_("Debugging"), content_width=520, content_height=380)
        if hasattr(dialog, "set_search_enabled"):
            dialog.set_search_enabled(False)
        dialog.add(DebuggingPage(self._paths))
        self._debugging_dialog = dialog
        dialog.connect("closed", self._on_debugging_dialog_closed)
        root = parent.get_root()
        if isinstance(root, Gtk.Window):
            dialog.present(root)
        else:
            dialog.present()

    def _on_debugging_dialog_closed(self, dialog: Adw.Dialog) -> None:
        """Drop the closed Debugging window so it can be garbage collected.

        Guarded so an older window closing late never clears the reference
        to a newer one opened in the meantime.
        """
        if self._debugging_dialog is dialog:
            self._debugging_dialog = None


def main(argv: list[str] | None = None) -> None:
    """Run the GTK application."""
    app = BoxRpgApplication()
    raise SystemExit(app.run(argv if argv is not None else sys.argv))
