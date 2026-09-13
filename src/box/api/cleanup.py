"""No-I/O cleanup enumeration for graphical front ends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from box.config.repository import ConfigRepository
from box.errors import RuntimeError
from box.launch.profiles import ProfileCatalog
from box.paths import AppPaths
from box.runtime.catalog import ManagedRuntime, RuntimeCatalog
from box.runtime.downloads import DownloadCatalog
from box.runtime.easyrpg import EasyRPGCatalog, EasyRPGDownloadCatalog, EasyRPGRuntime
from box.utils.i18n import _

__all__ = [
    "CATEGORIES",
    "CleanupCatalog",
    "CleanupItem",
    "RemovalResult",
]

CATEGORIES = ("roots", "runtimes", "downloads", "profiles")


@dataclass(frozen=True, slots=True)
class CleanupItem:
    """One safely enumerated item available for cleanup."""

    category: str
    selector: str
    label: str
    value: Path | ManagedRuntime | EasyRPGRuntime
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class RemovalResult:
    """The outcome of a batch of independently safe removal operations."""

    removed: int
    failed: int


class CleanupCatalog:
    """List and delete only data known to the launcher-managed catalogs."""

    def __init__(self, paths: AppPaths, repository: ConfigRepository) -> None:
        self._repository = repository
        self._runtimes = RuntimeCatalog(paths)
        self._easyrpg_runtimes = EasyRPGCatalog(paths)
        self._downloads = DownloadCatalog(paths)
        self._easyrpg_downloads = EasyRPGDownloadCatalog(paths)
        self._profiles = ProfileCatalog(paths)

    def list(self, category: str | None = None) -> tuple[CleanupItem, ...]:
        """Return canonical cleanup items for one category or every category."""
        if category is not None and category not in CATEGORIES:
            raise RuntimeError(_("unknown cleanup category: {category}").format(category=category))
        items: list[CleanupItem] = []
        if category in {None, "roots"}:
            items.extend(
                CleanupItem("roots", str(root), str(root), root)
                for root in self._repository.load().allowed_game_roots
            )
        if category in {None, "runtimes"}:
            items.extend(
                CleanupItem(
                    "runtimes",
                    _runtime_selector(runtime),
                    _render_runtime(runtime),
                    runtime,
                )
                for runtime in (
                    *self._runtimes.list_managed(),
                    *self._easyrpg_runtimes.list_managed(),
                )
            )
        if category in {None, "downloads"}:
            items.extend(
                CleanupItem("downloads", f"nwjs:{archive.name}", archive.name, archive, "nwjs")
                for archive in self._downloads.list()
            )
            items.extend(
                CleanupItem(
                    "downloads", f"easyrpg:{archive.name}", archive.name, archive, "easyrpg"
                )
                for archive in self._easyrpg_downloads.list()
            )
        if category in {None, "profiles"}:
            items.extend(
                CleanupItem("profiles", profile.name, profile.name, profile)
                for profile in self._profiles.list()
            )
        return tuple(items)

    def remove(self, item: CleanupItem) -> None:
        """Remove one previously listed item through its owning catalog."""
        if item.category == "roots":
            assert isinstance(item.value, Path)
            self._repository.remove_allowed_root(item.value)
        elif item.category == "runtimes":
            if isinstance(item.value, EasyRPGRuntime):
                self._easyrpg_runtimes.remove_managed(item.value)
            else:
                assert isinstance(item.value, ManagedRuntime)
                self._runtimes.remove_managed(item.value)
        elif item.category == "downloads":
            assert isinstance(item.value, Path)
            if item.provider == "easyrpg":
                self._easyrpg_downloads.remove(item.value)
            else:
                self._downloads.remove(item.value)
        elif item.category == "profiles":
            assert isinstance(item.value, Path)
            self._profiles.remove(item.value)
        else:
            raise RuntimeError(
                _("unknown cleanup category: {category}").format(category=item.category)
            )


def _runtime_selector(runtime: ManagedRuntime | EasyRPGRuntime) -> str:
    if isinstance(runtime, EasyRPGRuntime):
        return f"easyrpg:{runtime.version}"
    return f"nwjs:{runtime.spec.architecture}:{runtime.spec.directory_name}"


def _render_runtime(runtime: ManagedRuntime | EasyRPGRuntime) -> str:
    if isinstance(runtime, EasyRPGRuntime):
        return f"EasyRPG Player {runtime.version} x64"
    flavor = "SDK" if runtime.spec.sdk else _("standard")
    return f"NW.js {runtime.spec.version} {runtime.spec.architecture} {flavor}"
