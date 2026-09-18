"""Version banner shown by --version, aligned with the GUI footer."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError

import pytest

from box import __version__
from box.cli import version_info
from box.cli.parser import build_parser

_REPOSITORY_URL = "https://gitlab.com/christvh/box-project"
_DEV_FALLBACK_VERSION = "0.0.dev0"


def test_version_text_shows_version_author_and_url() -> None:
    """Banner carries the footer info: version, author, repository link."""
    text = version_info.version_text()
    assert text.startswith("box-rpg ")
    assert _REPOSITORY_URL in text


def test_version_text_falls_back_without_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing metadata falls back to the package version and author."""

    def _missing_version(_name: str) -> str:
        raise PackageNotFoundError

    def _missing_metadata(_name: str) -> object:
        raise PackageNotFoundError

    monkeypatch.setattr(version_info, "version", _missing_version)
    monkeypatch.setattr(version_info, "metadata", _missing_metadata)
    assert version_info.version_text() == f"box-rpg {__version__} by ChrisTVH ({_REPOSITORY_URL})"


def test_version_text_falls_back_without_author(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty author metadata falls back to the known author."""

    class _Metadata:
        def get(self, _name: str) -> str:
            return ""

    def _empty_metadata(_name: str) -> _Metadata:
        return _Metadata()

    monkeypatch.setattr(version_info, "metadata", _empty_metadata)
    assert "ChrisTVH" in version_info.version_text()


def test_version_flag_prints_banner(capsys: pytest.CaptureFixture[str]) -> None:
    """--version prints the banner to stdout and exits zero."""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == version_info.version_text()


def test_package_version_format() -> None:
    """Package version is either computed (year.month.commits) or the dev fallback."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__) is not None or (
        __version__ == _DEV_FALLBACK_VERSION
    )


def test_package_version_falls_back_without_generated_file_or_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing _version.py plus failing live computation pin the dev fallback."""
    import importlib
    import sys
    import types

    import box

    stub = types.ModuleType("tools.versioning")

    def _no_git(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("no .git directory found")

    stub.__dict__["compute_version"] = _no_git
    monkeypatch.setitem(sys.modules, "box._version", None)
    monkeypatch.setitem(sys.modules, "tools.versioning", stub)
    try:
        assert importlib.reload(box).__version__ == _DEV_FALLBACK_VERSION
    finally:
        monkeypatch.undo()
        importlib.reload(box)
