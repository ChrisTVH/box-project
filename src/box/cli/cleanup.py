"""Safe interactive and scripted cleanup of launcher-managed data."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

from box.api.cleanup import CATEGORIES, CleanupCatalog, CleanupItem, RemovalResult
from box.cli.menu import choose_paged
from box.config.repository import ConfigRepository
from box.errors import BoxError, RuntimeError
from box.paths import AppPaths
from box.utils.i18n import _, ngettext
from box.utils.terminal import abbreviate_prompt_path, safe_terminal_text


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


def _render_cleanup_item(item: CleanupItem) -> str:
    """Render one cleanup item for the interactive menu without altering stored data."""
    if item.category == "roots":
        assert isinstance(item.value, Path)
        available = max(shutil.get_terminal_size().columns - len("  10. "), 1)
        return safe_terminal_text(abbreviate_prompt_path(item.value, available, Path.home()))
    return item.label


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
        _render_cleanup_item,
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


def _category_title(category: str) -> str:
    return {
        "roots": _("Authorized game roots"),
        "runtimes": _("Managed runtimes"),
        "downloads": _("Download archives"),
        "profiles": _("Game profiles"),
    }[category]
