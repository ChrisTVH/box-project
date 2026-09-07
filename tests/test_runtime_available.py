from pathlib import Path

import pytest

from box.errors import RuntimeError
from box.paths import AppPaths
from box.runtime.available import AvailableVersions, available_url, parse_versions


def test_available_url_uses_pages_of_five_versions() -> None:
    assert available_url(2) == "https://dl.nwjs.io/"


def test_available_url_rejects_invalid_pages() -> None:
    with pytest.raises(RuntimeError, match="at least 1"):
        available_url(0)


def test_parse_versions_filters_unstable_and_invalid_directories() -> None:
    content = """
    <a href="v0.90.0/">v0.90.0/</a>
    <a href="v0.91.0-beta.1/">v0.91.0-beta.1/</a>
    <a href="v0.89.0/">v0.89.0/</a>
    <a href="not-a-version/">not-a-version/</a>
    """

    assert parse_versions(content) == ("v0.90.0", "v0.89.0")


def test_parse_versions_ignores_an_index_without_version_directories() -> None:
    assert parse_versions("<html><body>empty</body></html>") == ()


def test_interactive_selection_installs_the_chosen_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.cli import runtime

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    installed: list[tuple[str, str, bool]] = []
    output: list[str] = []
    choices = iter(("2", "yes"))

    def fetch(page: int, architecture: str, sdk: bool) -> AvailableVersions:
        assert architecture == "x64"
        assert not sdk
        return AvailableVersions(page=page, versions=("v0.90.0", "v0.89.0"))

    def install_version(_: AppPaths, version: str, architecture: str, sdk: bool) -> int:
        installed.append((version, architecture, sdk))
        return 0

    monkeypatch.setattr("box.cli.runtime.fetch_available_versions", fetch)
    monkeypatch.setattr("box.cli.runtime.install", install_version)

    result = runtime.select_interactively(
        paths,
        1,
        "x64",
        False,
        read=lambda _: next(choices),
        write=output.append,
    )

    assert result == 0
    assert installed == [("v0.89.0", "x64", False)]
    assert any("page 1, x64, standard" in line for line in output)


def test_interactive_selection_uses_five_item_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.cli import runtime

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    pages: list[int] = []
    choices = iter(("n", "q"))

    def fetch(page: int, architecture: str, sdk: bool) -> AvailableVersions:
        assert architecture == "x64"
        assert not sdk
        pages.append(page)
        return AvailableVersions(page=page, versions=("v0.90.0",))

    monkeypatch.setattr("box.cli.runtime.fetch_available_versions", fetch)

    assert runtime.select_interactively(paths, 1, "x64", False, read=lambda _: next(choices)) == 0
    assert pages == [1, 2]


def test_interactive_selection_handles_end_of_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.cli import runtime

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    output: list[str] = []

    def fetch(page: int, architecture: str, sdk: bool) -> AvailableVersions:
        assert architecture == "x64"
        assert not sdk
        return AvailableVersions(page=page, versions=("v0.90.0",))

    def end_of_input(_: str) -> str:
        raise EOFError

    monkeypatch.setattr("box.cli.runtime.fetch_available_versions", fetch)

    assert (
        runtime.select_interactively(paths, 1, "x64", False, read=end_of_input, write=output.append)
        == 0
    )
    assert output[-1] == "Selection cancelled."
