"""Pure unit tests for virtual pagination over backend version pages."""

from __future__ import annotations

from collections.abc import Callable

from box.runtime.paging import resolve_virtual_page


def _stub_fetch(
    pages: dict[int, tuple[tuple[str, ...], dict[str, int | None]]],
    seen_pages: list[int] | None = None,
) -> Callable[[int], tuple[tuple[str, ...], dict[str, int | None]]]:
    """Build a fetch_page stub serving disjoint per-page data."""

    def fetch_page(backend_page: int) -> tuple[tuple[str, ...], dict[str, int | None]]:
        if seen_pages is not None:
            seen_pages.append(backend_page)
        versions, sizes = pages.get(backend_page, ((), {}))
        return versions, dict(sizes)

    return fetch_page


def test_backfills_excluded_versions_from_next_page() -> None:
    """One excluded version on page one pulls the first item from page two."""
    first = tuple(f"v0.{20 - index}.0" for index in range(10))
    second = tuple(f"v0.{10 - index}.0" for index in range(10))
    sizes = {version: 100 for version in (*first, *second)}
    seen: list[int] = []
    fetch_page = _stub_fetch({1: (first, sizes), 2: (second, sizes)}, seen)

    def is_excluded(version: str) -> bool:
        return version == first[0]

    window, window_sizes = resolve_virtual_page(fetch_page, is_excluded, 1)

    assert len(window) == 10
    assert first[0] not in window
    assert window[:9] == first[1:]
    assert window[9] == second[0]
    assert window_sizes[second[0]] == 100
    assert seen == [1, 2]


def test_second_virtual_page_skips_borrowed_without_duplication() -> None:
    """Virtual page two starts after the item borrowed by virtual page one."""
    first = tuple(f"v0.{20 - index}.0" for index in range(10))
    second = tuple(f"v0.{10 - index}.0" for index in range(10))
    sizes = {version: 100 for version in (*first, *second)}
    fetch_page = _stub_fetch({1: (first, sizes), 2: (second, sizes)})

    def is_excluded(version: str) -> bool:
        return version == first[0]

    borrowed, _ = resolve_virtual_page(fetch_page, is_excluded, 1)
    window, window_sizes = resolve_virtual_page(fetch_page, is_excluded, 2)

    assert borrowed[9] == second[0]
    assert borrowed[9] not in window
    assert window == second[1:]
    assert set(window) == set(window_sizes)
    assert len(set(window)) == len(window)


def test_empty_backend_returns_empty_window() -> None:
    """An empty backend yields an empty virtual page with no sizes."""

    def fetch_page(_backend_page: int) -> tuple[tuple[str, ...], dict[str, int | None]]:
        return (), {}

    def is_excluded(_version: str) -> bool:
        return False

    window, window_sizes = resolve_virtual_page(fetch_page, is_excluded, 1)

    assert window == ()
    assert window_sizes == {}


def test_all_excluded_returns_empty_window() -> None:
    """A backend with only installed versions yields an empty virtual page."""
    first = tuple(f"v0.{20 - index}.0" for index in range(10))
    sizes = {version: 10 for version in first}
    seen: list[int] = []
    fetch_page = _stub_fetch({1: (first, sizes)}, seen)

    def is_excluded(_version: str) -> bool:
        return True

    window, window_sizes = resolve_virtual_page(fetch_page, is_excluded, 1)

    assert window == ()
    assert window_sizes == {}
    assert seen == [1, 2]


def test_repeated_stub_terminates_without_duplicates() -> None:
    """Identical backend pages stop on no progress and never duplicate."""
    repeated = tuple(f"v0.{10 - index}.0" for index in range(10))
    sizes = {version: 7 for version in repeated}
    seen: list[int] = []

    def fetch_page(backend_page: int) -> tuple[tuple[str, ...], dict[str, int | None]]:
        seen.append(backend_page)
        return repeated, dict(sizes)

    def is_excluded(_version: str) -> bool:
        return False

    first, _ = resolve_virtual_page(fetch_page, is_excluded, 1)
    assert first == repeated

    seen.clear()
    window, window_sizes = resolve_virtual_page(fetch_page, is_excluded, 2)

    assert window == ()
    assert window_sizes == {}
    assert seen == [1, 2]


def test_short_page_stops_early() -> None:
    """A short backend page is the last page even when more data exists."""
    first = ("v0.90.0", "v0.89.0", "v0.88.0")
    sizes = {"v0.90.0": 5, "v0.89.0": 6, "v0.88.0": None}
    second = tuple(f"v0.{80 - index}.0" for index in range(10))
    seen: list[int] = []
    fetch_page = _stub_fetch({1: (first, sizes), 2: (second, sizes)}, seen)

    def is_excluded(_version: str) -> bool:
        return False

    window, window_sizes = resolve_virtual_page(fetch_page, is_excluded, 1)

    assert window == first
    assert window_sizes == {"v0.90.0": 5, "v0.89.0": 6, "v0.88.0": None}
    assert seen == [1]
