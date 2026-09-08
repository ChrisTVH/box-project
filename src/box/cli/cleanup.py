"""Interactive cleanup of launcher-managed configuration and cache data."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from box.cli.menu import MenuSelection, choose_paged
from box.config.repository import ConfigRepository
from box.errors import BoxError, RuntimeError
from box.paths import AppPaths
from box.runtime.catalog import ManagedRuntime, RuntimeCatalog
from box.runtime.downloads import DownloadCatalog
from box.runtime.easyrpg import EasyRPGCatalog, EasyRPGDownloadCatalog, EasyRPGRuntime


def execute(
    paths: AppPaths,
    repository: ConfigRepository,
    *,
    interactive: bool,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> int:
    """Open the cleanup menu when the command has an interactive terminal."""
    if not interactive:
        raise RuntimeError("cleanup requires an interactive terminal")
    runtimes = RuntimeCatalog(paths)
    easyrpg_runtimes = EasyRPGCatalog(paths)
    downloads = DownloadCatalog(paths)
    easyrpg_downloads = EasyRPGDownloadCatalog(paths)
    while True:
        roots = repository.load().allowed_game_roots
        managed_runtimes = (*runtimes.list_managed(), *easyrpg_runtimes.list_managed())
        archives = (*downloads.list(), *easyrpg_downloads.list())
        write("Cleanup:")
        write(f"  1. Authorized game roots ({len(roots)})")
        write(f"  2. Managed runtimes ({len(managed_runtimes)})")
        write(f"  3. Download archives ({len(archives)})")
        write("  a. Remove all listed managed data")
        try:
            action = read("Select 1-3, [a]ll, or [q]uit: ").strip().lower()
        except EOFError:
            write("Cleanup cancelled.")
            return 0
        if action == "q":
            return 0
        if action == "1":
            _clean_roots(repository, roots, read, write)
        elif action == "2":
            _clean_runtimes(runtimes, easyrpg_runtimes, managed_runtimes, read, write)
        elif action == "3":
            _clean_downloads(downloads, easyrpg_downloads, archives, read, write)
        elif action == "a":
            _clean_all(
                repository,
                runtimes,
                easyrpg_runtimes,
                downloads,
                easyrpg_downloads,
                roots,
                managed_runtimes,
                archives,
                read,
                write,
            )
        else:
            write("Invalid selection.")


def _clean_roots(
    repository: ConfigRepository,
    roots: tuple[Path, ...],
    read: Callable[[str], str],
    write: Callable[[str], None],
) -> None:
    selection = choose_paged(
        "Authorized game roots", roots, str, allow_all=True, read=read, write=write
    )
    if selection is None:
        return
    if selection.select_all:
        if _confirm("Remove all authorized game roots", read, write):
            repository.clear_allowed_roots()
            write("Removed all authorized game roots.")
        return
    assert selection.item is not None
    if _confirm(f"Remove authorized game root {selection.item}", read, write):
        repository.remove_allowed_root(selection.item)
        write(f"Removed authorized game root {selection.item}.")


def _clean_runtimes(
    catalog: RuntimeCatalog,
    easyrpg_catalog: EasyRPGCatalog,
    runtimes: tuple[ManagedRuntime | EasyRPGRuntime, ...],
    read: Callable[[str], str],
    write: Callable[[str], None],
) -> None:
    selection = choose_paged(
        "Managed runtimes",
        runtimes,
        _render_runtime,
        allow_all=True,
        read=read,
        write=write,
    )
    _clean_selected(
        selection,
        runtimes,
        lambda runtime: _remove_runtime(runtime, catalog, easyrpg_catalog),
        "runtime",
        read,
        write,
    )


def _clean_downloads(
    catalog: DownloadCatalog,
    easyrpg_catalog: EasyRPGDownloadCatalog,
    archives: tuple[Path, ...],
    read: Callable[[str], str],
    write: Callable[[str], None],
) -> None:
    selection = choose_paged(
        "Download archives",
        archives,
        lambda archive: archive.name,
        allow_all=True,
        read=read,
        write=write,
    )
    _clean_selected(
        selection,
        archives,
        lambda archive: _remove_download(archive, catalog, easyrpg_catalog),
        "download archive",
        read,
        write,
    )


def _clean_selected[T](
    selection: MenuSelection[T] | None,
    items: tuple[T, ...],
    remove: Callable[[T], None],
    label: str,
    read: Callable[[str], str],
    write: Callable[[str], None],
) -> None:
    if selection is None:
        return
    if selection.select_all:
        if _confirm(f"Remove all {label}s", read, write):
            removed = _remove_all(items, remove, label, write)
            write(f"Removed {removed} {label}s.")
        return
    assert selection.item is not None
    if _confirm(f"Remove {label} {selection.item}", read, write):
        remove(selection.item)
        write(f"Removed {label}.")


def _clean_all(
    repository: ConfigRepository,
    runtimes: RuntimeCatalog,
    easyrpg_runtimes: EasyRPGCatalog,
    downloads: DownloadCatalog,
    easyrpg_downloads: EasyRPGDownloadCatalog,
    roots: tuple[Path, ...],
    managed_runtimes: tuple[ManagedRuntime | EasyRPGRuntime, ...],
    archives: tuple[Path, ...],
    read: Callable[[str], str],
    write: Callable[[str], None],
) -> None:
    write(
        f"This removes {len(roots)} roots, {len(managed_runtimes)} runtimes, and {len(archives)} downloads."
    )
    try:
        confirmation = read("Type DELETE ALL to confirm: ").strip()
    except EOFError:
        write("Cleanup cancelled.")
        return
    if confirmation != "DELETE ALL":
        write("Cleanup cancelled.")
        return
    repository.clear_allowed_roots()
    removed_runtimes = _remove_all(
        managed_runtimes,
        lambda runtime: _remove_runtime(runtime, runtimes, easyrpg_runtimes),
        "runtime",
        write,
    )
    removed_archives = _remove_all(
        archives,
        lambda archive: _remove_download(archive, downloads, easyrpg_downloads),
        "download archive",
        write,
    )
    write(
        f"Removed {len(roots)} roots, {removed_runtimes} runtimes, and {removed_archives} downloads."
    )


def _remove_all[T](
    items: tuple[T, ...], remove: Callable[[T], None], label: str, write: Callable[[str], None]
) -> int:
    """Remove every selected managed item while reporting individual failures."""
    removed = 0
    for item in items:
        try:
            remove(item)
        except (BoxError, OSError) as exc:
            write(f"Could not remove {label} {item}: {exc}")
            continue
        removed += 1
    return removed


def _confirm(label: str, read: Callable[[str], str], write: Callable[[str], None]) -> bool:
    """Require an affirmative answer before one destructive action."""
    try:
        answer = read(f"{label}? [y/N] ").strip().lower()
    except EOFError:
        write("Cleanup cancelled.")
        return False
    return answer in {"y", "yes"}


def _render_runtime(runtime: ManagedRuntime | EasyRPGRuntime) -> str:
    """Render one managed runtime with its owning provider."""
    if isinstance(runtime, EasyRPGRuntime):
        return f"EasyRPG Player {runtime.version} x64"
    return f"NW.js {runtime.spec.version} {runtime.spec.architecture} {runtime.spec.flavor}"


def _remove_runtime(
    runtime: ManagedRuntime | EasyRPGRuntime,
    nwjs: RuntimeCatalog,
    easyrpg: EasyRPGCatalog,
) -> None:
    """Delegate deletion to the runtime provider that owns the selected directory."""
    if isinstance(runtime, EasyRPGRuntime):
        easyrpg.remove_managed(runtime)
    else:
        nwjs.remove_managed(runtime)


def _remove_download(archive: Path, nwjs: DownloadCatalog, easyrpg: EasyRPGDownloadCatalog) -> None:
    """Delegate deletion to the download provider that owns the archive path."""
    if easyrpg.owns(archive):
        easyrpg.remove(archive)
    else:
        nwjs.remove(archive)
