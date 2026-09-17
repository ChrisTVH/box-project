"""Fallback reader for the AppImage build tag (Fase 1/2 side of the contract).

The builder (``tools/build_appimage.py``) stamps every AppImage with the
stable release tag (``year.month.commit-count``, e.g. ``26.9.43``) in two
places: the ``BOX_RPG_MAKER_APPIMAGE_TAG`` environment variable exported by
``AppRun``, and a generated ``appimage_tag.txt`` file sitting next to the
staged ``box_gui`` package inside the AppDir payload. Neither survives as a
git lookup at runtime, so the tag must travel with the artifact.

Reader contract for the Fase 3 GUI agent (do not implement it here, and
never import GUI code from ``tools/``):

- Module: ``box_gui.appimage_tag`` (new file, owned by the GUI agent).
- Function: ``def get_appimage_tag() -> str | None``.
- Lookup order: ``BOX_RPG_MAKER_APPIMAGE_TAG`` first, then the sibling
  ``box_gui/appimage_tag.txt`` file (``Path(__file__).with_name(...)``),
  returning the stripped content or ``None`` when absent.
- The generated ``.txt`` file is build output: it lives only inside the
  staged AppDir, never in the checkout, and must stay uncommitted.

This module implements the same lookup without importing ``box_gui`` so CI
and tests can verify a staged AppDir or a live AppImage environment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

APPIMAGE_TAG_ENV_VAR = "BOX_RPG_MAKER_APPIMAGE_TAG"
APPIMAGE_TAG_FILENAME = "appimage_tag.txt"
APPIMAGE_TAG_MODULE = "box_gui.appimage_tag"
APPIMAGE_TAG_FUNCTION = "get_appimage_tag"

_TAG_PATTERN = re.compile(r"\d+\.\d+\.\d+")


def is_valid_tag(tag: str) -> bool:
    """Return whether a tag matches the year.month.commit-count scheme."""
    return _TAG_PATTERN.fullmatch(tag) is not None


def read_tag_file(path: Path) -> str | None:
    """Return the stripped tag from a file, or None when unreadable/empty."""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content or None


def get_appimage_tag(search_from: Path | None = None) -> str | None:
    """Return the build tag from the environment or a staged payload.

    The environment wins so a running AppImage (whose AppRun exports the
    tag) never depends on file layout. ``search_from`` is either the tag
    file itself or a directory holding it; it exists for CI checks of a
    staged AppDir without importing GUI code.
    """
    from_environment = os.environ.get(APPIMAGE_TAG_ENV_VAR, "").strip()
    if from_environment:
        return from_environment
    if search_from is None:
        return None
    candidate = search_from if search_from.is_file() else search_from / APPIMAGE_TAG_FILENAME
    return read_tag_file(candidate)
