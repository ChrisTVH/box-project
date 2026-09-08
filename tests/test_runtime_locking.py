import os
import tarfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest

from box.errors import RuntimeError
from box.paths import AppPaths
from box.runtime import downloader
from box.runtime.catalog import RuntimeCatalog
from box.runtime.downloads import DownloadCatalog
from box.runtime.security import cache_lock


def test_partial_and_archive_share_a_persistent_lock(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with (
                cache_lock(descriptor, "runtime.tar.gz"),
                pytest.raises(RuntimeError, match="busy"),
            ):
                executor.submit(_take_lock, descriptor, "runtime.tar.gz.part").result(timeout=5)
            inode = (tmp_path / ".runtime.tar.gz.lock").stat().st_ino
            executor.submit(_take_lock, descriptor, "runtime.tar.gz").result(timeout=5)
            assert (tmp_path / ".runtime.tar.gz.lock").stat().st_ino == inode
    finally:
        os.close(descriptor)


def _take_lock(descriptor: int, name: str) -> None:
    with cache_lock(descriptor, name):
        pass


def test_install_holds_download_and_runtime_locks_until_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    archive = paths.downloads_root / "standard-v0.90.0-linux-x64.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("runtime/nw")
        member.mode = 0o700
        member.size = 2
        tar.addfile(member, BytesIO(b"nw"))
    extract = downloader.extract_runtime_at
    validate = downloader.validate_runtime_executable_at
    calls: list[str] = []

    def checked_extract(source: int, destination: int) -> str:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(RuntimeError, match="busy"):
                executor.submit(DownloadCatalog(paths).remove, archive).result(timeout=5)
            with pytest.raises(RuntimeError, match="busy"):
                executor.submit(downloader.install_runtime, paths, "0.90.0", "x64").result(
                    timeout=5
                )
        calls.append("locked")
        return extract(source, destination)

    monkeypatch.setattr(downloader, "extract_runtime_at", checked_extract)

    def checked_validate(directory_descriptor: int) -> None:
        validate(directory_descriptor)
        if not (paths.runtimes_root / "linux-x64" / "standard-v0.90.0").exists():
            return
        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            pytest.raises(RuntimeError, match="busy"),
        ):
            executor.submit(RuntimeCatalog(paths).remove, "0.90.0", "x64").result(timeout=5)
        calls.append("published")

    monkeypatch.setattr(downloader, "validate_runtime_executable_at", checked_validate)
    runtime = downloader.install_runtime(paths, "0.90.0", "x64")
    assert calls == ["locked", "published"]
    assert runtime.executable.read_bytes() == b"nw"
    DownloadCatalog(paths).remove(archive)
    assert not archive.exists()


def test_download_cleanup_cannot_unlink_an_active_partial(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    partial = paths.downloads_root / "standard-v0.90.0-linux-x64.tar.gz.part"
    partial.write_bytes(b"in progress")
    descriptor = paths.open_managed_cache_directory("downloads", "nwjs")
    try:
        with cache_lock(descriptor, partial.name), ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(RuntimeError, match="busy"):
                executor.submit(DownloadCatalog(paths).remove, partial).result(timeout=5)
            assert partial.read_bytes() == b"in progress"
    finally:
        os.close(descriptor)
    DownloadCatalog(paths).remove(partial)
