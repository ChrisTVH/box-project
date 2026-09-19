"""Backend setup navigation page guiding the box-rpg install."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from box_gui.core.appimage_update import (  # noqa: E402
    download_and_verify,
    locate_self,
    replace_self,
    restart_into,
)
from box_gui.core.backend_check import (  # noqa: E402
    BackendStatus,
    DependencyStatus,
    probe_dependencies,
)
from box_gui.core.backend_install import (  # noqa: E402
    BackendInstallError,
    InstallOutcome,
    InstallPhase,
    install_backend,
    install_button_label,
    resolve_expected_commit,
)
from box_gui.core.updates import UpdatesError, asset_urls  # noqa: E402
from box_gui.gtk.icons import (  # noqa: E402
    BOX_ICON_NAME,
    HEART_ICON_NAME,
)
from box_gui.i18n import _  # noqa: E402
from box_gui.widgets.external_link import confirm_and_open_external_link  # noqa: E402

__all__ = ["BackendSetupPage", "describe_status", "missing_required_text"]

# Project home opened from the setup footer version button.
_PROJECT_URL = "https://gitlab.com/christvh/box-project"


def describe_status(status: BackendStatus) -> tuple[str, str]:
    """Return the (heading, body) shown for one backend gate outcome."""
    if status.state == "mismatch":
        return (_("Backend Update Required"), status.message)
    if status.state == "error":
        return (_("Backend Check Failed"), status.message)
    return (_("Backend Required"), status.message)


def missing_required_text(dependencies: tuple[DependencyStatus, ...]) -> str | None:
    """Return the warning for missing required dependencies, if any."""
    missing = [item.label for item in dependencies if item.required and not item.available]
    if not missing:
        return None
    return _("Required dependency missing: {names}.").format(names=", ".join(missing))


def _get_app_version() -> str:
    """Return the app version with a package fallback for dev checkouts."""
    from box_gui.core.app_info import get_app_version

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
    from box_gui.core.app_info import get_app_author

    try:
        author = get_app_author()
    except Exception:
        author = ""
    if author:
        return author
    return "ChrisTVH"


class BackendSetupPage(Adw.NavigationPage):
    """Gate page shown before the library while the backend is unusable.

    Runs dependency detection off the main loop, then offers the single
    Install action cloning the repo at the embedded tag and running its
    install.py with live output. A verified install hands control to the
    ``on_ready`` callback (the application restarts into the library).
    """

    def __init__(
        self,
        status: BackendStatus,
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.set_title(_("Backend Setup"))
        self.set_tag("backend-setup")
        self._status = status
        self._on_ready = on_ready
        self._phase = InstallPhase.CHECKING
        self._dependencies: tuple[DependencyStatus, ...] | None = None
        self._detecting = False
        heading, body = describe_status(status)
        self._heading_label = Gtk.Label(label=heading)
        self._heading_label.add_css_class("title-1")
        self._heading_label.set_wrap(True)
        self._body_label = Gtk.Label(label=body)
        self._body_label.set_wrap(True)
        self._body_label.set_xalign(0.5)
        self._body_label.set_justify(Gtk.Justification.CENTER)
        self._body_label.add_css_class("dim")
        self._dep_group = Adw.PreferencesGroup(title=_("Dependencies"))
        self._dep_rows: list[Adw.ActionRow] = []
        self._output_buffer = Gtk.TextBuffer()
        self._output_view = Gtk.TextView(buffer=self._output_buffer)
        self._output_view.set_editable(False)
        self._output_view.set_cursor_visible(False)
        self._output_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._output_view.set_monospace(True)
        self._output_title: Gtk.Label | None = None
        self._scrolled: Gtk.ScrolledWindow | None = None
        self._status_label = Gtk.Label(label="")
        self._status_label.set_wrap(True)
        self._status_label.add_css_class("dim")
        # Empty means hidden, mirroring _set_status_text; avoids a blank row.
        self._status_label.set_visible(False)
        self._action_button = Gtk.Button()
        self._action_button.add_css_class("suggested-action")
        self._action_button.connect("clicked", self._on_action_clicked)
        # AppImage update mode: a secondary Skip plus a download progress bar.
        # Both stay hidden until start_appimage_update switches the page over.
        self._update_mode = False
        self._latest_tag: str | None = None
        self._update_source: str = "github"
        self._on_skip: Callable[[], None] | None = None
        self._on_update_done: Callable[[], None] | None = None
        self._skip_button = Gtk.Button(label=_("Skip"))
        self._skip_button.set_visible(False)
        self._skip_button.connect("clicked", self._on_skip_clicked)
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_visible(False)
        self._progress_bar.set_show_text(True)
        self._version_button: Gtk.Button | None = None
        self.set_child(self._build_view())
        self._sync_button()
        self.connect("map", self._on_mapped)

    def _build_view(self) -> Adw.ToolbarView:
        """Assemble the header, logo, status, deps, output, action, footer."""
        view = Adw.ToolbarView()
        view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Backend Setup")))
        self._header_bar = header
        view.add_top_bar(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        logo = Gtk.Image.new_from_icon_name(BOX_ICON_NAME)
        logo.set_pixel_size(64)
        logo.set_halign(Gtk.Align.CENTER)
        content.append(logo)
        content.append(self._heading_label)
        content.append(self._body_label)
        content.append(self._dep_group)
        output_title = Gtk.Label(label=_("Installation output"))
        output_title.set_xalign(0.0)
        # Hidden until the install streams; keeps the action button in view.
        output_title.set_visible(False)
        self._output_title = output_title
        content.append(output_title)
        self._output_view.set_hexpand(True)
        self._output_view.set_vexpand(True)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(220)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        scrolled.set_child(self._output_view)
        # Hidden until the install streams; keeps the action button in view.
        scrolled.set_visible(False)
        self._scrolled = scrolled
        content.append(scrolled)
        content.append(self._status_label)
        content.append(self._progress_bar)
        action_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        action_box.set_halign(Gtk.Align.CENTER)
        action_box.append(self._action_button)
        action_box.append(self._skip_button)
        content.append(action_box)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        clamp = Adw.Clamp(child=content)
        outer = Gtk.ScrolledWindow()
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.set_child(clamp)
        view.set_content(outer)
        view.add_bottom_bar(self._build_footer())
        return view

    def _build_footer(self) -> Gtk.Widget:
        """Build the fixed footer with a centered credit and right version."""
        version = _get_app_version()
        author = _get_app_author()
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
        heart = Gtk.Image.new_from_icon_name(HEART_ICON_NAME)
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
        bar.set_center_widget(message)
        bar.set_end_widget(version_button)
        footer.append(bar)
        self._version_button = version_button
        return footer

    def _on_version_clicked(self, _button: Gtk.Button) -> None:
        """Confirm, then open the project repository URL."""
        confirm_and_open_external_link(self, _PROJECT_URL)

    def _update_button_label(self) -> str:
        """Return the action label while in AppImage update mode."""
        if self._phase is InstallPhase.INSTALLING:
            return _("Updating…")
        if self._phase is InstallPhase.FAILED:
            return _("Retry")
        if self._phase is InstallPhase.SUCCEEDED:
            return _("Updated")
        return _("Update AppImage")

    def _sync_button(self) -> None:
        """Reflect the install phase on the single action button."""
        if self._update_mode:
            self._action_button.set_label(self._update_button_label())
        else:
            self._action_button.set_label(
                install_button_label(self._phase, self._status.expected_version)
            )
        self._action_button.set_sensitive(self._phase in (InstallPhase.READY, InstallPhase.FAILED))

    def _set_phase(self, phase: InstallPhase) -> None:
        """Move to one install phase, syncing the action button."""
        self._phase = phase
        self._sync_button()

    def _set_status_text(self, message: str) -> None:
        """Show one transient progress or result line under the output."""
        self._status_label.set_text(message)
        self._status_label.set_visible(bool(message))

    def _on_mapped(self, _widget: Gtk.Widget) -> None:
        """Start dependency detection once the page becomes visible."""
        if self._update_mode:
            return
        self.start_detection()

    def start_detection(self) -> None:
        """Probe dependencies off the main loop, enabling Install when done."""
        if self._update_mode:
            return
        if self._detecting or self._phase is InstallPhase.INSTALLING:
            return
        if self._dependencies is not None and self._phase not in (
            InstallPhase.CHECKING,
            InstallPhase.FAILED,
        ):
            return
        try:
            from box_gui.gtk.threads import run_in_thread
        except ImportError as exc:
            self._detecting = False
            self.show_error(str(exc) or exc.__class__.__name__, unexpected=True)
            return
        self._detecting = True
        self._set_phase(InstallPhase.CHECKING)
        run_in_thread(probe_dependencies, self._on_detection_done, self._on_detection_error)

    def _render_dependencies(self, dependencies: tuple[DependencyStatus, ...]) -> None:
        """Rebuild the dependency rows for freshly probed statuses."""
        for row in self._dep_rows:
            self._dep_group.remove(row)
        self._dep_rows = []
        for item in dependencies:
            row = Adw.ActionRow(title=item.label, subtitle=item.detail)
            mark = Gtk.Label(label="✓" if item.available else "✗")
            mark.add_css_class("success" if item.available else "error")
            mark.set_valign(Gtk.Align.CENTER)
            row.add_suffix(mark)
            self._dep_group.add(row)
            self._dep_rows.append(row)

    def _on_detection_done(self, dependencies: tuple[DependencyStatus, ...]) -> None:
        """Enable Install once detection completes, warning about gaps."""
        if self._update_mode:
            return
        self._detecting = False
        self._dependencies = dependencies
        self._render_dependencies(dependencies)
        warning = missing_required_text(dependencies)
        if warning is not None:
            self._set_status_text(warning)
        else:
            self._set_status_text("")
        self._set_phase(InstallPhase.READY)

    def _on_detection_error(self, error: BaseException) -> None:
        """Fail closed when detection itself breaks; Retry re-runs it."""
        if self._update_mode:
            return
        self._detecting = False
        message = str(error) or error.__class__.__name__
        self._set_status_text(message)
        self._set_phase(InstallPhase.FAILED)
        self._show_alert(_("Detection Failed"), message)

    def _on_action_clicked(self, _button: Gtk.Button) -> None:
        """Retry detection when it never completed, else start the install."""
        if self._update_mode:
            if self._phase is InstallPhase.INSTALLING:
                return
            self._start_appimage_download()
            return
        if self._dependencies is None:
            self.start_detection()
            return
        self._start_install()

    def _start_install(self) -> None:
        """Clone at the embedded tag and install, streaming output live."""
        import sys

        try:
            from box_gui.gtk.threads import run_in_thread
        except ImportError as exc:
            self.show_error(str(exc) or exc.__class__.__name__, unexpected=True)
            return
        tag = self._status.expected_version
        python = sys.executable
        self._output_buffer.set_text("")
        # Reveal the log for the whole install; success restarts, failure keeps it.
        # The requirement list already served its purpose, so it steps aside
        # and leaves the streaming output, status, and action in view.
        self._dep_group.set_visible(False)
        if self._output_title is not None:
            self._output_title.set_visible(True)
        if self._scrolled is not None:
            self._scrolled.set_visible(True)
        self._set_status_text(_("Installing box-rpg {tag} …").format(tag=tag))
        self._set_phase(InstallPhase.INSTALLING)

        def _work() -> InstallOutcome:
            # Resolve the pin on the worker: git ls-remote needs the network
            # and must never block the main loop. A failed resolve degrades
            # to no pin rather than guessing a commit.
            try:
                expected_commit = resolve_expected_commit(tag)
            except Exception:
                expected_commit = None
            return install_backend(
                tag,
                python=python,
                on_line=lambda line: GLib.idle_add(self._append_output_line, line),
                on_status=lambda message: GLib.idle_add(self._set_status_text, message),
                expected_commit=expected_commit,
            )

        run_in_thread(_work, self._on_install_done, self._on_install_error)

    def _append_output_line(self, line: str) -> None:
        """Append one verbatim install.py line, keeping the end visible."""
        end = self._output_buffer.get_end_iter()
        self._output_buffer.insert(end, line + "\n")
        GLib.idle_add(self._scroll_output_to_end)

    def _scroll_output_to_end(self) -> bool:
        """Scroll the output view to its last line; one-shot idle source."""
        scrolled = self._scrolled
        if scrolled is not None:
            adjustment = scrolled.get_vadjustment()
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        return False

    def _on_install_done(self, outcome: InstallOutcome) -> None:
        """Hand a verified install to the application restart callback."""
        self._set_phase(InstallPhase.SUCCEEDED)
        self._set_status_text(
            _("box-rpg {version} installed and verified.").format(version=outcome.installed_version)
        )
        if self._on_ready is None:
            return
        try:
            self._on_ready()
        except Exception as exc:
            self.show_error(str(exc) or exc.__class__.__name__, unexpected=True)

    def _on_install_error(self, error: BaseException) -> None:
        """Show install failures verbatim, offering Retry on the button."""
        message = str(error) or error.__class__.__name__
        self.show_error(message, unexpected=not isinstance(error, BackendInstallError))

    def start_appimage_update(
        self,
        latest_tag: str,
        on_done: Callable[[], None] | None = None,
        on_skip: Callable[[], None] | None = None,
        *,
        current_tag: str | None = None,
        source: str = "github",
    ) -> None:
        """Switch the page to AppImage update mode for one latest tag.

        Reuses the detection area: the heading becomes "AppImage Update
        Available", the body names both versions with a "v" prefix, the
        action button offers "Update AppImage", and a Skip button appears
        below it. The caller
        owns persistence: skipping must record the skipped version plus
        the check time, while the download chain replaces the running
        AppImage and restarts so the fresh process owns the backend gate.
        The ``source`` is the discovery origin (``"github"`` or
        ``"gitlab"``); downloads always use the GitHub release since GitLab
        serves discovery fallback only.
        """
        current = current_tag
        if current is None:
            try:
                from box_gui.core.app_info import get_embedded_tag

                current = get_embedded_tag()
            except Exception:
                current = None
        if not current:
            current = self._status.expected_version
        self._update_mode = True
        self._latest_tag = latest_tag
        self._update_source = source if source in ("github", "gitlab") else "github"
        self._on_skip = on_skip
        self._on_update_done = on_done
        self._phase = InstallPhase.READY
        self._heading_label.set_text(_("AppImage Update Available"))
        self._body_label.set_text(
            _(
                "AppImage {current} → {latest}.\nUpdate to get the latest features and fixes."
            ).format(current=f"v{current}", latest=f"v{latest_tag}")
        )
        self._dep_group.set_visible(False)
        self._progress_bar.set_visible(False)
        self._progress_bar.set_fraction(0.0)
        self._skip_button.set_visible(True)
        self._set_status_text("")
        self._set_phase(InstallPhase.READY)

    def _on_skip_clicked(self, _button: Gtk.Button) -> None:
        """Forward Skip to the caller, which records the skipped version."""
        callback = self._on_skip
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:
            self.show_error(str(exc) or exc.__class__.__name__, unexpected=True)

    def _update_progress(self, completed: int, total: int | None) -> None:
        """Reflect download progress on the bar; idle-dispatched, one-shot."""
        bar = self._progress_bar
        bar.set_visible(True)
        if total is not None and total > 0:
            try:
                fraction = min(1.0, max(0.0, completed / total))
            except ValueError, ZeroDivisionError:
                fraction = 0.0
            bar.set_fraction(fraction)
            bar.set_text(f"{completed} / {total}")
        else:
            bar.pulse()
            bar.set_text(f"{completed}")

    def _start_appimage_download(self) -> None:
        """Download, verify, replace, and restart into the latest AppImage."""
        try:
            from box_gui.gtk.threads import run_in_thread
        except ImportError as exc:
            self.show_error(str(exc) or exc.__class__.__name__, unexpected=True)
            return
        latest = self._latest_tag
        if latest is None:
            self.show_error(_("Missing update version."), unexpected=True)
            return
        try:
            target_dir = locate_self().parent
        except UpdatesError as exc:
            self.show_error(str(exc) or exc.__class__.__name__)
            return
        try:
            appimage_url, sha256_url = asset_urls(latest, self._update_source)
        except ValueError as exc:
            self.show_error(str(exc) or exc.__class__.__name__)
            return
        self._output_buffer.set_text("")
        self._dep_group.set_visible(False)
        if self._output_title is not None:
            self._output_title.set_visible(True)
        if self._scrolled is not None:
            self._scrolled.set_visible(True)
        self._progress_bar.set_visible(True)
        self._progress_bar.set_fraction(0.0)
        self._set_status_text(_("Downloading AppImage {tag} …").format(tag=latest))
        GLib.idle_add(self._append_output_line, f"Downloading {appimage_url}")
        self._set_phase(InstallPhase.INSTALLING)

        def _work() -> Path:
            def _thread_progress(completed: int, total: int | None) -> None:
                GLib.idle_add(self._update_progress, completed, total)

            return download_and_verify(
                appimage_url,
                sha256_url,
                target_dir,
                progress=_thread_progress,
                timeout=60.0,
            )

        run_in_thread(_work, self._on_appimage_done, self._on_appimage_error)

    def _on_appimage_done(self, new_file: Path) -> None:
        """Swap in the verified AppImage and restart into it."""
        GLib.idle_add(self._append_output_line, f"Verified {new_file.name}")
        self._set_status_text(_("Replacing AppImage …"))
        try:
            replace_self(new_file)
        except UpdatesError as exc:
            self.show_error(str(exc) or exc.__class__.__name__)
            return
        except Exception as exc:
            self.show_error(str(exc) or exc.__class__.__name__, unexpected=True)
            return
        self._set_phase(InstallPhase.SUCCEEDED)
        self._progress_bar.set_fraction(1.0)
        self._set_status_text(_("AppImage updated, restarting …"))
        callback = self._on_update_done
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                self.show_error(str(exc) or exc.__class__.__name__, unexpected=True)
                return
        try:
            target = locate_self()
        except UpdatesError as exc:
            self.show_error(str(exc) or exc.__class__.__name__)
            return
        try:
            restart_into(target)
        except Exception as exc:
            self.show_error(str(exc) or exc.__class__.__name__, unexpected=True)

    def _on_appimage_error(self, error: BaseException) -> None:
        """Show AppImage download failures, offering Retry on the button."""
        message = str(error) or error.__class__.__name__
        unexpected = not isinstance(error, (BackendInstallError, UpdatesError))
        self.show_error(message, unexpected=unexpected)

    def show_error(self, message: str, *, unexpected: bool = False) -> None:
        """Surface a failure (install, detection import, or restart) verbatim."""
        if self._update_mode:
            heading = _("Unexpected Error") if unexpected else _("Update Failed")
        else:
            heading = _("Unexpected Error") if unexpected else _("Installation Failed")
        self._set_status_text(message)
        self._set_phase(InstallPhase.FAILED)
        self._show_alert(heading, message)

    def _show_alert(self, heading: str, body: str) -> None:
        """Present a single-close alert over this page."""
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present(self)
