"""Game-icon behavior without the optional icoextract extra (no display needed)."""

# pyright: reportMissingImports=false

from __future__ import annotations

import sys
from pathlib import Path

import pytest

gi = pytest.importorskip("gi", reason="gi unavailable")
gi.require_version("GdkPixbuf", "2.0")
pytest.importorskip("gi.repository.GdkPixbuf", reason="GdkPixbuf typelib unavailable")


def _block_icoextract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Purge the icon module so its icoextract import fails like a bare setup."""
    dotted = "box_gui.core.game_icon"
    if dotted in sys.modules:
        monkeypatch.delitem(sys.modules, dotted, raising=False)
    parent = sys.modules.get("box_gui.core")
    if parent is not None:
        # Reimporting a purged child must rebind the parent attribute too:
        # otherwise teardown restores a stale sys.modules entry while the
        # parent keeps the fresh module, splitting the package state.
        monkeypatch.delattr(parent, "game_icon", raising=False)
    monkeypatch.setitem(sys.modules, "icoextract", None)


def test_icon_module_imports_without_icoextract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The library import must survive a missing icoextract extra."""
    _block_icoextract(monkeypatch)

    import box_gui.core.game_icon as game_icon_module

    assert game_icon_module.IconExtractor is None
    assert "box_gui.core.game_icon" in sys.modules


def test_exe_extraction_degrades_to_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without icoextract, extraction reports False instead of raising."""
    _block_icoextract(monkeypatch)

    from box_gui.core.game_icon import extract_icon_png

    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    dest = tmp_path / "icon.png"

    assert extract_icon_png(exe, dest) is False
    assert not dest.exists()
