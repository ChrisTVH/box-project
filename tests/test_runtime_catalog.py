from pathlib import Path

import pytest

from box.errors import RuntimeError
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


def test_runtime_catalog_rejects_architecture_path_traversal(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")

    with pytest.raises(RuntimeError, match=r"unsupported NW\.js architecture"):
        RuntimeCatalog(paths).get("0.90.0", "x64/..")
