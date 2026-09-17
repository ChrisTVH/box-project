"""Reader for the AppImage build tag (Fase 3 GUI side of the contract).

The builder (``tools/build_appimage.py``) stamps every AppImage with the
stable release tag (``year.month.commit-count``, e.g. ``26.9.43``) in two
places: the ``BOX_RPG_MAKER_APPIMAGE_TAG`` environment variable exported by
``AppRun``, and a generated ``appimage_tag.txt`` file sitting next to the
staged ``box_gui`` package inside the AppDir payload. Neither survives as a
git lookup at runtime, so the tag travels with the artifact and this reader
never requires git.

The environment wins so a running AppImage never depends on file layout.
The ``.txt`` file is build output: it lives only inside the staged AppDir,
never in the checkout, and stays uncommitted.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["APPIMAGE_TAG_ENV_VAR", "APPIMAGE_TAG_FILENAME", "get_appimage_tag"]

APPIMAGE_TAG_ENV_VAR = "BOX_RPG_MAKER_APPIMAGE_TAG"
APPIMAGE_TAG_FILENAME = "appimage_tag.txt"


def _sibling_tag_file() -> Path:
    """Return the staged tag file sitting next to this module."""
    return Path(__file__).with_name(APPIMAGE_TAG_FILENAME)


def _read_tag_file(path: Path) -> str | None:
    """Return the stripped tag from a file, or None when unreadable/empty."""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content or None


def get_appimage_tag() -> str | None:
    """Return the embedded AppImage build tag, or None outside an AppImage."""
    from_environment = os.environ.get(APPIMAGE_TAG_ENV_VAR, "").strip()
    if from_environment:
        return from_environment
    return _read_tag_file(_sibling_tag_file())
