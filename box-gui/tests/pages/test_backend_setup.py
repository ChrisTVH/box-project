"""Backend setup page tests: headless state logic plus display-gated widgets."""

# pyright: reportMissingImports=false
# pyright: reportPrivateUsage=false

from __future__ import annotations

import contextlib
import os
import sys
import types
from typing import Any

import pytest

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")

    from gi.repository import Adw, GLib, Gtk

    import box_gui.pages.backend_setup_page as backend_setup_page_module
    from box_gui.core.backend_check import BackendStatus, DependencyStatus
    from box_gui.core.backend_install import BackendInstallError, InstallOutcome, InstallPhase
    from box_gui.pages.backend_setup_page import (
        BackendSetupPage,
        describe_status,
        missing_required_text,
    )

    _setup_available = True
except Exception:
    backend_setup_page_module: Any = None
    BackendStatus: Any = None
    DependencyStatus: Any = None
    BackendInstallError: Any = Exception
    InstallOutcome: Any = None
    InstallPhase: Any = None
    Adw: Any = None
    GLib: Any = None
    Gtk: Any = None
    BackendSetupPage: Any = None
    describe_status: Any = None
    missing_required_text: Any = None
    _setup_available = False

pytestmark = pytest.mark.skipif(not _setup_available, reason="gi/Adw unavailable")


def _has_display() -> bool:
    """Return True when a Wayland or X11 display looks available."""
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _require_display() -> None:
    """Skip the test when no display is available for real widgets."""
    if not _has_display():
        pytest.skip("no display for setup widgets")


def _capture_alerts(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record AlertDialog presents without showing real dialogs."""
    presented: list[Any] = []

    def _fake_present(self: Any, parent: Any | None = None) -> None:
        presented.append(self)

    monkeypatch.setattr(Adw.AlertDialog, "present", _fake_present)
    return presented


def _missing_status() -> Any:
    """Build the gate status for a backend that was never installed."""
    return BackendStatus(
        state="missing",
        expected_version="26.9.43",
        installed_version=None,
        message="The box-rpg backend is not installed.",
    )


def _all_ok_dependencies() -> tuple[Any, ...]:
    """Build one available entry per setup dependency."""
    return (
        DependencyStatus(
            key="python", label="Python 3.14+", available=True, required=True, detail="3.14.7"
        ),
        DependencyStatus(key="pip", label="pip", available=True, required=True, detail="available"),
        DependencyStatus(
            key="bwrap", label="Bubblewrap", available=True, required=True, detail="ready"
        ),
        DependencyStatus(
            key="gamemode", label="GameMode", available=True, required=False, detail="available"
        ),
    )


def _install_sync_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve synchronous fake workers plus immediate idle dispatch."""
    import box_gui.gtk as gtk_package

    module = types.ModuleType("box_gui.gtk.workers")

    def _run_in_thread(fn: Any, on_done: Any, on_error: Any) -> None:
        try:
            result = fn()
        except BaseException as exc:
            on_error(exc)
        else:
            on_done(result)
        return None

    module.run_in_thread = _run_in_thread  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "box_gui.gtk.workers", module)
    monkeypatch.setattr(gtk_package, "workers", module, raising=False)

    def _immediate_idle(callback: Any, *args: Any) -> int:
        callback(*args)
        return 0

    monkeypatch.setattr(GLib, "idle_add", _immediate_idle)


def test_describe_status_headings() -> None:
    """Each gate outcome maps to a distinct heading with its message intact."""
    missing = BackendStatus(
        state="missing", expected_version="26.9.43", installed_version=None, message="gone"
    )
    mismatch = BackendStatus(
        state="mismatch",
        expected_version="26.9.43",
        installed_version="26.9.42",
        message="skew",
    )
    error = BackendStatus(
        state="error", expected_version="26.9.43", installed_version=None, message="denied"
    )

    assert describe_status(missing) == ("Backend Required", "gone")
    assert describe_status(mismatch) == ("Backend Update Required", "skew")
    assert describe_status(error) == ("Backend Check Failed", "denied")


def test_missing_required_text() -> None:
    """Only missing required dependencies are named; optionals never block."""
    assert missing_required_text(_all_ok_dependencies()) is None

    without_gamemode = tuple(item for item in _all_ok_dependencies() if item.key != "gamemode")
    assert missing_required_text(without_gamemode) is None

    broken = tuple(
        DependencyStatus(
            key=item.key,
            label=item.label,
            available=False,
            required=item.required,
            detail=item.detail,
        )
        for item in _all_ok_dependencies()
    )
    text = missing_required_text(broken)

    assert text is not None
    assert "Bubblewrap" in text
    assert "pip" in text
    assert "Python 3.14+" in text
    assert "GameMode" not in text


def test_restart_process_replaces_the_same_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ready callback restarts the identical command for a fresh gate."""
    from box_gui.app import _restart_process

    calls: list[list[str]] = []

    def _fake_execv(path: str, args: list[str]) -> None:
        calls.append([path, *args])

    monkeypatch.setattr(os, "execv", _fake_execv)

    _restart_process()

    assert calls == [[sys.executable, sys.executable, *sys.argv]]


def test_page_is_a_tagged_navigation_page() -> None:
    """The setup page plugs into the NavigationView with its own tag."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()

    page = BackendSetupPage(_missing_status())

    assert isinstance(page, Adw.NavigationPage)
    assert page.get_tag() == "backend-setup"
    assert page.get_title() == "Backend Setup"


def test_button_starts_disabled_until_detection() -> None:
    """Install stays disabled with its versioned label before detection."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()

    page = BackendSetupPage(_missing_status())

    assert page._action_button.get_sensitive() is False
    assert page._action_button.get_label() == "Install box-rpg 26.9.43"
    assert page._phase is InstallPhase.CHECKING


def test_header_hides_library_actions() -> None:
    """The setup header keeps the raised style but offers no + or settings."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()

    page = BackendSetupPage(_missing_status())
    tooltips: list[str] = []

    def _walk(widget: Any) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button) and child.get_tooltip_text():
                tooltips.append(str(child.get_tooltip_text()))
            _walk(child)
            child = child.get_next_sibling()

    _walk(page._header_bar)

    assert "Add game" not in tooltips
    assert "Settings" not in tooltips


def test_logo_uses_the_box_icon() -> None:
    """The centered logo resolves through the vendored box icon."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()

    page = BackendSetupPage(_missing_status())
    icons: list[str] = []

    def _walk(widget: Any) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Image) and child.get_icon_name():
                icons.append(str(child.get_icon_name()))
            _walk(child)
            child = child.get_next_sibling()

    _walk(page)

    assert "box-rpg-box-symbolic" in icons


def test_detection_enables_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Completed detection renders four rows and enables Install."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _all_ok_dependencies)
    page = BackendSetupPage(_missing_status())

    page.start_detection()

    assert page._dependencies is not None
    assert [row.get_title() for row in page._dep_rows] == [
        "Python 3.14+",
        "pip",
        "Bubblewrap",
        "GameMode",
    ]
    assert page._action_button.get_sensitive() is True
    assert page._action_button.get_label() == "Install box-rpg 26.9.43"
    assert page._phase is InstallPhase.READY


def test_detection_warns_about_missing_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing required dependency surfaces in the status line."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)

    def _without_bwrap() -> tuple[Any, ...]:
        kept = tuple(item for item in _all_ok_dependencies() if item.key != "bwrap")
        return (
            *kept,
            DependencyStatus(
                key="bwrap",
                label="Bubblewrap",
                available=False,
                required=True,
                detail="missing",
            ),
        )

    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _without_bwrap)
    page = BackendSetupPage(_missing_status())

    page.start_detection()

    assert "Bubblewrap" in page._status_label.get_text()
    assert page._action_button.get_sensitive() is True


def test_detection_error_alerts_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken detection fails closed; Retry re-runs detection."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    attempts: list[int] = []

    def _flaky() -> tuple[Any, ...]:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("probe exploded")
        return _all_ok_dependencies()

    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _flaky)
    page = BackendSetupPage(_missing_status())

    page.start_detection()

    assert page._phase is InstallPhase.FAILED
    assert page._action_button.get_label() == "Retry"
    assert len(presented) == 1
    assert presented[0].get_heading() == "Detection Failed"

    page._action_button.emit("clicked")

    assert page._phase is InstallPhase.READY
    assert page._action_button.get_label() == "Install box-rpg 26.9.43"


def _output_text(page: Any) -> str:
    """Read the whole install output buffer."""
    buffer = page._output_buffer
    return str(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False))


def test_successful_install_calls_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verified install streams its lines, then hands control to on_ready."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _all_ok_dependencies)
    seen: dict[str, Any] = {}

    def _fake_install(
        tag: str, *, python: str, on_line: Any, on_status: Any = None, clone_urls: Any = None
    ) -> Any:
        seen["tag"] = tag
        seen["python"] = python
        on_line("OK: Linux system detected.")
        on_line("installed 3 shell completions")
        return InstallOutcome(tag=tag, installed_version=tag)

    monkeypatch.setattr(backend_setup_page_module, "install_backend", _fake_install)
    ready: list[int] = []
    page = BackendSetupPage(_missing_status(), on_ready=lambda: ready.append(1))
    page.start_detection()

    page._action_button.emit("clicked")

    assert seen["tag"] == "26.9.43"
    assert seen["python"] == sys.executable
    assert _output_text(page) == "OK: Linux system detected.\ninstalled 3 shell completions\n"
    assert page._phase is InstallPhase.SUCCEEDED
    assert page._action_button.get_label() == "Installed"
    assert page._action_button.get_sensitive() is False
    assert ready == [1]
    assert presented == []


def test_failed_install_shows_retry_with_verbatim_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An install failure keeps its verbatim text and offers Retry."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _all_ok_dependencies)

    def _failing_install(
        tag: str, *, python: str, on_line: Any, on_status: Any = None, clone_urls: Any = None
    ) -> Any:
        on_line("error: Bubblewrap (/usr/bin/bwrap) is required to launch games")
        raise BackendInstallError("error: Bubblewrap (/usr/bin/bwrap) is required to launch games")

    monkeypatch.setattr(backend_setup_page_module, "install_backend", _failing_install)
    ready: list[int] = []
    page = BackendSetupPage(_missing_status(), on_ready=lambda: ready.append(1))
    page.start_detection()

    page._action_button.emit("clicked")

    assert ready == []
    assert page._phase is InstallPhase.FAILED
    assert page._action_button.get_label() == "Retry"
    assert page._action_button.get_sensitive() is True
    assert "Bubblewrap" in _output_text(page)
    assert len(presented) == 1
    assert presented[0].get_heading() == "Installation Failed"
    assert "Bubblewrap" in presented[0].get_body()


def test_restart_failure_stays_on_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restart that cannot exec fails closed on the setup page."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _all_ok_dependencies)
    monkeypatch.setattr(
        backend_setup_page_module,
        "install_backend",
        lambda tag, **kwargs: InstallOutcome(tag=tag, installed_version=tag),
    )

    def _boom() -> None:
        raise OSError("cannot restart")

    page = BackendSetupPage(_missing_status(), on_ready=_boom)
    page.start_detection()

    page._action_button.emit("clicked")

    assert page._phase is InstallPhase.FAILED
    assert page._action_button.get_label() == "Retry"
    assert len(presented) == 1
    assert presented[0].get_heading() == "Unexpected Error"
