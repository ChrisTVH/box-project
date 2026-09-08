import os
import tarfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from box.errors import ConfigurationError, RuntimeError
from box.paths import AppPaths
from box.runtime.easyrpg import (
    AvailableEasyRPGVersions,
    EasyRPGCatalog,
    EasyRPGDownloadCatalog,
    download_archive_path,
    download_url,
    extract_runtime,
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


def test_easyrpg_catalog_rejects_a_runtime_replaced_by_a_symlink_with_real_paths(
    tmp_path: Path,
) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    runtime = paths.easyrpg_runtimes_root / "0.8.1"
    runtime.mkdir(parents=True)
    player = runtime / "easyrpg-player"
    player.write_text("#!/bin/sh\n", encoding="utf-8")
    player.chmod(0o700)
    catalog = EasyRPGCatalog(paths)
    managed = catalog.list_managed()[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime.replace(outside / runtime.name)
    runtime.symlink_to(outside / runtime.name, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="contains a symlink"):
        catalog.remove_managed(managed)

    assert (outside / runtime.name).is_dir()


def test_easyrpg_download_catalog_rejects_a_replaced_archive_symlink_with_real_paths(
    tmp_path: Path,
) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    archive = paths.easyrpg_downloads_root / "easyrpg-player-0.8.1-linux.tar.gz"
    archive.write_bytes(b"archive")
    ignored = paths.easyrpg_downloads_root / "easyrpg-player-0.8.2-linux.tar.gz"
    ignored.symlink_to(archive)
    catalog = EasyRPGDownloadCatalog(paths)
    listed = catalog.list()
    outside = tmp_path / "outside.tar.gz"
    archive.replace(outside)
    archive.symlink_to(outside)

    assert listed == (archive,)
    with pytest.raises(ConfigurationError, match="contains a symlink"):
        catalog.remove(listed[0])
    assert outside.read_bytes() == b"archive"
    assert ignored.is_symlink()


def test_easyrpg_malformed_archive_staging_collision_raises_runtime_error(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime").mkdir()
    (source / "readme").write_text("readme", encoding="utf-8")
    archive = tmp_path / "player.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source / "runtime", arcname="runtime")
        tar.add(source / "readme", arcname="readme")
    destination = tmp_path / "destination"
    destination.mkdir()

    with pytest.raises(RuntimeError, match="cannot stage EasyRPG Player archive"):
        extract_runtime(archive, destination)
    assert tuple(destination.iterdir()) == ()


def test_easyrpg_install_keeps_archive_locked_through_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.runtime import easyrpg

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    archive = paths.easyrpg_downloads_root / "easyrpg-player-0.8.1-linux.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("easyrpg-player")
        member.mode = 0o700
        tar.addfile(member)
    extract = easyrpg.extract_runtime
    checks: list[str] = []

    def checked_extract(source: Path, destination: Path) -> Path:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(RuntimeError, match="busy"):
                executor.submit(EasyRPGDownloadCatalog(paths).remove, archive).result(timeout=5)
            with pytest.raises(RuntimeError, match="busy"):
                executor.submit(easyrpg.install_runtime, paths, "0.8.1").result(timeout=5)
        checks.append("locked")
        return extract(source, destination)

    monkeypatch.setattr(easyrpg, "current_architecture", lambda: "x64")
    monkeypatch.setattr(easyrpg, "extract_runtime", checked_extract)
    runtime = easyrpg.install_runtime(paths, "0.8.1")
    assert checks == ["locked"]
    assert (runtime.root / "easyrpg-player").is_file()
    EasyRPGDownloadCatalog(paths).remove(archive)


def test_easyrpg_extraction_rejects_archive_symlinks(tmp_path: Path) -> None:
    archive = tmp_path / "outside.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.addfile(tarfile.TarInfo("easyrpg-player"))
    link = tmp_path / "player.tar.gz"
    link.symlink_to(archive)
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(RuntimeError, match="cannot stage"):
        extract_runtime(link, destination)
    assert tuple(destination.iterdir()) == ()


def test_easyrpg_uses_the_open_archive_inode_after_name_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.runtime import easyrpg

    archive = tmp_path / "player.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("easyrpg-player")
        member.size = 4
        tar.addfile(member, BytesIO(b"safe"))
    extract = easyrpg.extract_bounded

    def replace_name(
        source: BinaryIO, destination: Path, prepare: Callable[[Path], None] | None = None
    ) -> None:
        archive.rename(tmp_path / "original.tar.gz")
        archive.symlink_to(tmp_path / "missing")
        extract(source, destination, prepare)

    monkeypatch.setattr(easyrpg, "extract_bounded", replace_name)
    destination = tmp_path / "destination"
    destination.mkdir()
    assert (extract_runtime(archive, destination) / "easyrpg-player").read_bytes() == b"safe"


def test_easyrpg_extraction_uses_shared_quotas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("box.runtime.limits.MAX_MEMBER_BYTES", 1)
    archive = tmp_path / "player.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("easyrpg-player")
        member.size = 2
        tar.addfile(member, BytesIO(b"ab"))
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(RuntimeError, match="limit"):
        extract_runtime(archive, destination)
    assert tuple(destination.iterdir()) == ()
