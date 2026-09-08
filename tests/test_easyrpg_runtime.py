import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from box.errors import ConfigurationError, RuntimeError
from box.paths import AppPaths
from box.runtime.easyrpg import (
    AvailableEasyRPGVersions,
    EasyRPGCatalog,
    download_archive_path,
    download_url,
    normalize_version,
    parse_available_versions,
    parse_versions,
)


@dataclass(frozen=True, slots=True)
class _EasyRPGPaths:
    """Temporary implementation of the EasyRPG path API for catalog tests."""

    cache_root: Path

    @property
    def easyrpg_downloads_root(self) -> Path:
        return self.cache_root / "downloads" / "easyrpg"

    @property
    def easyrpg_runtimes_root(self) -> Path:
        return self.cache_root / "runtimes" / "easyrpg"

    def ensure(self) -> None:
        self.easyrpg_downloads_root.mkdir(parents=True, exist_ok=True)
        self.easyrpg_runtimes_root.mkdir(parents=True, exist_ok=True)

    def ensure_managed_easyrpg_download_path(self, path: Path) -> Path:
        self.ensure()
        return self._ensure_direct_child(self.easyrpg_downloads_root, path, "download")

    def ensure_managed_easyrpg_runtime_path(self, path: Path) -> Path:
        self.ensure()
        return self._ensure_direct_child(self.easyrpg_runtimes_root, path, "runtime")

    def open_managed_cache_directory(self, *components: str) -> int:
        path = self.cache_root.joinpath(*components)
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY)

    @staticmethod
    def _ensure_direct_child(root: Path, path: Path, label: str) -> Path:
        candidate = path.absolute()
        if candidate.parent != root.absolute():
            raise ConfigurationError(f"unexpected EasyRPG {label} path: {path}")
        if candidate.name != candidate.name.lower() or candidate.is_symlink():
            raise ConfigurationError(f"unsafe EasyRPG {label} path: {path}")
        return candidate


def _paths(tmp_path: Path) -> _EasyRPGPaths:
    return _EasyRPGPaths(tmp_path / "cache")


def test_easyrpg_urls_and_archive_path_use_the_fixed_x64_linux_archive(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    assert (
        download_url("0.8.1.1")
        == "https://easyrpg.org/downloads/player/0.8.1.1/easyrpg-player-0.8.1.1-linux.tar.gz"
    )
    assert download_archive_path(cast(AppPaths, paths), "0.8.1.1") == (
        tmp_path / "cache" / "downloads" / "easyrpg" / "easyrpg-player-0.8.1.1-linux.tar.gz"
    )


def test_easyrpg_versions_accept_two_to_four_numeric_components() -> None:
    assert normalize_version("0.8") == "0.8"
    assert normalize_version("0.8.1") == "0.8.1"
    assert normalize_version("0.8.1.1") == "0.8.1.1"

    for invalid in ("0", "0.8.1.1.1", "v0.8.1", "0.8-beta", "0..8"):
        with pytest.raises(RuntimeError, match="invalid EasyRPG Player version"):
            normalize_version(invalid)


def test_easyrpg_local_html_index_is_sorted_and_paginated_in_fives() -> None:
    content = """
    <a href="/downloads/player/0.8/">0.8</a>
    <a href="/downloads/player/0.8.1/">0.8.1</a>
    <a href="/downloads/player/0.8.1.1/">0.8.1.1</a>
    <a href="0.7.0/">0.7.0</a>
    <a href="0.6.2.3/">0.6.2.3</a>
    <a href="0.6.2.3/">duplicate</a>
    <a href="latest/">latest</a>
    <a href="0.8.1-linux.tar.gz">archive</a>
    """

    assert parse_versions(content) == ("0.8.1.1", "0.8.1", "0.8", "0.7.0", "0.6.2.3")
    assert parse_available_versions(content, 1) == AvailableEasyRPGVersions(
        page=1, versions=("0.8.1.1", "0.8.1", "0.8", "0.7.0", "0.6.2.3")
    )
    assert parse_available_versions(content, 2) == AvailableEasyRPGVersions(page=2, versions=())


def test_easyrpg_catalog_lists_selects_and_removes_managed_runtimes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.easyrpg_runtimes_root.mkdir(parents=True)
    older = paths.easyrpg_runtimes_root / "0.8"
    latest = paths.easyrpg_runtimes_root / "0.8.1.1"
    ignored = paths.easyrpg_runtimes_root / "latest"
    older.mkdir()
    latest.mkdir()
    ignored.mkdir()
    for runtime in (older, latest):
        executable = runtime / "easyrpg-player"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o700)
    catalog = EasyRPGCatalog(cast(AppPaths, paths))

    assert catalog.list() == (
        catalog.get("0.8.1.1"),
        catalog.get("0.8"),
    )
    assert catalog.latest().root == latest

    catalog.remove("0.8")

    assert not older.exists()
    assert latest.is_dir()
    assert ignored.is_dir()
