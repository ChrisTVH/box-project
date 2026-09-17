"""Unit tests for the AppImage build tag reader (no GTK dependency)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import box_gui.appimage_tag as appimage_tag_module
from box_gui.appimage_tag import get_appimage_tag


def test_environment_wins_over_tag_file(monkeypatch: Any, tmp_path: Path) -> None:
    """The AppRun-exported variable wins without touching the filesystem."""
    monkeypatch.setenv(appimage_tag_module.APPIMAGE_TAG_ENV_VAR, "26.9.43")
    monkeypatch.setattr(appimage_tag_module, "_sibling_tag_file", lambda: tmp_path / "missing.txt")

    assert get_appimage_tag() == "26.9.43"


def test_environment_value_is_stripped(monkeypatch: Any, tmp_path: Path) -> None:
    """Surrounding whitespace in the environment never leaks into the tag."""
    monkeypatch.setenv(appimage_tag_module.APPIMAGE_TAG_ENV_VAR, "  26.9.43\n")
    monkeypatch.setattr(appimage_tag_module, "_sibling_tag_file", lambda: tmp_path / "missing.txt")

    assert get_appimage_tag() == "26.9.43"


def test_tag_file_falls_back_when_environment_is_empty(monkeypatch: Any, tmp_path: Path) -> None:
    """The staged sibling file serves checkouts staged into an AppDir."""
    monkeypatch.delenv(appimage_tag_module.APPIMAGE_TAG_ENV_VAR, raising=False)
    tag_file = tmp_path / "appimage_tag.txt"
    tag_file.write_text("26.9.43\n", encoding="utf-8")
    monkeypatch.setattr(appimage_tag_module, "_sibling_tag_file", lambda: tag_file)

    assert get_appimage_tag() == "26.9.43"


def test_missing_file_reports_no_tag(monkeypatch: Any, tmp_path: Path) -> None:
    """Dev checkouts without an embedded tag report None, never a crash."""
    monkeypatch.delenv(appimage_tag_module.APPIMAGE_TAG_ENV_VAR, raising=False)
    monkeypatch.setattr(appimage_tag_module, "_sibling_tag_file", lambda: tmp_path / "missing.txt")

    assert get_appimage_tag() is None


def test_empty_tag_file_reports_no_tag(monkeypatch: Any, tmp_path: Path) -> None:
    """A blank staged file counts as no tag."""
    monkeypatch.delenv(appimage_tag_module.APPIMAGE_TAG_ENV_VAR, raising=False)
    tag_file = tmp_path / "appimage_tag.txt"
    tag_file.write_text("  \n", encoding="utf-8")
    monkeypatch.setattr(appimage_tag_module, "_sibling_tag_file", lambda: tag_file)

    assert get_appimage_tag() is None
