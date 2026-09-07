"""Runtime subcommand implementations."""

from __future__ import annotations

from box.paths import AppPaths
from box.runtime.catalog import RuntimeCatalog
from box.runtime.downloader import install_runtime


def list_runtimes(catalog: RuntimeCatalog) -> int:
    """Print every valid launcher-owned runtime."""
    runtimes = catalog.list()
    if not runtimes:
        print("no NW.js runtimes installed")
        return 0
    for runtime in runtimes:
        print(
            f"{runtime.spec.version} {runtime.spec.architecture} {runtime.spec.flavor} {runtime.root}"
        )
    return 0


def install(paths: AppPaths, version: str, architecture: str, sdk: bool) -> int:
    """Install and report an NW.js runtime."""
    runtime = install_runtime(paths, version, architecture, sdk)
    print(f"installed {runtime.spec.version} ({runtime.spec.flavor}) at {runtime.root}")
    return 0


def remove(catalog: RuntimeCatalog, version: str, architecture: str, sdk: bool) -> int:
    """Remove a launcher-owned NW.js runtime."""
    catalog.remove(version, architecture, sdk)
    print(f"removed {version}")
    return 0
