from pathlib import Path

import pytest

from box.errors import ConfigurationError, RuntimeError
from box.paths import AppPaths
from box.runtime.catalog import RuntimeCatalog


def test_runtime_catalog_discovers_fake_runtime_with_nw_executable(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    runtime_root = paths.runtimes_root / "linux-x64" / "standard-v0.90.0"
    runtime_root.mkdir(parents=True)
    executable = runtime_root / "nw"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    invalid_runtime = paths.runtimes_root / "linux-x64" / "standard-v0.91.0"
    invalid_runtime.mkdir()
    catalog = RuntimeCatalog(paths)

    runtimes = catalog.list()
    runtime = catalog.get("0.90.0", "X64")

    assert len(runtimes) == 1
    assert runtimes[0].spec.version == "v0.90.0"
    assert runtimes[0].spec.architecture == "x64"
    assert not runtimes[0].spec.sdk
    assert runtimes[0].root == runtime_root
    assert runtimes[0].executable == executable
    assert runtime == runtimes[0]


def test_runtime_catalog_lists_managed_incomplete_runtime_directories(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    incomplete = paths.runtimes_root / "linux-x64" / "standard-v0.90.0"
    complete = paths.runtimes_root / "linux-x64" / "sdk-v0.91.0"
    incomplete.mkdir(parents=True)
    complete.mkdir()
    executable = complete / "nw"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    catalog = RuntimeCatalog(paths)

    managed = catalog.list_managed()

    assert [(runtime.spec.version, runtime.spec.flavor) for runtime in managed] == [
        ("v0.91.0", "sdk"),
        ("v0.90.0", "standard"),
    ]
    assert [runtime.root for runtime in managed] == [complete, incomplete]
    assert catalog.list() == (catalog.get("0.91.0", "x64", sdk=True),)


def test_runtime_catalog_removes_an_incomplete_runtime_directory(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    runtime_root = paths.runtimes_root / "linux-x64" / "standard-v0.90.0"
    runtime_root.mkdir(parents=True)

    RuntimeCatalog(paths).remove("0.90.0", "x64")

    assert not runtime_root.exists()


def test_runtime_catalog_lists_only_canonical_managed_layouts(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    valid = paths.runtimes_root / "linux-x64" / "standard-v0.90.0"
    upper_case_platform = paths.runtimes_root / "linux-X64" / "standard-v0.91.0"
    invalid_version = paths.runtimes_root / "linux-x64" / "sdk-v0.91"
    valid.mkdir(parents=True)
    upper_case_platform.mkdir(parents=True)
    invalid_version.mkdir()

    managed = RuntimeCatalog(paths).list_managed()

    assert [runtime.root for runtime in managed] == [valid]


def test_runtime_catalog_revalidates_enumerated_runtime_before_removal(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    runtime_root = paths.runtimes_root / "linux-x64" / "standard-v0.90.0"
    runtime_root.mkdir(parents=True)
    catalog = RuntimeCatalog(paths)
    managed = catalog.list_managed()[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime_root.rmdir()
    runtime_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="contains a symlink"):
        catalog.remove_managed(managed)

    assert outside.is_dir()


def test_runtime_catalog_rejects_a_platform_replaced_by_a_symlink(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    platform = paths.runtimes_root / "linux-x64"
    runtime_root = platform / "standard-v0.90.0"
    runtime_root.mkdir(parents=True)
    catalog = RuntimeCatalog(paths)
    managed = catalog.list_managed()[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime_root.rmdir()
    platform.rmdir()
    platform.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="managed runtime path contains a symlink"):
        catalog.remove_managed(managed)

    assert outside.is_dir()


def test_runtime_catalog_rejects_architecture_path_traversal(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")

    with pytest.raises(RuntimeError, match=r"unsupported NW\.js architecture"):
        RuntimeCatalog(paths).get("0.90.0", "x64/..")


def test_runtime_catalog_rejects_a_non_executable_nw_binary(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    runtime_root = paths.runtimes_root / "linux-x64" / "standard-v0.90.0"
    runtime_root.mkdir(parents=True)
    executable = runtime_root / "nw"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    catalog = RuntimeCatalog(paths)

    assert catalog.list() == ()
    with pytest.raises(RuntimeError, match="executable is missing"):
        catalog.get("0.90.0", "x64")
