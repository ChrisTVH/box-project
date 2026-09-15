"""Game icon discovery and extraction without any GTK dependency.

Windows executables shipped with a game are only ever read as an icon
source; they are never executed. The launcher keeps running its own
managed binaries selected through engine detection.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import gi

gi.require_version("GdkPixbuf", "2.0")

from box.api.launch import list_root_files  # noqa: E402
from box.models import GameInfo  # noqa: E402
from icoextract import IconExtractor, IconExtractorError  # noqa: E402

__all__ = [
    "MAX_ICON_SIDE",
    "extract_icon_png",
    "find_game_executables",
    "icon_path_for_game",
    "install_image_as_icon",
]

MAX_ICON_SIDE: int = 256
"""Longest side kept when writing icon files, to bound their size."""


def find_game_executables(game: GameInfo) -> tuple[Path, ...]:
    """Return .exe filenames from the game root, via list_root_files."""
    try:
        names = list_root_files(game)
    except Exception:
        return ()
    exes = sorted((name for name in names if name.lower().endswith(".exe")), key=str.lower)
    return tuple(game.root / name for name in exes)


def icon_path_for_game(config_root: Path, game_path: Path) -> Path:
    """Return the app-owned cache path for one game icon, creating its directory."""
    cache_dir = config_root / "icons"
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    slug = hashlib.sha256(str(game_path).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{slug}.png"


def extract_icon_png(exe: Path, dest: Path) -> bool:
    """Write the first icon of one executable as PNG, or False on failure."""
    try:
        data = IconExtractor(str(exe)).get_icon().getvalue()
    except IconExtractorError, OSError:
        return False
    return _write_png_from_ico(data, dest)


def install_image_as_icon(source: Path, dest: Path) -> bool:
    """Copy any readable image to dest as PNG, or False on failure."""
    try:
        from gi.repository import GdkPixbuf

        pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(source))
    except Exception:
        return False
    return _save_png(_cap_size(pixbuf), dest)


def _write_png_from_ico(data: bytes, dest: Path) -> bool:
    """Decode `.ico` bytes and write them as PNG, or False on failure."""
    try:
        from gi.repository import GdkPixbuf

        loader = GdkPixbuf.PixbufLoader.new_with_type("ico")
        loader.write(data)
        loader.close()
        pixbuf = loader.get_pixbuf()
        if pixbuf is None:
            return False
    except Exception:
        return False
    return _save_png(_cap_size(pixbuf), dest)


def _cap_size(pixbuf: Any, side: int = MAX_ICON_SIDE) -> Any:
    """Scale a pixbuf down so its longest side fits, keeping aspect."""
    from gi.repository import GdkPixbuf

    width = int(pixbuf.get_width())
    height = int(pixbuf.get_height())
    longest = max(width, height)
    if longest <= side:
        return pixbuf
    factor = side / longest
    return pixbuf.scale_simple(
        max(1, int(width * factor)),
        max(1, int(height * factor)),
        GdkPixbuf.InterpType.BILINEAR,
    )


def _save_png(pixbuf: Any, dest: Path) -> bool:
    """Write one pixbuf as PNG, or False on failure."""
    try:
        pixbuf.savev(str(dest), "png", [], [])
    except Exception:
        return False
    return True
