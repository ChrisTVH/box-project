"""Diagnose dialog row and error tests without running real diagnostics."""

# pyright: reportMissingImports=false
# pyright: reportPrivateUsage=false

from __future__ import annotations

import contextlib
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from box_gui import gtk

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")

    from box.api import AppPaths, ConfigRepository
    from box.api.diagnose import DiagnoseResult
    from box.errors import BoxError
    from gi.repository import Adw

    from box_gui.pages.diagnose_dialog import DiagnoseDialog, diagnose_error_heading

    def Environment(system: str, release: str, machine: str) -> Any:
        row = types.SimpleNamespace(system=system, release=release, machine=machine)
        return row

    def VersionReport(
        engine: str,
        engine_version: str | None,
        nwjs: str | None,
        easyrpg_player: str | None = None,
    ) -> Any:
        row = types.SimpleNamespace(
            engine=engine,
            engine_version=engine_version,
            nwjs=nwjs,
            easyrpg_player=easyrpg_player,
        )
        return row

    _diagnose_available = True
except Exception:
    AppPaths: Any = None
    ConfigRepository: Any = None
    DiagnoseResult: Any = None
    BoxError: Any = Exception
    Adw: Any = None
    DiagnoseDialog: Any = None
    diagnose_error_heading: Any = None
    _diagnose_available = False

pytestmark = pytest.mark.skipif(not _diagnose_available, reason="gi/Adw unavailable")


def _has_display() -> bool:
    """Return True when a Wayland or X11 display looks available."""
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _require_display() -> None:
    """Skip the test when no display is available for real widgets."""
    if not _has_display():
        pytest.skip("no display for diagnose widgets")


def _capture_alerts(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record AlertDialog presents without showing real dialogs."""
    presented: list[Any] = []

    def _fake_present(self: Any, parent: Any | None = None) -> None:
        presented.append(self)

    monkeypatch.setattr(Adw.AlertDialog, "present", _fake_present)
    return presented


def _make_paths(tmp_path: Path) -> Any:
    """Build isolated AppPaths under tmp_path."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environ = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    paths = AppPaths.from_environment(environ)
    paths.ensure()
    return paths


def _make_dialog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_diagnose: Any) -> Any:
    """Create a DiagnoseDialog with a synchronous stubbed diagnose worker."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    module = types.ModuleType("box_gui.gtk.workers")

    def _run_diagnose(
        paths: Any, repository: Any, game_path: Any, on_done: Any, on_error: Any
    ) -> None:
        try:
            result = fake_diagnose(paths, repository, game_path)
        except BaseException as exc:
            on_error(exc)
        else:
            on_done(result)
        return None

    module.run_diagnose = _run_diagnose  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "box_gui.gtk.workers", module)
    monkeypatch.setattr(gtk, "workers", module, raising=False)
    paths = _make_paths(tmp_path)
    return DiagnoseDialog(paths, ConfigRepository(paths), tmp_path / "game")


def _version_titles(dialog: Any) -> list[str]:
    """Collect engine plus optional version row titles."""
    titles = [str(dialog._engine_row.get_title())]
    titles.extend(str(row.get_title()) for row in dialog._version_rows)
    return titles


def _version_subtitles(dialog: Any) -> list[str]:
    """Collect engine plus optional version row subtitles."""
    subtitles = [str(dialog._engine_row.get_subtitle())]
    subtitles.extend(str(row.get_subtitle()) for row in dialog._version_rows)
    return subtitles


def _row_by_title(dialog: Any, title: str) -> Any | None:
    """Find the engine row or an optional row by title."""
    if str(dialog._engine_row.get_title()) == title:
        return dialog._engine_row
    for row in dialog._version_rows:
        if str(row.get_title()) == title:
            return row
    return None


def _is_descendant(widget: Any, ancestor: Any) -> bool:
    """Return True when the widget lives somewhere inside the ancestor."""
    return bool(widget.is_ancestor(ancestor))


def test_diagnose_error_headings_without_display() -> None:
    """Backend errors and unexpected errors map to distinct headings."""
    assert diagnose_error_heading(BoxError("bad game")) == "Diagnose Failed"
    assert diagnose_error_heading(ValueError("boom")) == "Unexpected Error"


def test_diagnose_all_fields_rendered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All populated fields render as rows with no literal None."""

    def _fake(paths: Any, repository: Any, game_path: Any) -> Any:
        return DiagnoseResult(
            Environment("Linux", "6.8", "x86_64"),
            VersionReport("rpg-maker-mv", "1.6.2", "v0.70.0", "0.8.1"),
        )

    presented = _capture_alerts(monkeypatch)
    dialog = _make_dialog(monkeypatch, tmp_path, _fake)
    try:
        assert dialog._system_row.get_subtitle() == "Linux"
        assert dialog._release_row.get_subtitle() == "6.8"
        assert dialog._machine_row.get_subtitle() == "x86_64"
        assert dialog._engine_row.get_subtitle() == "rpg-maker-mv"
        assert _version_titles(dialog) == [
            "Engine",
            "Engine Version",
            "NW.js",
            "EasyRPG Player",
        ]
        assert "None" not in _version_subtitles(dialog)
        assert presented == []
        assert dialog._status.get_text() == ""
        assert dialog._status_box.get_visible() is False
        assert _is_descendant(dialog._engine_row, dialog._versions_group)
        for title in ("Engine Version", "NW.js", "EasyRPG Player"):
            row = _row_by_title(dialog, title)
            assert row is not None
            assert not row.has_css_class("card")
            assert _is_descendant(row, dialog._versions_group)
    finally:
        with contextlib.suppress(Exception):
            dialog.close()


def test_diagnose_nwjs_none_omitted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing NW.js version omits its row without rendering None."""

    def _fake(paths: Any, repository: Any, game_path: Any) -> Any:
        return DiagnoseResult(
            Environment("Linux", "6.8", "x86_64"),
            VersionReport("rpg-maker-2000-2003", None, None, "0.8.1"),
        )

    _capture_alerts(monkeypatch)
    dialog = _make_dialog(monkeypatch, tmp_path, _fake)
    try:
        assert "NW.js" not in _version_titles(dialog)
        assert "Engine Version" not in _version_titles(dialog)
        assert "EasyRPG Player" in _version_titles(dialog)
        assert "None" not in _version_subtitles(dialog)
        easyrpg_row = _row_by_title(dialog, "EasyRPG Player")
        assert easyrpg_row is not None
        assert not easyrpg_row.has_css_class("card")
        assert _is_descendant(easyrpg_row, dialog._versions_group)
    finally:
        with contextlib.suppress(Exception):
            dialog.close()


def test_diagnose_easyrpg_none_omitted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing EasyRPG version omits its row without rendering None."""

    def _fake(paths: Any, repository: Any, game_path: Any) -> Any:
        return DiagnoseResult(
            Environment("Linux", "6.8", "x86_64"),
            VersionReport("rpg-maker-mv", "1.6.2", "v0.70.0", None),
        )

    _capture_alerts(monkeypatch)
    dialog = _make_dialog(monkeypatch, tmp_path, _fake)
    try:
        assert "EasyRPG Player" not in _version_titles(dialog)
        assert "NW.js" in _version_titles(dialog)
        assert "Engine Version" in _version_titles(dialog)
        assert "None" not in _version_subtitles(dialog)
        nwjs_row = _row_by_title(dialog, "NW.js")
        assert nwjs_row is not None
        assert not nwjs_row.has_css_class("card")
        assert _is_descendant(nwjs_row, dialog._versions_group)
        engine_version_row = _row_by_title(dialog, "Engine Version")
        assert engine_version_row is not None
        assert _is_descendant(engine_version_row, dialog._versions_group)
    finally:
        with contextlib.suppress(Exception):
            dialog.close()


def test_diagnose_box_error_heading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A BoxError failure shows a single-close Diagnose Failed dialog."""

    def _fake(paths: Any, repository: Any, game_path: Any) -> Any:
        raise BoxError("bad game")

    presented = _capture_alerts(monkeypatch)
    dialog = _make_dialog(monkeypatch, tmp_path, _fake)
    try:
        assert dialog._status.get_text() == "Diagnose failed."
        assert len(presented) == 1
        assert presented[0].get_heading() == "Diagnose Failed"
        assert presented[0].has_response("close")
    finally:
        with contextlib.suppress(Exception):
            dialog.close()


def test_diagnose_unexpected_error_heading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unexpected failure shows a single-close Unexpected Error dialog."""

    def _fake(paths: Any, repository: Any, game_path: Any) -> Any:
        raise ValueError("boom")

    presented = _capture_alerts(monkeypatch)
    dialog = _make_dialog(monkeypatch, tmp_path, _fake)
    try:
        assert dialog._status.get_text() == "Diagnose failed."
        assert len(presented) == 1
        assert presented[0].get_heading() == "Unexpected Error"
        assert presented[0].has_response("close")
    finally:
        with contextlib.suppress(Exception):
            dialog.close()
