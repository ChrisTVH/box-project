"""Runtime subcommand implementations."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable

from box.api.runtime import fetch_easyrpg_available as fetch_easyrpg_versions
from box.api.runtime import fetch_nwjs_available as fetch_available_versions
from box.api.runtime import install_easyrpg as install_easyrpg_runtime
from box.api.runtime import install_nwjs as api_install_nwjs
from box.api.runtime import list_easyrpg as api_list_easyrpg
from box.api.runtime import list_nwjs as api_list_nwjs
from box.api.runtime import remove_easyrpg as api_remove_easyrpg
from box.api.runtime import remove_nwjs as api_remove_nwjs
from box.errors import RuntimeError
from box.paths import AppPaths
from box.runtime.available import AvailableVersions
from box.runtime.catalog import RuntimeCatalog
from box.runtime.easyrpg import (
    AvailableEasyRPGVersions,
    EasyRPGCatalog,
)
from box.utils.i18n import _


def list_runtimes(catalog: RuntimeCatalog) -> int:
    """Print every valid launcher-owned runtime."""
    runtimes = api_list_nwjs(catalog)
    if not runtimes:
        print(_("no NW.js runtimes installed"))
        return 0
    for runtime in runtimes:
        print(
            f"{runtime.spec.version} {runtime.spec.architecture} {_runtime_flavor(runtime.spec.sdk)} {runtime.root}"
        )
    return 0


def install(paths: AppPaths, version: str, architecture: str, sdk: bool) -> int:
    """Install and report an NW.js runtime."""
    runtime = api_install_nwjs(paths, version, architecture, sdk, progress=None)
    print(
        _("installed {version} ({flavor}) at {path}").format(
            version=runtime.spec.version,
            flavor=_runtime_flavor(runtime.spec.sdk),
            path=runtime.root,
        )
    )
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
    """Browse ten online versions per page and install a confirmed selection."""
    page = initial_page
    while True:
        try:
            available_versions = fetch_available_versions(page, architecture, sdk)
        except RuntimeError as exc:
            write(_("Could not load the version list: {error}").format(error=exc))
            try:
                retry = read(_("[r]etry this page, or [q]uit: ")).strip().lower()
            except EOFError:
                write(_("Selection cancelled."))
                return 0
            if retry == "q":
                write(_("Selection cancelled."))
                return 0
            continue
        _print_available(available_versions, architecture, sdk, write)
        try:
            action = read(_("Select 1-10, [n]ext, [p]revious, or [q]uit: ")).strip().lower()
        except EOFError:
            write(_("Selection cancelled."))
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
            write(_("Invalid selection."))
            continue
        index = int(action) - 1
        if index < 0 or index >= len(available_versions.versions):
            write(_("Invalid selection."))
            continue
        version = available_versions.versions[index]
        try:
            confirmation = (
                read(
                    _("Install NW.js {version} for {architecture}? [y/N] ").format(
                        version=version, architecture=architecture
                    )
                )
                .strip()
                .lower()
            )
        except EOFError:
            write(_("Selection cancelled."))
            return 0
        if confirmation not in {"y", "yes"}:
            write(_("Installation cancelled."))
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
    flavor = _runtime_flavor(sdk)
    write(
        _("Available NW.js versions (page {page}, {architecture}, {flavor}):").format(
            page=available_versions.page, architecture=architecture, flavor=flavor
        )
    )
    if not available_versions.versions:
        write(_("  (no stable versions on this page)"))
        return
    for index, version in enumerate(available_versions.versions, start=1):
        write(f"  {index}. {version}")
    sdk_argument = " --sdk" if sdk else ""
    write(
        _("Install with: {command}").format(
            command=(
                f"box-rpg runtime nwjs install VERSION --architecture {architecture}{sdk_argument}"
            )
        )
    )


def _runtime_flavor(sdk: bool) -> str:
    """Return the user-facing NW.js build flavor."""
    return "SDK" if sdk else _("standard")


def remove(catalog: RuntimeCatalog, version: str, architecture: str, sdk: bool) -> int:
    """Remove a launcher-owned NW.js runtime."""
    api_remove_nwjs(catalog, version, architecture, sdk)
    print(_("removed {version}").format(version=version))
    return 0


def easyrpg(paths: AppPaths, action: str, arguments: Namespace) -> int:
    """Dispatch EasyRPG Player runtime management commands."""
    catalog = EasyRPGCatalog(paths)
    if action == "list":
        runtimes = api_list_easyrpg(catalog)
        if not runtimes:
            print(_("no EasyRPG Player runtimes installed"))
        for runtime in runtimes:
            print(f"{runtime.version} x64 {runtime.root}")
        return 0
    if action == "install":
        runtime = install_easyrpg_runtime(paths, arguments.version)
        print(
            _("installed EasyRPG Player {version} at {path}").format(
                version=runtime.version, path=runtime.root
            )
        )
        return 0
    if action == "remove":
        api_remove_easyrpg(catalog, arguments.version)
        print(_("removed EasyRPG Player {version}").format(version=arguments.version))
        return 0
    return _easyrpg_available(paths, arguments.page, arguments.interactive)


def _easyrpg_available(
    paths: AppPaths,
    page: int,
    interactive: bool,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> int:
    """List online EasyRPG Player releases or choose one to install."""
    if not interactive:
        _print_easyrpg_versions(fetch_easyrpg_versions(page))
        return 0
    current_page = page
    while True:
        try:
            versions = fetch_easyrpg_versions(current_page)
        except RuntimeError as exc:
            write(_("Could not load the version list: {error}").format(error=exc))
            try:
                retry = read(_("[r]etry this page, or [q]uit: ")).strip().lower()
            except EOFError:
                write(_("Selection cancelled."))
                return 0
            if retry == "q":
                write(_("Selection cancelled."))
                return 0
            continue
        _print_easyrpg_versions(versions)
        try:
            action = read(_("Select 1-10, [n]ext, [p]revious, or [q]uit: ")).strip().lower()
        except EOFError:
            write(_("Selection cancelled."))
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
            write(_("Invalid selection."))
            continue
        version = versions.versions[int(action) - 1]
        try:
            confirmation = (
                read(_("Install EasyRPG Player {version} for x64? [y/N] ").format(version=version))
                .strip()
                .lower()
            )
        except EOFError:
            write(_("Selection cancelled."))
            return 0
        if confirmation in {"y", "yes"}:
            runtime = install_easyrpg_runtime(paths, version)
            write(
                _("installed EasyRPG Player {version} at {path}").format(
                    version=runtime.version, path=runtime.root
                )
            )
            return 0
        write(_("Installation cancelled."))


def _print_easyrpg_versions(versions: AvailableEasyRPGVersions) -> None:
    """Print one EasyRPG Player release page."""
    print(_("Available EasyRPG Player versions (page {page}, x64):").format(page=versions.page))
    if not versions.versions:
        print(_("  (no versions on this page)"))
        return
    for index, version in enumerate(versions.versions, start=1):
        print(f"  {index}. {version}")
