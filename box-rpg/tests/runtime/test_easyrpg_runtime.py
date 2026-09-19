import json
import os
import tarfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, ClassVar, cast
from urllib.error import URLError

import pytest

from box.errors import ConfigurationError, RuntimeError
from box.paths import AppPaths
from box.runtime import easyrpg
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


def test_easyrpg_local_html_index_is_sorted_and_paginated_in_tens() -> None:
    content = """
    <a href="/downloads/player/0.8/">0.8</a>
    <a href="/downloads/player/0.8.1/">0.8.1</a>
    <a href="/downloads/player/0.8.1.1/">0.8.1.1</a>
    <a href="0.7.0/">0.7.0</a>
    <a href="0.6.2.3/">0.6.2.3</a>
    <a href="0.6.2/">0.6.2</a>
    <a href="0.6.1/">0.6.1</a>
    <a href="0.6.0/">0.6.0</a>
    <a href="0.5.0/">0.5.0</a>
    <a href="0.4.0/">0.4.0</a>
    <a href="0.3.0/">0.3.0</a>
    <a href="0.6.2.3/">duplicate</a>
    <a href="latest/">latest</a>
    <a href="0.8.1-linux.tar.gz">archive</a>
    """
    newest_ten = (
        "0.8.1.1",
        "0.8.1",
        "0.8",
        "0.7.0",
        "0.6.2.3",
        "0.6.2",
        "0.6.1",
        "0.6.0",
        "0.5.0",
        "0.4.0",
    )

    assert parse_versions(content) == (*newest_ten, "0.3.0")
    assert parse_available_versions(content, 1) == AvailableEasyRPGVersions(
        page=1, versions=newest_ten
    )
    assert parse_available_versions(content, 2) == AvailableEasyRPGVersions(
        page=2, versions=("0.3.0",)
    )


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


def test_easyrpg_interactive_browser_retries_a_failed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.cli import runtime as cli_runtime
    from box.runtime.easyrpg import EasyRPGRuntime

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    output: list[str] = []
    installed: list[str] = []
    attempts = iter((False, True))
    choices = iter(("r", "1", "yes"))

    def flaky_fetch(page: int, *, paths: AppPaths | None = None) -> AvailableEasyRPGVersions:
        if next(attempts):
            return AvailableEasyRPGVersions(page=page, versions=("0.8.1.1",))
        raise RuntimeError("connection reset")

    def install_version(_: AppPaths, version: str) -> EasyRPGRuntime:
        installed.append(version)
        return EasyRPGRuntime(version, tmp_path)

    monkeypatch.setattr("box.cli.runtime.fetch_easyrpg_versions", flaky_fetch)
    monkeypatch.setattr("box.cli.runtime.install_easyrpg_runtime", install_version)
    browser = cli_runtime._easyrpg_available  # pyright: ignore[reportPrivateUsage]

    def _fake_read(prompt: str) -> str:
        return next(choices)

    result = browser(paths, 1, True, read=_fake_read, write=output.append)

    assert result == 0
    assert installed == ["0.8.1.1"]
    assert any("Could not load the version list" in line for line in output)


def test_easyrpg_interactive_browser_quits_after_a_failed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.cli import runtime as cli_runtime

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    output: list[str] = []
    choices = iter(("q",))

    def failing_fetch(page: int, *, paths: AppPaths | None = None) -> AvailableEasyRPGVersions:
        raise RuntimeError("connection reset")

    monkeypatch.setattr("box.cli.runtime.fetch_easyrpg_versions", failing_fetch)
    browser = cli_runtime._easyrpg_available  # pyright: ignore[reportPrivateUsage]

    def _fake_read(prompt: str) -> str:
        return next(choices)

    result = browser(paths, 1, True, read=_fake_read, write=output.append)

    assert result == 0
    assert output[-1] == "Selection cancelled."


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


class _EasyProbeResponse:
    """Minimal Range-probe response for EasyRPG Player archives."""

    def __init__(
        self,
        headers: dict[str, str],
        status: int = 206,
        url: str = "https://easyrpg.org/downloads/player/0.8.1/easyrpg-player-0.8.1-linux.tar.gz",
    ) -> None:
        self.headers = headers
        self.status = status
        self._url = url

    def __enter__(self) -> _EasyProbeResponse:
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            return b"x"
        return b"x"[:amount]


class _EasyIndexResponse:
    """Minimal index response for EasyRPG Player listing fetches."""

    headers: ClassVar[dict[str, str]] = {}
    status: ClassVar[int] = 200

    def __init__(self, content: bytes, url: str = "https://easyrpg.org/downloads/player/") -> None:
        self._content = content
        self._url = url

    def __enter__(self) -> _EasyIndexResponse:
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            return self._content
        return self._content[:amount]


def _stub_easy_probe(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], status: int = 206
) -> None:
    response = _EasyProbeResponse(headers, status=status)

    def _fake_open(request: object, timeout: float, allowed_hosts: object) -> _EasyProbeResponse:
        return response

    monkeypatch.setattr(easyrpg, "open_official", _fake_open)
    easyrpg.easyrpg_archive_available.cache_clear()


def _stub_easy_index(monkeypatch: pytest.MonkeyPatch, html: str) -> None:
    payload = html.encode("utf-8")

    def fake_open(request: object, timeout: float, allowed_hosts: object) -> _EasyIndexResponse:
        assert getattr(request, "full_url", "").startswith("https://easyrpg.org/")
        return _EasyIndexResponse(payload)

    monkeypatch.setattr(easyrpg, "open_official", fake_open)


def test_easyrpg_available_sizes_default_to_empty() -> None:
    assert AvailableEasyRPGVersions(page=1, versions=()).sizes == {}
    assert AvailableEasyRPGVersions(page=1, versions=("0.8.1",)).sizes == {}


def test_easyrpg_probe_prefers_content_range(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_easy_probe(monkeypatch, {"Content-Range": "bytes 0-0/54321", "Content-Length": "1"})
    assert easyrpg.easyrpg_archive_available("0.8.1") == (True, 54321)


def test_easyrpg_probe_returns_none_for_206_without_content_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 206 response without Content-Range never falls back to Content-Length."""
    _stub_easy_probe(monkeypatch, {"Content-Length": "4321"})
    assert easyrpg.easyrpg_archive_available("0.8.1.1") == (True, None)


def test_easyrpg_probe_returns_none_for_non_zero_start_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only bytes 0-0/N counts as a valid 206 size, other ranges are ignored."""
    _stub_easy_probe(monkeypatch, {"Content-Range": "bytes 0-1/54321"})
    assert easyrpg.easyrpg_archive_available("0.8.1.2") == (True, None)


def test_easyrpg_probe_ignores_content_range_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 response uses Content-Length even when Content-Range is present."""
    _stub_easy_probe(
        monkeypatch,
        {"Content-Range": "bytes 0-0/999", "Content-Length": "432"},
        status=200,
    )
    assert easyrpg.easyrpg_archive_available("0.8.1.3") == (True, 432)


def test_easyrpg_probe_returns_none_for_200_with_only_content_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response without Content-Length reports an unknown size."""
    _stub_easy_probe(monkeypatch, {"Content-Range": "bytes 0-0/999"}, status=200)
    assert easyrpg.easyrpg_archive_available("0.8.1.4") == (True, None)


def test_easyrpg_probe_returns_missing_on_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect validation failures surface as a missing archive, not an abort."""

    def _bad_redirect(request: object, timeout: float, allowed_hosts: object) -> object:
        raise RuntimeError("redirect to untrusted host")

    monkeypatch.setattr(easyrpg, "open_official", _bad_redirect)
    easyrpg.easyrpg_archive_available.cache_clear()
    assert easyrpg.easyrpg_archive_available("0.8.2") == (False, None)


def test_easyrpg_fetch_skips_bad_version_without_aborting_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One RuntimeError probe reports unknown size instead of failing the page."""
    html = '<a href="0.8.1/">0.8.1</a><a href="0.8/">0.8</a>'
    payload = html.encode("utf-8")
    easyrpg.easyrpg_archive_available.cache_clear()

    def fake_open(request: object, timeout: float, allowed_hosts: object) -> object:
        url = getattr(request, "full_url", "")
        if url == easyrpg.VERSIONS_INDEX:
            return _EasyIndexResponse(payload)
        if "/0.8.1/" in url:
            raise RuntimeError("redirect to untrusted host")
        return _EasyProbeResponse({"Content-Range": "bytes 0-0/42"}, status=206)

    monkeypatch.setattr(easyrpg, "open_official", fake_open)
    try:
        result = easyrpg.fetch_available_versions(1)
    finally:
        easyrpg.easyrpg_archive_available.cache_clear()
    assert result.versions == ("0.8.1", "0.8")
    assert result.sizes == {"0.8.1": None, "0.8": 42}


def test_easyrpg_probe_returns_none_without_size_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_easy_probe(monkeypatch, {})
    assert easyrpg.easyrpg_archive_available("0.8") == (True, None)


def test_easyrpg_probe_handles_range_ignored_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_easy_probe(monkeypatch, {"Content-Length": "7777"}, status=200)
    assert easyrpg.easyrpg_archive_available("0.7.0") == (True, 7777)


def test_easyrpg_probe_returns_none_for_malformed_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_easy_probe(monkeypatch, {"Content-Range": "invalid", "Content-Length": "bad"})
    assert easyrpg.easyrpg_archive_available("0.6.0") == (True, None)


def test_easyrpg_fetch_includes_per_page_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = '<a href="0.8.1/">0.8.1</a><a href="0.8/">0.8</a>'
    payload = html.encode("utf-8")
    calls: list[str] = []

    def fake_open(request: object, timeout: float, allowed_hosts: object) -> object:
        url = getattr(request, "full_url", "")
        calls.append(url)
        if url == easyrpg.VERSIONS_INDEX:
            return _EasyIndexResponse(payload)
        return _EasyProbeResponse({"Content-Length": "1000"}, status=200)

    monkeypatch.setattr(easyrpg, "open_official", fake_open)
    easyrpg.easyrpg_archive_available.cache_clear()
    result = easyrpg.fetch_available_versions(1)
    assert result.versions == ("0.8.1", "0.8")
    assert result.sizes == {"0.8.1": 1000, "0.8": 1000}
    assert calls[0] == easyrpg.VERSIONS_INDEX


def _prime_easyrpg_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, html: str, size: int | None
) -> AppPaths:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    payload = html.encode("utf-8")

    def fake_open(request: object, timeout: float, allowed_hosts: object) -> object:
        url = getattr(request, "full_url", "")
        if url == easyrpg.VERSIONS_INDEX:
            return _EasyIndexResponse(payload)
        return _EasyProbeResponse(
            {} if size is None else {"Content-Length": str(size)},
            status=200,
        )

    monkeypatch.setattr(easyrpg, "open_official", fake_open)
    easyrpg.easyrpg_archive_available.cache_clear()
    result = easyrpg.fetch_available_versions(1, paths=paths)
    assert result.versions
    return paths


def test_easyrpg_cache_round_trip_and_fresh_avoids_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prime_easyrpg_cache(
        tmp_path, monkeypatch, '<a href="0.8.1/">0.8.1</a><a href="0.8/">0.8</a>', 500
    )
    target = paths.listings_root / "easyrpg" / "easyrpg-p1.json"
    assert target.is_file()
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["source"] == "easyrpg"
    assert document["page"] == 1
    assert document["versions"] == ["0.8.1", "0.8"]
    assert document["sizes"] == {"0.8.1": 500, "0.8": 500}
    assert (target.stat().st_mode & 0o777) == 0o600

    def forbidden(request: object, timeout: float, allowed_hosts: object) -> object:
        raise AssertionError("fresh cache must not use the network")

    monkeypatch.setattr(easyrpg, "open_official", forbidden)
    cached = easyrpg.fetch_available_versions(1, paths=paths)
    assert cached.versions == ("0.8.1", "0.8")
    assert cached.sizes == {"0.8.1": 500, "0.8": 500}


def test_easyrpg_cache_expired_entry_triggers_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prime_easyrpg_cache(tmp_path, monkeypatch, '<a href="0.8/">0.8</a>', 10)
    target = paths.listings_root / "easyrpg" / "easyrpg-p1.json"
    document = json.loads(target.read_text(encoding="utf-8"))
    document["fetched_at"] = time.time() - 7200
    target.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    easyrpg.easyrpg_archive_available.cache_clear()
    _stub_easy_index(monkeypatch, '<a href="0.7.0/">0.7.0</a>')

    def _fake_archive_available(version: str) -> tuple[bool, int | None]:
        return (True, 55)

    monkeypatch.setattr(easyrpg, "easyrpg_archive_available", _fake_archive_available)
    refreshed = easyrpg.fetch_available_versions(1, paths=paths)
    assert refreshed.versions == ("0.7.0",)
    assert refreshed.sizes == {"0.7.0": 55}


def test_easyrpg_stale_cache_returned_when_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prime_easyrpg_cache(tmp_path, monkeypatch, '<a href="0.8/">0.8</a>', 77)
    target = paths.listings_root / "easyrpg" / "easyrpg-p1.json"
    document = json.loads(target.read_text(encoding="utf-8"))
    document["fetched_at"] = time.time() - 7200
    target.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

    def offline(request: object, timeout: float, allowed_hosts: object) -> object:
        raise URLError("offline")

    monkeypatch.setattr(easyrpg, "open_official", offline)
    stale = easyrpg.fetch_available_versions(1, paths=paths)
    assert stale.versions == ("0.8",)
    assert stale.sizes == {"0.8": 77}


def test_easyrpg_corrupt_cache_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    target = paths.listings_root / "easyrpg" / "easyrpg-p1.json"
    target.write_text("{not-json", encoding="utf-8")
    _stub_easy_index(monkeypatch, '<a href="0.8/">0.8</a>')

    def _fake_archive_available(version: str) -> tuple[bool, int | None]:
        return (True, 5)

    monkeypatch.setattr(easyrpg, "easyrpg_archive_available", _fake_archive_available)
    result = easyrpg.fetch_available_versions(1, paths=paths)
    assert result.versions == ("0.8",)
    assert result.sizes == {"0.8": 5}
