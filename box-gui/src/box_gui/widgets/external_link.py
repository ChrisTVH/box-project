"""Confirmation dialog before opening an external link.

The library footer links out to project pages, so every navigation goes
through an explicit consent step: an ``Adw.AlertDialog`` shows the literal
URL and only opens it after the user picks Open.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, Gtk  # noqa: E402

from box_gui.i18n import _  # noqa: E402

__all__ = ["confirm_and_open_external_link"]


def confirm_and_open_external_link(parent: Gtk.Window | Gtk.Widget | None, url: str) -> None:
    """Confirm with the user, then open an external URL in the default app.

    The dialog body keeps the URL literal through a ``{url}`` placeholder
    so translators never split the address. On Open the URL launches via
    ``Gtk.UriLauncher`` with a ``Gio.AppInfo`` fallback when the launcher
    class is unavailable; Cancel and close do nothing.
    """
    dialog = Adw.AlertDialog(
        heading=_("Open external link?"),
        body=_("You are about to open {url}.").format(url=url),
    )
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("open", _("Open"))
    dialog.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def _on_response(_source: Adw.AlertDialog, response: str) -> None:
        if response != "open":
            return
        _open_url(url)

    dialog.connect("response", _on_response)
    dialog.present(parent)


def _open_url(url: str) -> None:
    """Open one URL, preferring UriLauncher with a Gio fallback.

    Never raises: launch failures on headless or locked-down systems stay
    silent so the footer cannot crash the library view. The Gio fallback
    only runs when the UriLauncher class itself is unavailable.
    """
    launcher_type = getattr(Gtk, "UriLauncher", None)
    if launcher_type is not None:
        try:
            launcher_type(uri=url).launch()
        except Exception:
            return
        return
    try:
        Gio.AppInfo.launch_default_for_uri(url, None)
    except Exception:
        return
