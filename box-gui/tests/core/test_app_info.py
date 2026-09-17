"""Unit tests for application version and author metadata (no GTK dependency)."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from typing import Any

import box_gui.core.app_info as app_info_module
from box_gui import __version__
from box_gui.core.app_info import get_app_author, get_app_version


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
