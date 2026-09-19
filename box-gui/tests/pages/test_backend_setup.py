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
        DependencyStatus(
            key="icoextract", label="icoextract", available=True, required=False, detail="available"
        ),
    )


def _install_sync_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve synchronous fake workers plus immediate idle dispatch."""
    import box_gui.gtk as gtk_package

    module = types.ModuleType("box_gui.gtk.threads")

    def _run_in_thread(fn: Any, on_done: Any, on_error: Any) -> None:
        try:
            result = fn()
        except BaseException as exc:
            on_error(exc)
        else:
            on_done(result)
        return None

    module.run_in_thread = _run_in_thread  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "box_gui.gtk.threads", module)
    monkeypatch.setattr(gtk_package, "threads", module, raising=False)

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
        "icoextract",
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
        tag: str,
        *,
        python: str,
        on_line: Any,
        on_status: Any = None,
        clone_urls: Any = None,
        expected_commit: Any = None,
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
        tag: str,
        *,
        python: str,
        on_line: Any,
        on_status: Any = None,
        clone_urls: Any = None,
        expected_commit: Any = None,
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


def test_output_hidden_during_checking_and_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """The empty install log stays hidden so the action button keeps its place."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _all_ok_dependencies)
    page = BackendSetupPage(_missing_status())

    assert page._phase is InstallPhase.CHECKING
    assert page._output_title is not None
    assert page._scrolled is not None
    assert page._output_title.get_visible() is False
    assert page._scrolled.get_visible() is False
    assert page._dep_group.get_visible() is True

    page.start_detection()

    assert page._phase is InstallPhase.READY
    assert page._output_title.get_visible() is False
    assert page._scrolled.get_visible() is False
    assert page._dep_group.get_visible() is True


def test_output_visible_once_install_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting the install reveals the log so streamed lines stay at hand."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _all_ok_dependencies)
    monkeypatch.setattr(
        backend_setup_page_module,
        "install_backend",
        lambda tag, **kwargs: InstallOutcome(tag=tag, installed_version=tag),
    )
    page = BackendSetupPage(_missing_status(), on_ready=lambda: None)
    page.start_detection()

    assert page._output_title.get_visible() is False
    assert page._scrolled.get_visible() is False
    assert page._dep_group.get_visible() is True

    page._action_button.emit("clicked")

    assert page._output_title.get_visible() is True
    assert page._scrolled.get_visible() is True
    assert page._dep_group.get_visible() is False


def test_output_stays_visible_after_failed_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed install keeps the revealed log visible for diagnosis."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _all_ok_dependencies)

    def _failing_install(
        tag: str,
        *,
        python: str,
        on_line: Any,
        on_status: Any = None,
        clone_urls: Any = None,
        expected_commit: Any = None,
    ) -> Any:
        on_line("error: install broke")
        raise BackendInstallError("error: install broke")

    monkeypatch.setattr(backend_setup_page_module, "install_backend", _failing_install)
    page = BackendSetupPage(_missing_status(), on_ready=lambda: None)
    page.start_detection()

    page._action_button.emit("clicked")

    assert page._phase is InstallPhase.FAILED
    assert page._output_title.get_visible() is True
    assert page._scrolled.get_visible() is True
    assert page._dep_group.get_visible() is False


def _update_status() -> Any:
    """Build a compatible gate status for the AppImage update flow."""
    return BackendStatus(
        state="compatible",
        expected_version="26.9.43",
        installed_version="26.9.43",
        message="box-rpg 26.9.43 is ready.",
    )


def test_update_mode_labels_and_skip_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Update mode reuses the page with versioned copy plus a Skip button."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    page = BackendSetupPage(_update_status())

    page.start_appimage_update("26.9.44", lambda: None, lambda: None, current_tag="26.9.43")

    assert page._heading_label.get_text() == "AppImage Update Available"
    assert "26.9.43" in page._body_label.get_text()
    assert "26.9.44" in page._body_label.get_text()
    assert page._action_button.get_label() == "Update AppImage"
    assert page._action_button.get_sensitive() is True
    assert page._skip_button.get_visible() is True
    assert page._skip_button.get_label() == "Skip"
    assert page._progress_bar.get_visible() is False
    assert page._dep_group.get_visible() is False
    assert page._phase is InstallPhase.READY


def test_update_mode_infers_current_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an explicit current tag the body still names both versions."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    page = BackendSetupPage(_update_status())

    page.start_appimage_update("26.9.44", lambda: None, lambda: None)

    assert "26.9.44" in page._body_label.get_text()
    assert page._action_button.get_label() == "Update AppImage"


def test_update_skip_calls_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip forwards to the caller, which records the skipped version."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    page = BackendSetupPage(_update_status())
    skipped: list[str] = []
    page.start_appimage_update(
        "26.9.44", lambda: None, lambda: skipped.append("26.9.44"), current_tag="26.9.43"
    )

    page._skip_button.emit("clicked")

    assert skipped == ["26.9.44"]
    # Skipping never fails the page itself.
    assert page._phase is InstallPhase.READY


def test_update_skip_without_callback_stays_put(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing skip callback is a no-op instead of a crash."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    page = BackendSetupPage(_update_status())
    page.start_appimage_update("26.9.44", current_tag="26.9.43")

    page._skip_button.emit("clicked")

    assert page._phase is InstallPhase.READY


def test_update_mode_skips_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detection stays off in update mode; the action downloads instead."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _all_ok_dependencies)
    page = BackendSetupPage(_update_status())
    page.start_appimage_update("26.9.44", lambda: None, lambda: None, current_tag="26.9.43")

    page.start_detection()

    assert page._dependencies is None
    assert page._phase is InstallPhase.READY


def test_update_download_replace_restart_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The action downloads, replaces, and restarts with stubbed helpers."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    from pathlib import Path as _Path

    running = tmp_path / "box.AppImage"
    running.write_bytes(b"old")
    staged = tmp_path / "box-rpg-maker.appimage.part"
    staged.write_bytes(b"new")
    monkeypatch.setattr(backend_setup_page_module, "locate_self", lambda: running)
    seen_download: dict[str, Any] = {}

    def _fake_download(
        appimage_url: str,
        sha256_url: str,
        dest_dir: Any,
        *,
        progress: Any = None,
        timeout: Any = None,
    ) -> Any:
        seen_download["appimage_url"] = appimage_url
        seen_download["sha256_url"] = sha256_url
        seen_download["dest_dir"] = _Path(dest_dir)
        seen_download["timeout"] = timeout
        assert progress is not None
        progress(5, 10)
        return staged

    replaced: list[Any] = []
    restarted: list[Any] = []
    monkeypatch.setattr(backend_setup_page_module, "download_and_verify", _fake_download)
    monkeypatch.setattr(
        backend_setup_page_module, "replace_self", lambda path: replaced.append(_Path(path))
    )
    monkeypatch.setattr(
        backend_setup_page_module, "restart_into", lambda path: restarted.append(str(path))
    )
    done: list[int] = []
    page = BackendSetupPage(_update_status())
    page.start_appimage_update(
        "26.9.44", lambda: done.append(1), lambda: None, current_tag="26.9.43"
    )

    page._action_button.emit("clicked")

    assert "26.9.44" in seen_download["appimage_url"]
    assert seen_download["sha256_url"] == f"{seen_download['appimage_url']}.sha256"
    assert seen_download["dest_dir"] == tmp_path
    assert replaced == [staged]
    assert restarted == [str(running)]
    assert done == [1]
    assert page._phase is InstallPhase.SUCCEEDED
    assert page._action_button.get_label() == "Updated"
    assert page._progress_bar.get_visible() is True
    assert page._progress_bar.get_fraction() == 1.0


def test_update_progress_bar_reflects_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Progress callbacks land on the bar through the main loop."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    from pathlib import Path as _Path

    running = tmp_path / "box.AppImage"
    running.write_bytes(b"old")
    staged = tmp_path / "box-rpg-maker.appimage.part"
    staged.write_bytes(b"new")
    monkeypatch.setattr(backend_setup_page_module, "locate_self", lambda: running)

    def _fake_download(
        appimage_url: str,
        sha256_url: str,
        dest_dir: Any,
        *,
        progress: Any = None,
        timeout: Any = None,
    ) -> Any:
        assert progress is not None
        progress(5, 10)
        assert page._progress_bar.get_fraction() == pytest.approx(0.5)
        progress(10, 10)
        return staged

    monkeypatch.setattr(backend_setup_page_module, "download_and_verify", _fake_download)
    monkeypatch.setattr(backend_setup_page_module, "replace_self", lambda _path: None)
    monkeypatch.setattr(backend_setup_page_module, "restart_into", lambda _path: None)
    page = BackendSetupPage(_update_status())
    page.start_appimage_update("26.9.44", lambda: None, lambda: None, current_tag="26.9.43")

    page._action_button.emit("clicked")

    assert page._phase is InstallPhase.SUCCEEDED
    assert _Path(running).read_bytes() == b"old"


def test_update_download_error_offers_retry(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """A failed download fails closed with an Update Failed alert."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    running = tmp_path / "box.AppImage"
    running.write_bytes(b"old")
    monkeypatch.setattr(backend_setup_page_module, "locate_self", lambda: running)
    from box_gui.core.updates import UpdatesError

    def _boom(
        appimage_url: str,
        sha256_url: str,
        dest_dir: Any,
        *,
        progress: Any = None,
        timeout: Any = None,
    ) -> Any:
        raise UpdatesError("checksum mismatch for https://example.invalid/appimage")

    monkeypatch.setattr(backend_setup_page_module, "download_and_verify", _boom)
    page = BackendSetupPage(_update_status())
    page.start_appimage_update("26.9.44", lambda: None, lambda: None, current_tag="26.9.43")

    page._action_button.emit("clicked")

    assert page._phase is InstallPhase.FAILED
    assert page._action_button.get_label() == "Retry"
    assert page._action_button.get_sensitive() is True
    assert len(presented) == 1
    assert presented[0].get_heading() == "Update Failed"
    assert "checksum mismatch" in presented[0].get_body()


def test_detection_done_ignored_in_update_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Late detection results never clobber an active AppImage update prompt."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    page = BackendSetupPage(_update_status())
    page.start_appimage_update("26.9.44", lambda: None, lambda: None, current_tag="26.9.43")

    page._on_detection_done(_all_ok_dependencies())

    assert page._update_mode is True
    assert page._dependencies is None
    assert page._phase is InstallPhase.READY
    assert page._action_button.get_label() == "Update AppImage"
    assert presented == []


def test_detection_error_ignored_in_update_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Late detection errors never fail an active AppImage update prompt."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    page = BackendSetupPage(_update_status())
    page.start_appimage_update("26.9.44", lambda: None, lambda: None, current_tag="26.9.43")

    page._on_detection_error(OSError("probe exploded"))

    assert page._phase is InstallPhase.READY
    assert page._action_button.get_label() == "Update AppImage"
    assert presented == []


def test_install_passes_resolved_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The GUI resolves the tag commit and passes it as expected_commit."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _all_ok_dependencies)
    pin = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"
    monkeypatch.setattr(backend_setup_page_module, "resolve_expected_commit", lambda tag: pin)
    seen: dict[str, Any] = {}

    def _fake_install(
        tag: str,
        *,
        python: str,
        on_line: Any,
        on_status: Any = None,
        clone_urls: Any = None,
        expected_commit: Any = None,
    ) -> Any:
        seen["expected_commit"] = expected_commit
        return InstallOutcome(tag=tag, installed_version=tag)

    monkeypatch.setattr(backend_setup_page_module, "install_backend", _fake_install)
    page = BackendSetupPage(_missing_status(), on_ready=lambda: None)
    page.start_detection()

    page._action_button.emit("clicked")

    assert seen["expected_commit"] == pin
    assert page._phase is InstallPhase.SUCCEEDED


def test_install_degrades_without_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed commit resolve still installs, without a pin."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    monkeypatch.setattr(backend_setup_page_module, "probe_dependencies", _all_ok_dependencies)

    def _boom(tag: str) -> str | None:
        raise OSError("offline")

    monkeypatch.setattr(backend_setup_page_module, "resolve_expected_commit", _boom)
    seen: dict[str, Any] = {}

    def _fake_install(
        tag: str,
        *,
        python: str,
        on_line: Any,
        on_status: Any = None,
        clone_urls: Any = None,
        expected_commit: Any = None,
    ) -> Any:
        seen["expected_commit"] = expected_commit
        return InstallOutcome(tag=tag, installed_version=tag)

    monkeypatch.setattr(backend_setup_page_module, "install_backend", _fake_install)
    page = BackendSetupPage(_missing_status(), on_ready=lambda: None)
    page.start_detection()

    page._action_button.emit("clicked")

    assert seen["expected_commit"] is None
    assert page._phase is InstallPhase.SUCCEEDED


def test_update_gitlab_source_still_downloads_from_github(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """GitLab discovery still downloads the GitHub release asset."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_sync_workers(monkeypatch)
    running = tmp_path / "box.AppImage"
    running.write_bytes(b"old")
    staged = tmp_path / "box-rpg-maker.appimage.part"
    staged.write_bytes(b"new")
    monkeypatch.setattr(backend_setup_page_module, "locate_self", lambda: running)
    seen_download: dict[str, Any] = {}

    def _fake_download(
        appimage_url: str,
        sha256_url: str,
        dest_dir: Any,
        *,
        progress: Any = None,
        timeout: Any = None,
    ) -> Any:
        seen_download["appimage_url"] = appimage_url
        return staged

    monkeypatch.setattr(backend_setup_page_module, "download_and_verify", _fake_download)
    monkeypatch.setattr(backend_setup_page_module, "replace_self", lambda path: None)
    monkeypatch.setattr(backend_setup_page_module, "restart_into", lambda path: None)
    page = BackendSetupPage(_update_status())
    page.start_appimage_update(
        "26.9.44", lambda: None, lambda: None, current_tag="26.9.43", source="gitlab"
    )

    assert page._update_source == "gitlab"

    page._action_button.emit("clicked")

    assert seen_download["appimage_url"].startswith("https://github.com/")
    assert "26.9.44" in seen_download["appimage_url"]
    assert page._phase is InstallPhase.SUCCEEDED
