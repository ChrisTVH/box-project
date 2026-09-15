"""Shared game-icon prefix widget for library rows and the detail page.

Resolution order: the cached icon in the app-owned icons directory first,
then the engine Tabler icon, then the generic box icon. Loading
failures fall through silently to the next step.
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gtk  # noqa: E402

try:
    import cairo
except ImportError:
    cairo = None  # type: ignore[no-redef]

from box_gui.core.library import LibraryEntry  # noqa: E402
from box_gui.gtk.icons import BOX_ICON_NAME, EASYRPG_ICON_NAME, NWJS_ICON_NAME  # noqa: E402

__all__ = ["build_game_icon"]

_ENGINE_ICON_NAMES: dict[str, str] = {
    "rpg-maker-2000-2003": EASYRPG_ICON_NAME,
    "rpg-maker-mv": NWJS_ICON_NAME,
    "rpg-maker-mz": NWJS_ICON_NAME,
}

_ROUND_RADIUS_RATIO: float = 0.22
"""Corner radius of file icons as a fraction of the side, like Bottles rows."""


def build_game_icon(entry: LibraryEntry, pixel_size: int = 48) -> Gtk.Widget | None:
    """Build the icon prefix for one library entry, or None when it cannot load."""
    if entry.icon_path is not None:
        file_icon = _file_icon(entry.icon_path, pixel_size)
        if file_icon is not None:
            return file_icon
    fallback = _ENGINE_ICON_NAMES.get(entry.engine or "", BOX_ICON_NAME)
    try:
        image = Gtk.Image.new_from_icon_name(fallback)
    except Exception:
        return None
    image.set_pixel_size(pixel_size)
    image.set_valign(Gtk.Align.CENTER)
    return image


def _display_scale() -> int:
    """Return the highest monitor scale factor, defaulting to one."""
    try:
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        if display is None:
            return 1
        monitors = display.get_monitors()
        scale = 1
        for index in range(monitors.get_n_items()):
            monitor = monitors.get_item(index)
            if monitor is not None:
                scale = max(scale, int(monitor.get_scale_factor()))
        return max(1, scale)
    except Exception:
        return 1


def _file_icon(path: Path, pixel_size: int) -> Gtk.Image | None:
    """Load one PNG file as a rounded image, or None on any failure.

    Like Bottles program rows, file icons render big with rounded
    corners. GTK will not clip image content through CSS, so the
    rounding is baked in with a Cairo mask at device resolution while
    the widget keeps the logical pixel size.
    """
    try:
        from gi.repository import Gdk, GdkPixbuf

        if not path.is_file():
            return None
        target = pixel_size * _display_scale()
        _format, natural_width, natural_height = GdkPixbuf.Pixbuf.get_file_info(str(path))
        if natural_width > 0 and natural_height > 0:
            target = max(1, min(target, natural_width, natural_height))
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), target, target)
        if pixbuf is None:
            return None
        rounded = _rounded_pixbuf(pixbuf)
        texture = Gdk.Texture.new_for_pixbuf(rounded if rounded is not None else pixbuf)
        image = Gtk.Image.new_from_paintable(texture)
    except Exception:
        return None
    image.set_pixel_size(pixel_size)
    image.set_valign(Gtk.Align.CENTER)
    return image


def _rounded_pixbuf(pixbuf: object) -> object | None:
    """Return a copy with rounded corners, or None when Cairo is missing."""
    if cairo is None:
        return None
    try:
        from gi.repository import Gdk
    except Exception:
        return None
    try:
        width = int(pixbuf.get_width())
        height = int(pixbuf.get_height())
        radius = max(1, round(min(width, height) * _ROUND_RADIUS_RATIO))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        context = cairo.Context(surface)
        _rounded_rect_path(context, 0, 0, width, height, radius)
        context.clip()
        Gdk.cairo_set_source_pixbuf(context, pixbuf, 0, 0)
        context.paint()
        surface.flush()
        return Gdk.pixbuf_get_from_surface(surface, 0, 0, width, height)
    except Exception:
        return None


def _rounded_rect_path(
    context: object, x: int, y: int, width: int, height: int, radius: int
) -> None:
    """Append a rounded rectangle path to a Cairo context."""
    import math

    radius = max(1, min(radius, width // 2, height // 2))
    context.new_path()
    context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    context.close_path()
