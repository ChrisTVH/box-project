"""Safe interactive and scripted cleanup of launcher-managed data."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from box.cli.menu import choose_paged
from box.config.repository import ConfigRepository
from box.errors import BoxError, RuntimeError
from box.launch.profiles import ProfileCatalog
from box.paths import AppPaths
from box.runtime.catalog import ManagedRuntime, RuntimeCatalog
from box.runtime.downloads import DownloadCatalog
from box.runtime.easyrpg import EasyRPGCatalog, EasyRPGDownloadCatalog, EasyRPGRuntime
from box.utils.i18n import _, ngettext

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


def execute(
    paths: AppPaths,
    repository: ConfigRepository,
    *,
    command: str | None = None,
    category: str | None = None,
    selector: str | None = None,
    remove_all: bool = False,
    yes: bool = False,
    interactive: bool = False,
    has_tty: bool,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> int:
    """List or safely delete launcher-managed data in the requested mode."""
    if interactive:
        if yes or command is not None:
            raise RuntimeError(
                _("{interactive} cannot be combined with another cleanup mode").format(
                    interactive="--interactive"
                )
            )
        if not has_tty:
            raise RuntimeError(
                _("cleanup {interactive} requires an interactive terminal").format(
                    interactive="--interactive"
                )
            )
        return _interactive_cleanup(CleanupCatalog(paths, repository), read, write)
    catalog = CleanupCatalog(paths, repository)
    if command == "list":
        _write_listing(catalog.list(category), write)
        return 0
    if command == "remove":
        return _remove_requested(catalog, category, selector, remove_all, yes, has_tty, read, write)
    if command is None:
        raise RuntimeError(
            _("cleanup requires an action; run '{command}'").format(
                command="box-rpg cleanup --help"
            )
        )
    if command != "all":
        raise RuntimeError(_("unknown cleanup command: {command}").format(command=command))
    return _remove_global(catalog, yes, has_tty, read, write)


def _remove_requested(
    catalog: CleanupCatalog,
    category: str | None,
    selector: str | None,
    remove_all: bool,
    yes: bool,
    has_tty: bool,
    read: Callable[[str], str],
    write: Callable[[str], None],
) -> int:
    if category not in CATEGORIES:
        raise RuntimeError(_("unknown cleanup category: {category}").format(category=category))
    if remove_all == (selector is not None):
        raise RuntimeError(
            _("cleanup remove requires exactly one of {selector} or {all_option}").format(
                selector="SELECTOR", all_option="--all"
            )
        )
    items = catalog.list(category)
    if selector is not None:
        selected = tuple(item for item in items if item.selector == selector)
        if not selected:
            raise RuntimeError(
                _("no {category} item matches selector: {selector}").format(
                    category=category, selector=selector
                )
            )
        items = selected
    return _confirm_and_remove(catalog, items, yes, has_tty, read, write, all_items=remove_all)


def _remove_global(
    catalog: CleanupCatalog,
    yes: bool,
    has_tty: bool,
    read: Callable[[str], str],
    write: Callable[[str], None],
) -> int:
    return _confirm_and_remove(catalog, catalog.list(), yes, has_tty, read, write, all_items=True)


def _confirm_and_remove(
    catalog: CleanupCatalog,
    items: tuple[CleanupItem, ...],
    yes: bool,
    has_tty: bool,
    read: Callable[[str], str],
    write: Callable[[str], None],
    *,
    all_items: bool,
) -> int:
    _write_scope(items, write)
    confirmation = "DELETE ALL" if all_items else "DELETE"
    if not yes:
        if not has_tty:
            raise RuntimeError(
                _("cleanup requires {yes} without an interactive terminal").format(yes="--yes")
            )
        if not _confirm(
            _("Type {confirmation} to confirm").format(confirmation=confirmation),
            confirmation,
            read,
            write,
        ):
            write(_("Cleanup cancelled."))
            return 0
    result = _remove_items(catalog, items, write)
    write(_removal_summary(result))
    return 1 if result.failed else 0


def _interactive_cleanup(
    catalog: CleanupCatalog,
    read: Callable[[str], str],
    write: Callable[[str], None],
) -> int:
    while True:
        write(_("Cleanup:"))
        for index, category in enumerate(CATEGORIES, start=1):
            write(f"  {index}. {_category_title(category)} ({len(catalog.list(category))})")
        write(_("  a. Remove all listed managed data"))
        try:
            action = read(_("Select 1-4, [a]ll, or [q]uit: ")).strip().lower()
        except EOFError:
            write(_("Cleanup cancelled."))
            return 0
        if action == "q":
            return 0
        if action == "a":
            _interactive_remove(catalog, catalog.list(), read, write, all_items=True)
            continue
        if action.isdigit() and 1 <= int(action) <= len(CATEGORIES):
            category = CATEGORIES[int(action) - 1]
            _interactive_choose(catalog, category, read, write)
            continue
        write(_("Invalid selection."))


def _interactive_choose(
    catalog: CleanupCatalog,
    category: str,
    read: Callable[[str], str],
    write: Callable[[str], None],
) -> None:
    items = catalog.list(category)
    selection = choose_paged(
        _category_title(category),
        items,
        lambda item: item.label,
        allow_all=True,
        read=read,
        write=write,
    )
    if selection is None:
        return
    selected = items if selection.select_all else (selection.item,)
    _interactive_remove(catalog, selected, read, write, all_items=selection.select_all)


def _interactive_remove(
    catalog: CleanupCatalog,
    items: tuple[CleanupItem, ...] | tuple[CleanupItem | None, ...],
    read: Callable[[str], str],
    write: Callable[[str], None],
    *,
    all_items: bool,
) -> None:
    selected = tuple(item for item in items if item is not None)
    confirmation = "DELETE ALL" if all_items else "DELETE"
    if not _confirm(
        _("Type {confirmation} to confirm").format(confirmation=confirmation),
        confirmation,
        read,
        write,
    ):
        write(_("Cleanup cancelled."))
        return
    result = _remove_items(catalog, selected, write)
    write(_removal_summary(result))


def _remove_items(
    catalog: CleanupCatalog, items: tuple[CleanupItem, ...], write: Callable[[str], None]
) -> RemovalResult:
    removed = 0
    failed = 0
    for item in items:
        try:
            catalog.remove(item)
        except (BoxError, OSError) as exc:
            failed += 1
            write(
                _("Could not remove {category} {selector}: {error}").format(
                    category=item.category, selector=item.selector, error=exc
                )
            )
            continue
        removed += 1
    return RemovalResult(removed, failed)


def _removal_summary(result: RemovalResult) -> str:
    """Render the removal result with the correct plural form."""
    return ngettext(
        "Removed {removed} item; {failed} failed.",
        "Removed {removed} items; {failed} failed.",
        result.removed,
    ).format(removed=result.removed, failed=result.failed)


def _write_listing(items: tuple[CleanupItem, ...], write: Callable[[str], None]) -> None:
    """Write JSON Lines records with stable selectors suitable for scripted cleanup."""
    for item in items:
        write(
            json.dumps(
                {"category": item.category, "selector": item.selector, "label": item.label},
                ensure_ascii=False,
            )
        )


def _write_scope(items: tuple[CleanupItem, ...], write: Callable[[str], None]) -> None:
    counts = {category: 0 for category in CATEGORIES}
    for item in items:
        counts[item.category] += 1
    write(_("Cleanup scope:"))
    for category in CATEGORIES:
        write(f"  {_category_title(category)}: {counts[category]}")


def _confirm(
    prompt: str, expected: str, read: Callable[[str], str], write: Callable[[str], None]
) -> bool:
    try:
        return read(f"{prompt}: ").strip() == expected
    except EOFError:
        write(_("Cleanup cancelled."))
        return False


def _runtime_selector(runtime: ManagedRuntime | EasyRPGRuntime) -> str:
    if isinstance(runtime, EasyRPGRuntime):
        return f"easyrpg:{runtime.version}"
    return f"nwjs:{runtime.spec.architecture}:{runtime.spec.directory_name}"


def _render_runtime(runtime: ManagedRuntime | EasyRPGRuntime) -> str:
    if isinstance(runtime, EasyRPGRuntime):
        return f"EasyRPG Player {runtime.version} x64"
    flavor = "SDK" if runtime.spec.sdk else _("standard")
    return f"NW.js {runtime.spec.version} {runtime.spec.architecture} {flavor}"


def _category_title(category: str) -> str:
    return {
        "roots": _("Authorized game roots"),
        "runtimes": _("Managed runtimes"),
        "downloads": _("Download archives"),
        "profiles": _("Game profiles"),
    }[category]
