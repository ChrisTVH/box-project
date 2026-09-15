from pathlib import Path

import pytest

from box.errors import ConfigurationError, RuntimeError
from box.paths import AppPaths
from box.runtime.downloads import DownloadCatalog


def test_download_catalog_lists_only_direct_regular_nwjs_archives(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    archive = paths.downloads_root / "nwjs-v0.90.0-linux-x64.tar.gz"
    partial = paths.downloads_root / "standard-v0.91.0-linux-arm64.tar.gz.part"
    archive.write_bytes(b"archive")
    partial.write_bytes(b"partial")
    (paths.downloads_root / "not-an-archive.tar.gz").write_bytes(b"other")
    (paths.downloads_root / "nwjs-v0.90.0-linux-x64.tar.gz.bak").write_bytes(b"other")
    (paths.downloads_root / "nwjs-v0.92.0-linux-x64.tar.gz").mkdir()
    nested = paths.downloads_root / "nested"
    nested.mkdir()
    (nested / "nwjs-v0.93.0-linux-x64.tar.gz").write_bytes(b"nested")
    symlink = paths.downloads_root / "nwjs-v0.94.0-linux-x64.tar.gz"
    symlink.symlink_to(archive)

    downloads = DownloadCatalog(paths).list()

    assert downloads == (archive, partial)


def test_download_catalog_removes_only_a_valid_managed_archive(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    catalog = DownloadCatalog(paths)
    archive = paths.downloads_root / "nwjs-sdk-v0.90.0-linux-ia32.tar.gz"
    archive.write_bytes(b"archive")
    outside = tmp_path / "nwjs-v0.90.0-linux-x64.tar.gz"
    outside.write_bytes(b"outside")
    invalid = paths.downloads_root / "unrelated.tar.gz"
    invalid.write_bytes(b"invalid")

    catalog.remove(archive)

    assert not archive.exists()
    with pytest.raises(ConfigurationError, match="outside cache"):
        catalog.remove(outside)
    with pytest.raises(RuntimeError, match=r"unsafe NW\.js download archive"):
        catalog.remove(invalid)
    assert outside.read_bytes() == b"outside"
    assert invalid.read_bytes() == b"invalid"


def test_download_catalog_remove_all_preserves_unrecognized_and_symlinked_entries(
    tmp_path: Path,
) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    archive = paths.downloads_root / "nwjs-v0.90.0-linux-x64.tar.gz"
    partial = paths.downloads_root / "sdk-v0.90.0-linux-arm.tar.gz.part"
    ignored = paths.downloads_root / "unrelated.tar.gz"
    archive.write_bytes(b"archive")
    partial.write_bytes(b"partial")
    ignored.write_bytes(b"ignored")
    symlink = paths.downloads_root / "nwjs-v0.91.0-linux-x64.tar.gz"
    symlink.symlink_to(ignored)

    removed = DownloadCatalog(paths).remove_all()

    assert removed == 2
    assert not archive.exists()
    assert not partial.exists()
    assert ignored.read_bytes() == b"ignored"
    assert symlink.is_symlink()


def test_download_catalog_rejects_nested_download_paths(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    nested = paths.downloads_root / "nested" / "nwjs-v0.90.0-linux-x64.tar.gz"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"nested")

    with pytest.raises(ConfigurationError, match="nested download path"):
        DownloadCatalog(paths).remove(nested)

    assert nested.read_bytes() == b"nested"


def test_download_catalog_rejects_a_download_root_replaced_by_a_symlink(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    archive = paths.downloads_root / "nwjs-v0.90.0-linux-x64.tar.gz"
    archive.write_bytes(b"archive")
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = outside / archive.name
    archive.replace(moved)
    paths.downloads_root.rmdir()
    paths.downloads_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="managed directory must not be a symlink"):
        DownloadCatalog(paths).remove(archive)

    assert moved.read_bytes() == b"archive"
