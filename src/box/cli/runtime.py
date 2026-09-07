"""Runtime subcommand implementations."""

from __future__ import annotations

from collections.abc import Callable

from box.paths import AppPaths
from box.runtime.available import AvailableVersions, fetch_available_versions
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


def available(
    paths: AppPaths,
    page: int,
    interactive: bool,
    architecture: str,
    sdk: bool,
) -> int:
    """List online stable versions or interactively install one."""
    if interactive:
        return select_interactively(paths, page, architecture, sdk)
    _print_available(fetch_available_versions(page, architecture, sdk), architecture, sdk)
    return 0


def select_interactively(
    paths: AppPaths,
    initial_page: int,
    architecture: str,
    sdk: bool,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> int:
    """Browse five online versions per page and install a confirmed selection."""
    page = initial_page
    while True:
        available_versions = fetch_available_versions(page, architecture, sdk)
        _print_available(available_versions, architecture, sdk, write)
        try:
            action = read("Select 1-5, [n]ext, [p]revious, or [q]uit: ").strip().lower()
        except EOFError:
            write("Selection cancelled.")
            return 0
        if action == "q":
            return 0
        if action == "n":
            page += 1
            continue
        if action == "p":
            page = max(1, page - 1)
            continue
        if not action.isdigit():
            write("Invalid selection.")
            continue
        index = int(action) - 1
        if index < 0 or index >= len(available_versions.versions):
            write("Invalid selection.")
            continue
        version = available_versions.versions[index]
        try:
            confirmation = (
                read(f"Install NW.js {version} for {architecture}? [y/N] ").strip().lower()
            )
        except EOFError:
            write("Selection cancelled.")
            return 0
        if confirmation not in {"y", "yes"}:
            write("Installation cancelled.")
            continue
        install(paths, version, architecture, sdk)
        return 0


def _print_available(
    available_versions: AvailableVersions,
    architecture: str,
    sdk: bool,
    write: Callable[[str], None] = print,
) -> None:
    """Print a concise version page and the associated install command."""
    flavor = "SDK" if sdk else "standard"
    write(f"Available NW.js versions (page {available_versions.page}, {architecture}, {flavor}):")
    if not available_versions.versions:
        write("  (no stable versions on this page)")
        return
    for index, version in enumerate(available_versions.versions, start=1):
        write(f"  {index}. {version}")
    sdk_argument = " --sdk" if sdk else ""
    write(
        f"Install with: box-rpg runtime install VERSION --architecture {architecture}{sdk_argument}"
    )


def remove(catalog: RuntimeCatalog, version: str, architecture: str, sdk: bool) -> int:
    """Remove a launcher-owned NW.js runtime."""
    catalog.remove(version, architecture, sdk)
    print(f"removed {version}")
    return 0
