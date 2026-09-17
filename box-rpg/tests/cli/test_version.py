"""Version banner shown by --version, aligned with the GUI footer."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError

import pytest

from box import __version__
from box.cli import version_info
from box.cli.parser import build_parser

_REPOSITORY_URL = "https://gitlab.com/christvh/box-project"


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

    monkeypatch.setattr(version_info, "metadata", lambda _name: _Metadata())
    assert "ChrisTVH" in version_info.version_text()


def test_version_flag_prints_banner(capsys: pytest.CaptureFixture[str]) -> None:
    """--version prints the banner to stdout and exits zero."""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == version_info.version_text()
