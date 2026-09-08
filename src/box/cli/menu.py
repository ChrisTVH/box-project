"""Reusable paginated selection prompts for interactive commands."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from box.utils.i18n import _

PAGE_SIZE = 5


@dataclass(frozen=True, slots=True)
class MenuSelection[T]:
    """One item or an all-items action chosen from a paginated menu."""

    item: T | None = None
    select_all: bool = False


def choose_paged[T](
    title: str,
    items: Sequence[T],
    render: Callable[[T], str],
    *,
    allow_all: bool = False,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> MenuSelection[T] | None:
    """Interactively select one item, all items, or cancel without side effects."""
    if not items:
        write(f"{title}: {_('(no entries)')}")
        return None
    page = 1
    total_pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE
    while True:
        start = (page - 1) * PAGE_SIZE
        visible = items[start : start + PAGE_SIZE]
        write(
            _("{title} (page {page}/{total_pages}):").format(
                title=title, page=page, total_pages=total_pages
            )
        )
        for index, item in enumerate(visible, start=1):
            write(f"  {index}. {render(item)}")
        prompt = _("Select 1-5, [n]ext, [p]revious")
        if allow_all:
            prompt += _(", [a]ll")
        prompt += _(", or [q]uit: ")
        try:
            action = read(prompt).strip().lower()
        except EOFError:
            write(_("Selection cancelled."))
            return None
        if action == "q":
            return None
        if action == "n":
            page = min(total_pages, page + 1)
            continue
        if action == "p":
            page = max(1, page - 1)
            continue
        if action == "a" and allow_all:
            return MenuSelection(select_all=True)
        if action.isdigit():
            index = int(action) - 1
            if 0 <= index < len(visible):
                return MenuSelection(item=visible[index])
        write(_("Invalid selection."))
