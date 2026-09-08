"""Runtime subcommand implementations."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable

from box.paths import AppPaths
from box.runtime.available import AvailableVersions, fetch_available_versions
from box.runtime.catalog import RuntimeCatalog
from box.runtime.downloader import install_runtime
from box.runtime.easyrpg import (
    AvailableEasyRPGVersions,
    EasyRPGCatalog,
)
from box.runtime.easyrpg import (
    fetch_available_versions as fetch_easyrpg_versions,
)
from box.runtime.easyrpg import (
    install_runtime as install_easyrpg_runtime,
)


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
        f"Install with: box-rpg runtime nwjs install VERSION --architecture {architecture}{sdk_argument}"
    )


def remove(catalog: RuntimeCatalog, version: str, architecture: str, sdk: bool) -> int:
    """Remove a launcher-owned NW.js runtime."""
    catalog.remove(version, architecture, sdk)
    print(f"removed {version}")
    return 0


def easyrpg(paths: AppPaths, action: str, arguments: Namespace) -> int:
    """Dispatch EasyRPG Player runtime management commands."""
    catalog = EasyRPGCatalog(paths)
    if action == "list":
        runtimes = catalog.list()
        if not runtimes:
            print("no EasyRPG Player runtimes installed")
        for runtime in runtimes:
            print(f"{runtime.version} x64 {runtime.root}")
        return 0
    if action == "install":
        runtime = install_easyrpg_runtime(paths, arguments.version)
        print(f"installed EasyRPG Player {runtime.version} at {runtime.root}")
        return 0
    if action == "remove":
        catalog.remove(arguments.version)
        print(f"removed EasyRPG Player {arguments.version}")
        return 0
    return _easyrpg_available(paths, arguments.page, arguments.interactive)


def _easyrpg_available(paths: AppPaths, page: int, interactive: bool) -> int:
    """List online EasyRPG Player releases or choose one to install."""
    if not interactive:
        _print_easyrpg_versions(fetch_easyrpg_versions(page))
        return 0
    current_page = page
    while True:
        versions = fetch_easyrpg_versions(current_page)
        _print_easyrpg_versions(versions)
        try:
            action = input("Select 1-5, [n]ext, [p]revious, or [q]uit: ").strip().lower()
        except EOFError:
            print("Selection cancelled.")
            return 0
        if action == "q":
            return 0
        if action == "n":
            current_page += 1
            continue
        if action == "p":
            current_page = max(1, current_page - 1)
            continue
        if not action.isdigit() or not 1 <= int(action) <= len(versions.versions):
            print("Invalid selection.")
            continue
        version = versions.versions[int(action) - 1]
        try:
            confirmation = (
                input(f"Install EasyRPG Player {version} for x64? [y/N] ").strip().lower()
            )
        except EOFError:
            print("Selection cancelled.")
            return 0
        if confirmation in {"y", "yes"}:
            runtime = install_easyrpg_runtime(paths, version)
            print(f"installed EasyRPG Player {runtime.version} at {runtime.root}")
            return 0
        print("Installation cancelled.")


def _print_easyrpg_versions(versions: AvailableEasyRPGVersions) -> None:
    """Print one EasyRPG Player release page."""
    print(f"Available EasyRPG Player versions (page {versions.page}, x64):")
    if not versions.versions:
        print("  (no versions on this page)")
        return
    for index, version in enumerate(versions.versions, start=1):
        print(f"  {index}. {version}")
