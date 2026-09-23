"""Virtual pagination over backend version pages with hide-installed filtering."""

from __future__ import annotations

from collections.abc import Callable

# Backend version pages are fixed at ten items for both engines
# (box.runtime.available.PAGE_SIZE and box.runtime.easyrpg.PAGE_SIZE); this
# constant describes that backend page size, not the virtual window size.
PAGE_SIZE = 10


def resolve_virtual_page(
    fetch_page: Callable[[int], tuple[tuple[str, ...], dict[str, int | None]]],
    is_excluded: Callable[[str], bool],
    virtual_page: int,
    page_size: int = PAGE_SIZE,
    max_backend_pages: int = 50,
) -> tuple[tuple[str, ...], dict[str, int | None]]:
    """Return one virtual page of uninstalled versions, backfilled from later pages.

    Backend fetches return fixed ten-item pages that may shrink when versions
    are hidden after the fetch. Walk backend pages from page one, skipping
    excluded versions, until the requested virtual window is full or the
    backend is exhausted. Deduplicate by version string so repeated stub data
    never inflates the count. Stop early when a backend page is short (last
    page) or contributes nothing new.
    """
    target = max(1, page_size)
    clamped = max(1, virtual_page)
    skip = (clamped - 1) * target
    collected: list[str] = []
    sizes: dict[str, int | None] = {}
    seen: set[str] = set()
    backend_page = 1
    limit = max(1, max_backend_pages)
    while backend_page <= limit:
        versions, raw_sizes = fetch_page(backend_page)
        current = tuple(versions)
        if not current:
            break
        new_in_page = 0
        for version in current:
            if version in seen:
                continue
            seen.add(version)
            new_in_page += 1
            if version in raw_sizes and version not in sizes:
                sizes[version] = raw_sizes[version]
            if is_excluded(version):
                continue
            collected.append(version)
        if len(collected) >= skip + target:
            break
        if len(current) < PAGE_SIZE:
            break
        if new_in_page == 0:
            break
        backend_page += 1
    window = tuple(collected[skip : skip + target])
    window_sizes = {version: sizes[version] for version in window if version in sizes}
    return window, window_sizes
