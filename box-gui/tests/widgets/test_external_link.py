"""External link confirmation tests with stubbed toolkit, no network."""

# pyright: reportMissingImports=false

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")

    import box_gui.widgets.external_link as external_link_module
    from box_gui.widgets.external_link import confirm_and_open_external_link

    _external_link_available = True
except Exception:
    external_link_module: Any = None
    confirm_and_open_external_link: Any = None
    _external_link_available = False

pytestmark = pytest.mark.skipif(not _external_link_available, reason="gi/Adw unavailable")


class _FakeResponseAppearance:
    """Minimal ResponseAppearance with only the SUGGESTED member."""

    SUGGESTED = "suggested"


class _FakeDialog:
    """Record AlertDialog construction without needing a display."""

    presented: ClassVar[list[Any]] = []

    def __init__(self, heading: str = "", body: str = "") -> None:
        self._heading = heading
        self._body = body
        self._responses: dict[str, str] = {}
        self._appearances: dict[str, str] = {}
        self._default_response: str | None = None
        self._close_response: str | None = None
        self._handlers: dict[str, Any] = {}
        self._parent: Any = None

    def add_response(self, response_id: str, label: str) -> None:
        """Record one response identifier and its label."""
        self._responses[response_id] = label

    def set_response_appearance(self, response_id: str, appearance: str) -> None:
        """Record the appearance for one response."""
        self._appearances[response_id] = appearance

    def set_default_response(self, response_id: str) -> None:
        """Record the default response."""
        self._default_response = response_id

    def set_close_response(self, response_id: str) -> None:
        """Record the close response."""
        self._close_response = response_id

    def connect(self, signal: str, handler: Any) -> None:
        """Record a signal handler for later emission."""
        self._handlers[signal] = handler

    def present(self, parent: Any | None = None) -> None:
        """Record the present without showing a real dialog."""
        self._parent = parent
        _FakeDialog.presented.append(self)

    def emit(self, signal: str, response: str) -> None:
        """Invoke a recorded response handler synchronously."""
        handler = self._handlers.get(signal)
        if handler is not None:
            handler(self, response)

    def get_heading(self) -> str:
        """Return the recorded heading."""
        return self._heading

    def get_body(self) -> str:
        """Return the recorded body."""
        return self._body

    def get_response_label(self, response_id: str) -> str | None:
        """Return the label recorded for one response."""
        return self._responses.get(response_id)

    def get_response_appearance(self, response_id: str) -> str | None:
        """Return the appearance recorded for one response."""
        return self._appearances.get(response_id)


class _FakeLauncher:
    """Record UriLauncher launches without touching the network."""

    launched: ClassVar[list[str]] = []

    def __init__(self, uri: str = "") -> None:
        self._uri = uri

    def launch(self) -> None:
        """Record the launch URI instead of opening a browser."""
        _FakeLauncher.launched.append(self._uri)


class _FakeAppInfo:
    """Record Gio fallback launches without touching the network."""

    launched: ClassVar[list[str]] = []

    @staticmethod
    def launch_default_for_uri(uri: str, _context: Any | None = None) -> None:
        """Record the fallback URI instead of opening a browser."""
        _FakeAppInfo.launched.append(uri)


def _install_fake_toolkit(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stub Adw/Gtk/Gio in the module so tests run headless without network."""
    _FakeDialog.presented.clear()
    _FakeLauncher.launched.clear()
    _FakeAppInfo.launched.clear()
    fake_adw = SimpleNamespace(AlertDialog=_FakeDialog, ResponseAppearance=_FakeResponseAppearance)
    fake_gtk = SimpleNamespace(UriLauncher=_FakeLauncher)
    fake_gio = SimpleNamespace(AppInfo=_FakeAppInfo)
    monkeypatch.setattr(external_link_module, "Adw", fake_adw)
    monkeypatch.setattr(external_link_module, "Gtk", fake_gtk)
    monkeypatch.setattr(external_link_module, "Gio", fake_gio)
    return SimpleNamespace(dialogs=_FakeDialog.presented, launched=_FakeLauncher.launched)


def test_dialog_shows_literal_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The confirmation shows the literal URL with Cancel/Open responses."""
    records = _install_fake_toolkit(monkeypatch)
    url = "https://example.com/?a=1&b=2"

    confirm_and_open_external_link(None, url)

    assert len(records.dialogs) == 1
    dialog = records.dialogs[0]
    assert dialog.get_heading() == "Open external link?"
    assert dialog.get_body() == f"You are about to open {url}."
    assert url in dialog.get_body()
    assert dialog.get_response_label("cancel") == "Cancel"
    assert dialog.get_response_label("open") == "Open"
    assert dialog.get_response_appearance("open") == "suggested"


def test_open_response_launches_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    """Picking Open launches the confirmed URL without network access."""
    records = _install_fake_toolkit(monkeypatch)
    url = "https://example.com/project"

    confirm_and_open_external_link(None, url)
    assert records.launched == []

    records.dialogs[0].emit("response", "open")

    assert records.launched == [url]
    assert _FakeAppInfo.launched == []


def test_cancel_response_does_not_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Picking Cancel leaves browsers and fallbacks untouched."""
    records = _install_fake_toolkit(monkeypatch)

    confirm_and_open_external_link(None, "https://example.com/")
    records.dialogs[0].emit("response", "cancel")

    assert records.launched == []
    assert _FakeAppInfo.launched == []


def test_fallback_uses_gio_without_uri_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing UriLauncher degrades to the Gio default-URI fallback."""
    _FakeDialog.presented.clear()
    _FakeLauncher.launched.clear()
    _FakeAppInfo.launched.clear()
    fake_adw = SimpleNamespace(AlertDialog=_FakeDialog, ResponseAppearance=_FakeResponseAppearance)
    fake_gtk = SimpleNamespace(UriLauncher=None)
    fake_gio = SimpleNamespace(AppInfo=_FakeAppInfo)
    monkeypatch.setattr(external_link_module, "Adw", fake_adw)
    monkeypatch.setattr(external_link_module, "Gtk", fake_gtk)
    monkeypatch.setattr(external_link_module, "Gio", fake_gio)
    url = "https://example.com/fallback"

    confirm_and_open_external_link(None, url)
    assert len(_FakeDialog.presented) == 1

    _FakeDialog.presented[0].emit("response", "open")

    assert _FakeLauncher.launched == []
    assert _FakeAppInfo.launched == [url]


def test_launch_failure_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Launcher errors never propagate to the library view."""

    class _FailingLauncher:
        def __init__(self, uri: str = "") -> None:
            self._uri = uri

        def launch(self) -> None:
            raise RuntimeError("no browser")

    _FakeDialog.presented.clear()
    _FakeAppInfo.launched.clear()
    fake_adw = SimpleNamespace(AlertDialog=_FakeDialog, ResponseAppearance=_FakeResponseAppearance)
    fake_gtk = SimpleNamespace(UriLauncher=_FailingLauncher)
    fake_gio = SimpleNamespace(AppInfo=_FakeAppInfo)
    monkeypatch.setattr(external_link_module, "Adw", fake_adw)
    monkeypatch.setattr(external_link_module, "Gtk", fake_gtk)
    monkeypatch.setattr(external_link_module, "Gio", fake_gio)

    confirm_and_open_external_link(None, "https://example.com/")
    _FakeDialog.presented[0].emit("response", "open")

    assert _FakeAppInfo.launched == []
