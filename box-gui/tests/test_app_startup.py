"""Startup replay tests for the frontend-owned preferences."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from box.paths import AppPaths

from box_gui.core.cimount_debug import CIMOUNT_DEBUG_LOG_ENV, CIMOUNT_DEBUG_LOG_FILENAME
from box_gui.core.defaults import DefaultsRepository

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")

    from gi.repository import Adw, Gtk

    from box_gui import app as app_module

    _app_available = True
except Exception:
    app_module: Any = None
    Adw: Any = None
    Gtk: Any = None
    _app_available = False

pytestmark = pytest.mark.skipif(not _app_available, reason="gi/Adw unavailable")


@pytest.fixture(autouse=True)
def _restore_debug_env() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Restore the ci-mount trace variable around every startup test.

    The replay writes the real process environment, so a test that fails
    mid-assertion would otherwise leave a trace path behind for the rest of
    the session. The fixture tears down even on failure, which monkeypatch
    alone cannot guarantee for a variable the code sets itself.
    """
    previous = os.environ.pop(CIMOUNT_DEBUG_LOG_ENV, None)
    try:
        yield
    finally:
        os.environ.pop(CIMOUNT_DEBUG_LOG_ENV, None)
        if previous is not None:
            os.environ[CIMOUNT_DEBUG_LOG_ENV] = previous


def _paths(tmp_path: Path) -> AppPaths:
    """Build isolated AppPaths under tmp_path with its roots created."""
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    return paths


def test_startup_replays_the_stored_debug_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An enabled switch with a chosen file re-exports it before any launch.

    The preference lives in defaults.json and the daemon only reads the
    environment of the process that forks it, so dropping this replay makes
    the switch read as on while the daemon logs nothing after a restart.
    """
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)
    paths = _paths(tmp_path)
    defaults = DefaultsRepository(paths)
    chosen = tmp_path / "traces" / "ci.log"
    defaults.set_ci_mount_debug(True, str(chosen))

    app_module._apply_stored_ci_mount_debug(paths, defaults)

    assert os.environ[CIMOUNT_DEBUG_LOG_ENV] == str(chosen)


def test_startup_replays_the_default_debug_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An enabled switch without a file exports the cache-root default."""
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)
    paths = _paths(tmp_path)
    defaults = DefaultsRepository(paths)
    defaults.set_ci_mount_debug(True)

    app_module._apply_stored_ci_mount_debug(paths, defaults)

    expected = str(paths.cache_root / CIMOUNT_DEBUG_LOG_FILENAME)
    assert os.environ[CIMOUNT_DEBUG_LOG_ENV] == expected


def test_startup_drops_a_disabled_debug_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A disabled switch removes a trace variable inherited from the shell."""
    monkeypatch.setenv(CIMOUNT_DEBUG_LOG_ENV, str(Path("/tmp") / "inherited.log"))
    paths = _paths(tmp_path)
    defaults = DefaultsRepository(paths)
    defaults.set_ci_mount_debug(False, str(tmp_path / "remembered.log"))

    app_module._apply_stored_ci_mount_debug(paths, defaults)

    assert CIMOUNT_DEBUG_LOG_ENV not in os.environ


@pytest.mark.parametrize(
    "content",
    [None, "not json", json.dumps({"version": 99}), json.dumps(["not", "an", "object"])],
    ids=["absent", "corrupt", "wrong-version", "wrong-shape"],
)
def test_startup_never_raises_on_unusable_preferences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str | None
) -> None:
    """A missing or malformed defaults file leaves startup on a clean trace.

    The replay runs inside the application constructor, where an escaping
    exception would abort startup before the window exists, so the run must
    continue with no trace variable instead.
    """
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)
    paths = _paths(tmp_path)
    defaults = DefaultsRepository(paths)
    if content is not None:
        defaults.defaults_file.parent.mkdir(parents=True, exist_ok=True)
        defaults.defaults_file.write_text(content, encoding="utf-8")

    app_module._apply_stored_ci_mount_debug(paths, defaults)

    assert CIMOUNT_DEBUG_LOG_ENV not in os.environ


def _debug_app(paths: AppPaths | None) -> Any:
    """Build a bare application object with paths wired and no window."""
    app = app_module.BoxRpgApplication.__new__(app_module.BoxRpgApplication)
    app._paths = paths
    app._debugging_dialog = None
    return app


def _dialog_pages(dialog: Any) -> list[Any]:
    """Return the preferences pages reachable from a dialog's widget tree.

    AdwPreferencesDialog keeps its pages in a private template stack, so
    they are not reachable this way; the helper exists so a caller can skip
    the page assertion on versions that do not expose them.
    """
    pages: list[Any] = []

    def _walk(widget: Any, depth: int = 0) -> None:
        if widget is None or depth > 14:
            return
        # libadwaita nests its own internal AdwPreferencesPage subclasses
        # (the search results view), which carry no name; only the hosted
        # page does.
        if isinstance(widget, Adw.PreferencesPage) and widget.get_name() is not None:
            pages.append(widget)
        child = widget.get_first_child()
        while child is not None:
            _walk(child, depth + 1)
            child = child.get_next_sibling()

    _walk(dialog)
    return pages


def test_present_debugging_opens_a_titled_window(tmp_path: Path) -> None:
    """The footer callback opens a titled Debugging window holding the page.

    A bare Adw.Dialog has no header bar of its own, and AdwPreferencesPage
    does not draw one either, so the window would come up with no title and
    no close button. Asserting the concrete host widget pins that.
    """
    with contextlib.suppress(Exception):
        Adw.init()
    paths = _paths(tmp_path)
    app = _debug_app(paths)

    app.present_debugging(Gtk.Box())

    dialog = app._debugging_dialog
    assert isinstance(dialog, Adw.PreferencesDialog)
    assert dialog.get_title() == "Debugging"
    assert dialog.get_search_enabled() is False
    # A single page means libadwaita draws no tab switcher at all, which is
    # why this page is hosted here instead of inside the Settings dialog.
    assert dialog.get_visible_page() is not None
    assert [page.get_name() for page in _dialog_pages(dialog)] == ["depuracion"]

    dialog.close()
    assert app._debugging_dialog is None


def test_present_debugging_without_paths_does_nothing() -> None:
    """No configured paths means no window, rather than a failing import."""
    app = _debug_app(None)
    app.present_debugging(Gtk.Box())
    assert app._debugging_dialog is None
