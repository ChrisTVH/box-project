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
from box.errors import ConfigurationError, RuntimeError
from box.models import RuntimeSpec
from box.paths import AppPaths
from box.runtime.available import AvailableVersions
from box.runtime.catalog import RuntimeCatalog
from box.runtime.easyrpg import (
    AvailableEasyRPGVersions,
    EasyRPGCatalog,
)
from box.runtime.easyrpg import normalize_version as normalize_easyrpg_version
from box.runtime.paging import resolve_virtual_page
from box.runtime.platform import normalize_architecture
from box.runtime.validator import normalize_version as normalize_nwjs_version
from box.utils.i18n import _
from box.utils.sizes import format_size_decimal


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


def _installed_nwjs_specs(paths: AppPaths) -> frozenset[RuntimeSpec]:
    """Return installed NW.js specs for hide-installed filtering."""
    try:
        runtimes = api_list_nwjs(RuntimeCatalog(paths))
    except ConfigurationError, OSError, RuntimeError:
        return frozenset()
    return frozenset(runtime.spec for runtime in runtimes)


def _installed_easyrpg_keys(paths: AppPaths) -> frozenset[str]:
    """Return normalized installed EasyRPG versions for hide-installed filtering."""
    try:
        runtimes = api_list_easyrpg(EasyRPGCatalog(paths))
    except ConfigurationError, OSError, RuntimeError:
        return frozenset()
    keys: set[str] = set()
    for runtime in runtimes:
        try:
            keys.add(normalize_easyrpg_version(runtime.version))
        except RuntimeError:
            keys.add(runtime.version)
    return frozenset(keys)


def _nwjs_spec_for_available(version: str, architecture: str, sdk: bool) -> RuntimeSpec:
    """Build a comparable spec for one available version."""
    try:
        normalized_version = normalize_nwjs_version(version)
    except RuntimeError:
        normalized_version = version
    try:
        normalized_arch = normalize_architecture(architecture)
    except RuntimeError:
        normalized_arch = architecture
    return RuntimeSpec(normalized_version, normalized_arch, sdk)


def _fetch_nwjs_virtual_page(
    virtual_page: int,
    architecture: str,
    sdk: bool,
    installed: frozenset[RuntimeSpec],
    paths: AppPaths,
    page_size: int = 10,
) -> AvailableVersions:
    """Return one virtual page of NW.js versions with installed specs hidden."""
    clamped = max(1, virtual_page)

    def fetch_page(backend_page: int) -> tuple[tuple[str, ...], dict[str, int | None]]:
        fetched = fetch_available_versions(backend_page, architecture, sdk, paths=paths)
        return fetched.versions, dict(fetched.sizes)

    def is_excluded(version: str) -> bool:
        return _nwjs_spec_for_available(version, architecture, sdk) in installed

    versions, sizes = resolve_virtual_page(
        fetch_page, is_excluded, clamped, page_size=page_size
    )
    return AvailableVersions(page=clamped, versions=versions, sizes=sizes)


def _easyrpg_key(version: str) -> str:
    """Return the normalized key for one EasyRPG version."""
    try:
        return normalize_easyrpg_version(version)
    except RuntimeError:
        return version


def _fetch_easyrpg_virtual_page(
    virtual_page: int,
    installed: frozenset[str],
    paths: AppPaths,
    page_size: int = 10,
) -> AvailableEasyRPGVersions:
    """Return one virtual page of EasyRPG versions with installed keys hidden."""
    clamped = max(1, virtual_page)

    def fetch_page(backend_page: int) -> tuple[tuple[str, ...], dict[str, int | None]]:
        fetched = fetch_easyrpg_versions(backend_page, paths=paths)
        return fetched.versions, dict(fetched.sizes)

    def is_excluded(version: str) -> bool:
        return _easyrpg_key(version) in installed

    versions, sizes = resolve_virtual_page(
        fetch_page, is_excluded, clamped, page_size=page_size
    )
    return AvailableEasyRPGVersions(page=clamped, versions=versions, sizes=sizes)


def _display_version(version: str, sizes: dict[str, int | None]) -> str:
    """Render one version with its size suffix when the size is known."""
    size = sizes.get(version)
    if size is None:
        return version
    return _("{version} ({size})").format(version=version, size=format_size_decimal(size))


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
    installed = _installed_nwjs_specs(paths)
    _print_available(
        _fetch_nwjs_virtual_page(page, architecture, sdk, installed, paths), architecture, sdk
    )
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
    installed = _installed_nwjs_specs(paths)
    page = initial_page
    while True:
        try:
            available_versions = _fetch_nwjs_virtual_page(page, architecture, sdk, installed, paths)
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
        prompt = _("Select 1-{count}, [n]ext, [p]revious, or [q]uit: ").format(
            count=len(available_versions.versions)
        )
        try:
            action = read(prompt).strip().lower()
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
        write(f"  {index}. {_display_version(version, available_versions.sizes)}")
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
        installed = _installed_easyrpg_keys(paths)
        _print_easyrpg_versions(_fetch_easyrpg_virtual_page(page, installed, paths), write)
        return 0
    installed = _installed_easyrpg_keys(paths)
    current_page = page
    while True:
        try:
            versions = _fetch_easyrpg_virtual_page(current_page, installed, paths)
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
        _print_easyrpg_versions(versions, write)
        prompt = _("Select 1-{count}, [n]ext, [p]revious, or [q]uit: ").format(
            count=len(versions.versions)
        )
        try:
            action = read(prompt).strip().lower()
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


def _print_easyrpg_versions(
    versions: AvailableEasyRPGVersions,
    write: Callable[[str], None] = print,
) -> None:
    """Print one EasyRPG Player release page."""
    write(_("Available EasyRPG Player versions (page {page}, x64):").format(page=versions.page))
    if not versions.versions:
        write(_("  (no versions on this page)"))
        return
    for index, version in enumerate(versions.versions, start=1):
        write(f"  {index}. {_display_version(version, versions.sizes)}")
