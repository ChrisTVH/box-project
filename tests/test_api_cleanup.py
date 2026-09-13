"""Tests for the no-I/O cleanup API."""

from __future__ import annotations

from pathlib import Path

import pytest

from box.api import cleanup as api_cleanup
from box.config.repository import ConfigRepository
from box.paths import AppPaths


def _paths(tmp_path: Path) -> AppPaths:
    """Build isolated launcher paths inside a temporary directory."""
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


def _make_root(paths: AppPaths, tmp_path: Path) -> tuple[ConfigRepository, Path]:
    """Authorize one game root and return its repository and path."""
    root = tmp_path / "games" / "sample"
    root.mkdir(parents=True)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(root)
    return repository, root


def _make_nwjs_runtime(paths: AppPaths) -> Path:
    """Create one valid fake NW.js runtime with an executable."""
    root = paths.runtimes_root / "linux-x64" / "standard-v0.90.0"
    root.mkdir(parents=True)
    executable = root / "nw"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    return root


def _make_easyrpg_runtime(paths: AppPaths) -> Path:
    """Create one valid fake EasyRPG Player runtime with an executable."""
    root = paths.easyrpg_runtimes_root / "0.8.1"
    root.mkdir(parents=True)
    player = root / "easyrpg-player"
    player.write_text("#!/bin/sh\n", encoding="utf-8")
    player.chmod(0o700)
    return root


def _make_downloads(paths: AppPaths) -> tuple[Path, Path]:
    """Create one NW.js and one EasyRPG download archive."""
    paths.ensure()
    nwjs = paths.downloads_root / "standard-v0.90.0-linux-x64.tar.gz"
    easyrpg = paths.easyrpg_downloads_root / "easyrpg-player-0.8.1-linux.tar.gz"
    nwjs.write_bytes(b"archive")
    easyrpg.write_bytes(b"archive")
    return nwjs, easyrpg


def _make_profile(paths: AppPaths) -> Path:
    """Create one persistent game profile directory."""
    paths.ensure()
    profile = paths.profiles_root / "0123456789abcdef"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def test_list_roots_returns_authorized_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    repository, root = _make_root(paths, tmp_path)
    catalog = api_cleanup.CleanupCatalog(paths, repository)

    items = catalog.list("roots")

    assert len(items) == 1
    assert items[0].category == "roots"
    assert items[0].selector == str(root)
    assert items[0].value == root


def test_list_runtimes_returns_both_providers(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _make_nwjs_runtime(paths)
    _make_easyrpg_runtime(paths)
    catalog = api_cleanup.CleanupCatalog(paths, ConfigRepository(paths))

    items = catalog.list("runtimes")

    assert {item.selector for item in items} == {
        "nwjs:x64:standard-v0.90.0",
        "easyrpg:0.8.1",
    }
    assert all(item.category == "runtimes" for item in items)


def test_list_downloads_returns_both_providers(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    nwjs, easyrpg = _make_downloads(paths)
    catalog = api_cleanup.CleanupCatalog(paths, ConfigRepository(paths))

    items = catalog.list("downloads")

    assert {item.selector for item in items} == {
        f"nwjs:{nwjs.name}",
        f"easyrpg:{easyrpg.name}",
    }
    assert {item.provider for item in items} == {"nwjs", "easyrpg"}


def test_list_profiles_returns_created_profile(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    profile = _make_profile(paths)
    catalog = api_cleanup.CleanupCatalog(paths, ConfigRepository(paths))

    items = catalog.list("profiles")

    assert len(items) == 1
    assert items[0].selector == profile.name
    assert items[0].value == profile


def test_remove_root_forgets_allowed_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    repository, _ = _make_root(paths, tmp_path)
    catalog = api_cleanup.CleanupCatalog(paths, repository)

    (item,) = catalog.list("roots")
    catalog.remove(item)

    assert repository.load().allowed_game_roots == ()
    assert catalog.list("roots") == ()


def test_remove_nwjs_runtime_deletes_managed_directory(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    root = _make_nwjs_runtime(paths)
    _make_easyrpg_runtime(paths)
    catalog = api_cleanup.CleanupCatalog(paths, ConfigRepository(paths))

    (item,) = tuple(
        item for item in catalog.list("runtimes") if item.selector == "nwjs:x64:standard-v0.90.0"
    )
    catalog.remove(item)

    assert not root.exists()
    assert {item.selector for item in catalog.list("runtimes")} == {"easyrpg:0.8.1"}


def test_remove_easyrpg_runtime_deletes_managed_directory(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _make_nwjs_runtime(paths)
    root = _make_easyrpg_runtime(paths)
    catalog = api_cleanup.CleanupCatalog(paths, ConfigRepository(paths))

    (item,) = tuple(item for item in catalog.list("runtimes") if item.selector == "easyrpg:0.8.1")
    catalog.remove(item)

    assert not root.exists()
    selectors = {item.selector for item in catalog.list("runtimes")}
    assert selectors == {"nwjs:x64:standard-v0.90.0"}


def test_remove_nwjs_download_deletes_archive(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    nwjs, easyrpg = _make_downloads(paths)
    catalog = api_cleanup.CleanupCatalog(paths, ConfigRepository(paths))

    (item,) = tuple(item for item in catalog.list("downloads") if item.provider == "nwjs")
    catalog.remove(item)

    assert not nwjs.exists()
    assert easyrpg.exists()


def test_remove_easyrpg_download_deletes_archive(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    nwjs, easyrpg = _make_downloads(paths)
    catalog = api_cleanup.CleanupCatalog(paths, ConfigRepository(paths))

    (item,) = tuple(item for item in catalog.list("downloads") if item.provider == "easyrpg")
    catalog.remove(item)

    assert not easyrpg.exists()
    assert nwjs.exists()


def test_remove_profile_deletes_directory(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    profile = _make_profile(paths)
    catalog = api_cleanup.CleanupCatalog(paths, ConfigRepository(paths))

    (item,) = catalog.list("profiles")
    catalog.remove(item)

    assert not profile.exists()
    assert catalog.list("profiles") == ()


def test_cleanup_api_performs_no_console_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden_print(*args: object, **kwargs: object) -> None:
        raise AssertionError("cleanup API must not print")

    def forbidden_input(*args: object, **kwargs: object) -> str:
        raise AssertionError("cleanup API must not read")

    monkeypatch.setattr("builtins.print", forbidden_print)
    monkeypatch.setattr("builtins.input", forbidden_input)

    paths = _paths(tmp_path)
    repository, _ = _make_root(paths, tmp_path)
    _make_nwjs_runtime(paths)
    _make_easyrpg_runtime(paths)
    _make_downloads(paths)
    _make_profile(paths)
    catalog = api_cleanup.CleanupCatalog(paths, repository)

    assert len(catalog.list("roots")) == 1
    assert len(catalog.list("runtimes")) == 2
    assert len(catalog.list("downloads")) == 2
    assert len(catalog.list("profiles")) == 1
    assert len(catalog.list()) == 6

    for category in ("roots", "runtimes", "downloads", "profiles"):
        items = catalog.list(category)
        assert items
        catalog.remove(items[0])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
