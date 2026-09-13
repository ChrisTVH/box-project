"""Tests for the no-I/O runtime API."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from box.api import runtime as api_runtime
from box.models import RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime.available import AvailableVersions
from box.runtime.catalog import RuntimeCatalog
from box.runtime.easyrpg import AvailableEasyRPGVersions, EasyRPGCatalog, EasyRPGRuntime


def _paths(tmp_path: Path) -> AppPaths:
    """Build isolated launcher paths inside a temporary directory."""
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


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


def test_list_nwjs_returns_catalog_runtimes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    root = _make_nwjs_runtime(paths)
    catalog = RuntimeCatalog(paths)

    runtimes = api_runtime.list_nwjs(catalog)

    assert runtimes == catalog.list()
    assert len(runtimes) == 1
    assert runtimes[0].root == root
    assert runtimes[0].spec.version == "v0.90.0"


def test_remove_nwjs_deletes_managed_runtime(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    root = _make_nwjs_runtime(paths)
    catalog = RuntimeCatalog(paths)

    api_runtime.remove_nwjs(catalog, "0.90.0", "x64")

    assert not root.exists()
    assert catalog.list() == ()


def test_install_nwjs_forwards_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path)
    captured: dict[str, object] = {}
    expected = RuntimeInfo(RuntimeSpec("v0.90.0", "x64", False), tmp_path, tmp_path / "nw")

    def fake_install(
        actual_paths: AppPaths,
        version: str,
        architecture: str,
        sdk: bool = False,
        progress: api_runtime.ProgressReporter | None = None,
    ) -> RuntimeInfo:
        captured["paths"] = actual_paths
        captured["version"] = version
        captured["architecture"] = architecture
        captured["sdk"] = sdk
        captured["progress"] = progress
        return expected

    def reporter(completed: int, total: int | None) -> None:
        raise AssertionError("reporter must only be forwarded, not called")

    monkeypatch.setattr(api_runtime, "install_nwjs_runtime", fake_install)

    result = api_runtime.install_nwjs(paths, "v0.90.0", "x64", False, reporter)

    assert result == expected
    assert captured["paths"] == paths
    assert captured["version"] == "v0.90.0"
    assert captured["architecture"] == "x64"
    assert captured["sdk"] is False
    assert captured["progress"] is reporter


def test_install_nwjs_defaults_progress_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    captured: dict[str, object] = {}
    expected = RuntimeInfo(RuntimeSpec("v0.90.0", "x64", False), tmp_path, tmp_path / "nw")

    def fake_install(
        actual_paths: AppPaths,
        version: str,
        architecture: str,
        sdk: bool = False,
        progress: api_runtime.ProgressReporter | None = None,
    ) -> RuntimeInfo:
        captured["progress"] = progress
        return expected

    monkeypatch.setattr(api_runtime, "install_nwjs_runtime", fake_install)

    assert api_runtime.install_nwjs(paths, "v0.90.0", "x64") == expected
    assert captured["progress"] is None


def test_fetch_nwjs_available_forwards_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    expected = AvailableVersions(page=2, versions=("v0.90.0",))

    def fake_fetch(page: int, architecture: str, sdk: bool) -> AvailableVersions:
        captured["page"] = page
        captured["architecture"] = architecture
        captured["sdk"] = sdk
        return expected

    monkeypatch.setattr(api_runtime, "fetch_nwjs_versions", fake_fetch)

    result = api_runtime.fetch_nwjs_available(2, "x64", True)

    assert result == expected
    assert captured == {"page": 2, "architecture": "x64", "sdk": True}


def test_list_easyrpg_returns_catalog_runtimes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    root = _make_easyrpg_runtime(paths)
    catalog = EasyRPGCatalog(paths)

    runtimes = api_runtime.list_easyrpg(catalog)

    assert runtimes == catalog.list()
    assert len(runtimes) == 1
    assert runtimes[0].root == root
    assert runtimes[0].version == "0.8.1"


def test_remove_easyrpg_deletes_managed_runtime(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    root = _make_easyrpg_runtime(paths)
    catalog = EasyRPGCatalog(paths)

    api_runtime.remove_easyrpg(catalog, "0.8.1")

    assert not root.exists()
    assert catalog.list() == ()


def test_install_easyrpg_forwards_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path)
    captured: dict[str, object] = {}
    expected = EasyRPGRuntime("0.8.1", tmp_path)

    def fake_install(
        actual_paths: AppPaths,
        version: str,
        progress: api_runtime.ProgressReporter | None = None,
    ) -> EasyRPGRuntime:
        captured["paths"] = actual_paths
        captured["version"] = version
        captured["progress"] = progress
        return expected

    def reporter(completed: int, total: int | None) -> None:
        raise AssertionError("reporter must only be forwarded, not called")

    monkeypatch.setattr(api_runtime, "install_easyrpg_runtime", fake_install)

    result = api_runtime.install_easyrpg(paths, "0.8.1", reporter)

    assert result == expected
    assert captured["paths"] == paths
    assert captured["version"] == "0.8.1"
    assert captured["progress"] is reporter


def test_fetch_easyrpg_available_forwards_page(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    expected = AvailableEasyRPGVersions(page=3, versions=("0.8.1",))

    def fake_fetch(page: int) -> AvailableEasyRPGVersions:
        captured["page"] = page
        return expected

    monkeypatch.setattr(api_runtime, "fetch_easyrpg_versions", fake_fetch)

    result = api_runtime.fetch_easyrpg_available(3)

    assert result == expected
    assert captured == {"page": 3}


def test_nwjs_install_runtime_forwards_progress_to_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.runtime import downloader

    paths = _paths(tmp_path)
    captured: dict[str, object] = {}

    def fake_download(
        url: str,
        destination_name: str,
        directory_descriptor: int,
        progress: downloader.ProgressReporter | None = None,
        allowed_hosts: frozenset[str] = downloader.OFFICIAL_DOWNLOAD_HOSTS,
    ) -> None:
        captured["progress"] = progress
        descriptor = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
            dir_fd=directory_descriptor,
        )
        os.close(descriptor)

    def fake_extract(archive_descriptor: int, temporary_descriptor: int) -> str:
        os.mkdir("extracted", dir_fd=temporary_descriptor)
        return "extracted"

    def fake_verify(archive_descriptor: int, url: str) -> None:
        return None

    def fake_validate(descriptor: int) -> None:
        return None

    monkeypatch.setattr(downloader, "download_archive_at", fake_download)
    monkeypatch.setattr(downloader, "verify_archive", fake_verify)
    monkeypatch.setattr(downloader, "extract_runtime_at", fake_extract)
    monkeypatch.setattr(downloader, "validate_runtime_executable_at", fake_validate)

    def reporter(completed: int, total: int | None) -> None:
        return None

    runtime = downloader.install_runtime(paths, "0.90.0", "x64", False, reporter)

    assert captured["progress"] is reporter
    assert runtime.spec.version == "v0.90.0"
    assert runtime.root.is_dir()


def test_easyrpg_install_runtime_forwards_progress_to_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.runtime import easyrpg

    paths = _paths(tmp_path)
    captured: dict[str, object] = {}

    def fake_download(
        url: str,
        destination_name: str,
        directory_descriptor: int,
        progress: easyrpg.ProgressReporter | None = None,
        allowed_hosts: frozenset[str] = easyrpg.OFFICIAL_DOWNLOAD_HOSTS,
    ) -> None:
        captured["progress"] = progress
        descriptor = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
            dir_fd=directory_descriptor,
        )
        os.close(descriptor)

    def fake_extract(archive: Path, destination: Path) -> Path:
        staged = destination / "runtime"
        staged.mkdir()
        player = staged / "easyrpg-player"
        player.write_text("#!/bin/sh\n", encoding="utf-8")
        player.chmod(0o700)
        return staged

    monkeypatch.setattr(easyrpg, "current_architecture", lambda: "x64")
    monkeypatch.setattr(easyrpg, "download_archive_at", fake_download)
    monkeypatch.setattr(easyrpg, "extract_runtime", fake_extract)

    def reporter(completed: int, total: int | None) -> None:
        return None

    runtime = easyrpg.install_runtime(paths, "0.8.1", reporter)

    assert captured["progress"] is reporter
    assert runtime.version == "0.8.1"
    assert (runtime.root / "easyrpg-player").is_file()


def test_runtime_api_performs_no_console_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden_print(*args: object, **kwargs: object) -> None:
        raise AssertionError("runtime API must not print")

    def forbidden_input(*args: object, **kwargs: object) -> str:
        raise AssertionError("runtime API must not read")

    monkeypatch.setattr("builtins.print", forbidden_print)
    monkeypatch.setattr("builtins.input", forbidden_input)

    def fake_fetch_nwjs(page: int, architecture: str, sdk: bool) -> AvailableVersions:
        assert (architecture, sdk) == ("x64", False)
        return AvailableVersions(page=page, versions=())

    def fake_fetch_easyrpg(page: int) -> AvailableEasyRPGVersions:
        return AvailableEasyRPGVersions(page=page, versions=())

    def fake_install_nwjs(
        paths: AppPaths,
        version: str,
        architecture: str,
        sdk: bool = False,
        progress: api_runtime.ProgressReporter | None = None,
    ) -> RuntimeInfo:
        assert (version, architecture, sdk) == ("v0.90.0", "x64", False)
        assert isinstance(paths, AppPaths)
        return RuntimeInfo(RuntimeSpec("v0.90.0", "x64", False), tmp_path, tmp_path / "nw")

    def fake_install_easyrpg(
        paths: AppPaths,
        version: str,
        progress: api_runtime.ProgressReporter | None = None,
    ) -> EasyRPGRuntime:
        assert version == "0.8.1"
        assert isinstance(paths, AppPaths)
        return EasyRPGRuntime("0.8.1", tmp_path)

    monkeypatch.setattr(api_runtime, "fetch_nwjs_versions", fake_fetch_nwjs)
    monkeypatch.setattr(api_runtime, "fetch_easyrpg_versions", fake_fetch_easyrpg)
    monkeypatch.setattr(api_runtime, "install_nwjs_runtime", fake_install_nwjs)
    monkeypatch.setattr(api_runtime, "install_easyrpg_runtime", fake_install_easyrpg)

    paths = _paths(tmp_path)
    _make_nwjs_runtime(paths)
    _make_easyrpg_runtime(paths)
    nwjs_catalog = RuntimeCatalog(paths)
    easyrpg_catalog = EasyRPGCatalog(paths)

    assert len(api_runtime.list_nwjs(nwjs_catalog)) == 1
    assert len(api_runtime.list_easyrpg(easyrpg_catalog)) == 1
    assert api_runtime.fetch_nwjs_available(1, "x64", False).versions == ()
    assert api_runtime.fetch_easyrpg_available(1).versions == ()

    def silent_reporter(completed: int, total: int | None) -> None:
        return None

    api_runtime.install_nwjs(paths, "v0.90.0", "x64", False, silent_reporter)
    api_runtime.install_easyrpg(paths, "0.8.1", silent_reporter)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
