"""Unit tests for application version and author metadata (no GTK dependency)."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from typing import Any

import box_gui.appimage_tag as appimage_tag_module
import box_gui.core.app_info as app_info_module
from box_gui import __version__
from box_gui.core.app_info import (
    get_app_author,
    get_app_version,
    get_embedded_tag,
    get_expected_backend_version,
)


def test_version_returns_installed_distribution(monkeypatch: Any) -> None:
    """The installed distribution version wins over the fallback."""
    monkeypatch.setattr(app_info_module, "version", lambda _name: "99.0.1")
    assert get_app_version() == "99.0.1"


def test_version_falls_back_without_distribution(monkeypatch: Any) -> None:
    """A missing distribution falls back to the package version."""

    def _missing(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(app_info_module, "version", _missing)
    assert get_app_version() == __version__


def test_author_returns_distribution_field(monkeypatch: Any) -> None:
    """The distribution Author field is returned verbatim."""
    monkeypatch.setattr(app_info_module, "metadata", lambda _name: {"Author": "Test Author"})
    assert get_app_author() == "Test Author"


def test_author_falls_back_without_distribution(monkeypatch: Any) -> None:
    """A missing distribution falls back to the default author."""

    def _missing(_name: str) -> Any:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(app_info_module, "metadata", _missing)
    assert get_app_author() == "ChrisTVH"


def test_author_falls_back_without_field(monkeypatch: Any) -> None:
    """A missing or empty Author field falls back to the default author."""
    monkeypatch.setattr(app_info_module, "metadata", lambda _name: {})
    assert get_app_author() == "ChrisTVH"
    monkeypatch.setattr(app_info_module, "metadata", lambda _name: {"Author": ""})
    assert get_app_author() == "ChrisTVH"


def test_embedded_tag_reads_the_staged_tag(monkeypatch: Any) -> None:
    """An embedded AppImage tag is reported verbatim."""
    monkeypatch.setattr(appimage_tag_module, "get_appimage_tag", lambda: "26.9.43")

    assert get_embedded_tag() == "26.9.43"


def test_embedded_tag_tolerates_a_missing_reader(monkeypatch: Any) -> None:
    """A broken tag reader means no embedded tag, never a crash."""

    def _boom() -> str:
        raise OSError("unreadable")

    monkeypatch.setattr(appimage_tag_module, "get_appimage_tag", _boom)

    assert get_embedded_tag() is None


def test_expected_backend_prefers_the_embedded_tag(monkeypatch: Any) -> None:
    """An AppImage gates on its embedded tag, not the distribution version."""
    monkeypatch.setattr(appimage_tag_module, "get_appimage_tag", lambda: "26.9.43")
    monkeypatch.setattr(app_info_module, "version", lambda _name: "0.0.0")

    assert get_expected_backend_version() == "26.9.43"


def test_expected_backend_falls_back_to_distribution(monkeypatch: Any) -> None:
    """Dev checkouts without an embedded tag expect the aligned version."""
    monkeypatch.setattr(appimage_tag_module, "get_appimage_tag", lambda: None)
    monkeypatch.setattr(app_info_module, "version", lambda _name: "99.0.1")

    assert get_expected_backend_version() == "99.0.1"
