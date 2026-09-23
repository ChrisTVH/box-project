"""No-I/O orchestration for NW.js and EasyRPG Player runtimes."""

from __future__ import annotations

from box.models import RuntimeInfo
from box.paths import AppPaths
from box.runtime.available import AvailableVersions
from box.runtime.available import fetch_available_versions as fetch_nwjs_versions
from box.runtime.catalog import RuntimeCatalog
from box.runtime.downloader import ProgressReporter
from box.runtime.downloader import install_runtime as install_nwjs_runtime
from box.runtime.easyrpg import AvailableEasyRPGVersions, EasyRPGCatalog, EasyRPGRuntime
from box.runtime.easyrpg import fetch_available_versions as fetch_easyrpg_versions
from box.runtime.easyrpg import install_runtime as install_easyrpg_runtime
from box.runtime.paging import resolve_virtual_page
from box.runtime.platform import current_architecture as _current_architecture

__all__ = [
    "ProgressReporter",
    "default_architecture",
    "fetch_easyrpg_available",
    "fetch_nwjs_available",
    "install_easyrpg",
    "install_nwjs",
    "list_easyrpg",
    "list_nwjs",
    "remove_easyrpg",
    "remove_nwjs",
    "resolve_virtual_page",
]


def default_architecture() -> str:
    """Return the NW.js architecture for the current machine.

    Raise RuntimeError for unsupported CPUs, same as the CLI fallback.
    """
    return _current_architecture()


def list_nwjs(catalog: RuntimeCatalog) -> tuple[RuntimeInfo, ...]:
    """Return valid launcher-owned NW.js runtimes."""
    return catalog.list()


def install_nwjs(
    paths: AppPaths,
    version: str,
    architecture: str,
    sdk: bool = False,
    progress: ProgressReporter | None = None,
) -> RuntimeInfo:
    """Install an NW.js runtime, forwarding progress to the downloader."""
    return install_nwjs_runtime(paths, version, architecture, sdk, progress)


def remove_nwjs(
    catalog: RuntimeCatalog, version: str, architecture: str, sdk: bool = False
) -> None:
    """Delete one launcher-owned NW.js runtime."""
    catalog.remove(version, architecture, sdk)


def fetch_nwjs_available(
    page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
) -> AvailableVersions:
    """Fetch one page of installable NW.js versions without any interaction."""
    return fetch_nwjs_versions(page, architecture, sdk, paths=paths)


def list_easyrpg(catalog: EasyRPGCatalog) -> tuple[EasyRPGRuntime, ...]:
    """Return valid launcher-owned EasyRPG Player runtimes."""
    return catalog.list()


def install_easyrpg(
    paths: AppPaths, version: str, progress: ProgressReporter | None = None
) -> EasyRPGRuntime:
    """Install an EasyRPG Player runtime, forwarding progress to the downloader."""
    return install_easyrpg_runtime(paths, version, progress)


def remove_easyrpg(catalog: EasyRPGCatalog, version: str) -> None:
    """Delete one launcher-owned EasyRPG Player runtime."""
    catalog.remove(version)


def fetch_easyrpg_available(
    page: int, *, paths: AppPaths | None = None
) -> AvailableEasyRPGVersions:
    """Fetch one page of EasyRPG Player versions without any interaction."""
    return fetch_easyrpg_versions(page, paths=paths)
