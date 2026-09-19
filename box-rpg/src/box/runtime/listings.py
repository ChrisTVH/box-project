"""Disk cache for online version listings with stale-while-offline fallback."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from box.errors import ConfigurationError
from box.paths import AppPaths
from box.runtime.security import cache_lock

LISTINGS_TTL_SECONDS = 3600.0
CLOCK_SKEW_SECONDS = 300.0
NWJS_SOURCE = "nwjs"
EASYRPG_SOURCE = "easyrpg"


@dataclass(frozen=True, slots=True)
class CachedListing:
    """A validated cached listing page with its fetch timestamp."""

    versions: tuple[str, ...]
    sizes: dict[str, int | None]
    fetched_at: float


def is_fresh(fetched_at: float, now: float | None = None) -> bool:
    """Return whether a cached timestamp is still within the listing TTL."""
    current = time.time() if now is None else now
    if fetched_at > current:
        return fetched_at - current <= CLOCK_SKEW_SECONDS
    age = current - fetched_at
    return 0 <= age <= LISTINGS_TTL_SECONDS


def nwjs_filename(architecture: str, flavor: str, page: int) -> str:
    """Return the cache filename for one NW.js listing page."""
    return f"nwjs-{architecture}-{flavor}-p{page}.json"


def easyrpg_filename(page: int) -> str:
    """Return the cache filename for one EasyRPG Player listing page."""
    return f"easyrpg-p{page}.json"


def nwjs_listing_path(paths: AppPaths, architecture: str, flavor: str, page: int) -> Path:
    """Return the validated managed path for one NW.js listing page."""
    candidate = paths.listings_root / "nwjs" / nwjs_filename(architecture, flavor, page)
    return paths.ensure_managed_listing_path(candidate)


def easyrpg_listing_path(paths: AppPaths, page: int) -> Path:
    """Return the validated managed path for one EasyRPG Player listing page."""
    candidate = paths.listings_root / "easyrpg" / easyrpg_filename(page)
    return paths.ensure_managed_listing_path(candidate)


def load_nwjs_listing(
    paths: AppPaths,
    architecture: str,
    flavor: str,
    page: int,
    parse_version: Callable[[str], str],
) -> CachedListing | None:
    """Return a valid cached NW.js page or None when missing or corrupt."""
    try:
        target = nwjs_listing_path(paths, architecture, flavor, page)
    except ConfigurationError, OSError:
        return None
    document = _read_document(target)
    if document is None:
        return None
    source: object = document.get("source")
    arch: object = document.get("arch")
    flavor_value: object = document.get("flavor")
    page_value: object = document.get("page")
    if (
        source != NWJS_SOURCE
        or arch != architecture
        or flavor_value != flavor
        or page_value != page
    ):
        return None
    return _validated_payload(document, parse_version)


def save_nwjs_listing(
    paths: AppPaths,
    architecture: str,
    flavor: str,
    page: int,
    versions: tuple[str, ...] | list[str],
    sizes: dict[str, int | None],
) -> None:
    """Atomically persist one NW.js listing page under the cache lock."""
    ordered = tuple(versions)
    payload: dict[str, Any] = {
        "source": NWJS_SOURCE,
        "arch": architecture,
        "flavor": flavor,
        "page": page,
        "fetched_at": time.time(),
        "versions": list(ordered),
        "sizes": dict(sizes),
    }
    target = nwjs_listing_path(paths, architecture, flavor, page)
    _atomic_write_locked(paths, ("listings", "nwjs"), target.name, target, payload)


def load_easyrpg_listing(
    paths: AppPaths, page: int, parse_version: Callable[[str], str]
) -> CachedListing | None:
    """Return a valid cached EasyRPG Player page or None when missing or corrupt."""
    try:
        target = easyrpg_listing_path(paths, page)
    except ConfigurationError, OSError:
        return None
    document = _read_document(target)
    if document is None:
        return None
    source: object = document.get("source")
    page_value: object = document.get("page")
    if source != EASYRPG_SOURCE or page_value != page:
        return None
    return _validated_payload(document, parse_version)


def save_easyrpg_listing(
    paths: AppPaths,
    page: int,
    versions: tuple[str, ...] | list[str],
    sizes: dict[str, int | None],
) -> None:
    """Atomically persist one EasyRPG Player listing page under the cache lock."""
    ordered = tuple(versions)
    payload: dict[str, Any] = {
        "source": EASYRPG_SOURCE,
        "page": page,
        "fetched_at": time.time(),
        "versions": list(ordered),
        "sizes": dict(sizes),
    }
    target = easyrpg_listing_path(paths, page)
    _atomic_write_locked(paths, ("listings", "easyrpg"), target.name, target, payload)


def _read_document(target: Path) -> dict[str, object] | None:
    """Read and decode one JSON listing document, returning None when invalid."""
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    try:
        loaded: object = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return None
    if not isinstance(loaded, dict):
        return None
    return cast(dict[str, object], loaded)


def _validated_payload(
    document: dict[str, object], parse_version: Callable[[str], str]
) -> CachedListing | None:
    """Validate versions and sizes, returning None for any corrupt entry."""
    fetched_raw: object = document.get("fetched_at")
    if isinstance(fetched_raw, bool) or not isinstance(fetched_raw, (int, float)):
        return None
    versions_raw: object = document.get("versions")
    if not isinstance(versions_raw, list):
        return None
    raw_list = cast(list[object], versions_raw)
    versions: list[str] = []
    for entry_raw in raw_list:
        if not isinstance(entry_raw, str):
            return None
        try:
            normalized = parse_version(entry_raw)
        except Exception:
            return None
        if normalized != entry_raw:
            return None
        if entry_raw not in versions:
            versions.append(entry_raw)
        else:
            return None
    if list(raw_list) != versions:
        return None
    sizes_raw: object = document.get("sizes")
    if not isinstance(sizes_raw, dict):
        return None
    raw_sizes = cast(dict[str, object], sizes_raw)
    sizes: dict[str, int | None] = {}
    for key, value in raw_sizes.items():
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            return None
        size_value: int | None = value if isinstance(value, int) else None
        if value is not None and size_value is None:
            return None
        sizes[key] = size_value
    if set(sizes.keys()) != set(versions):
        return None
    fetched_value: int | float = fetched_raw
    return CachedListing(versions=tuple(versions), sizes=sizes, fetched_at=float(fetched_value))


def _atomic_write_locked(
    paths: AppPaths,
    components: tuple[str, ...],
    lock_name: str,
    target: Path,
    payload: dict[str, Any],
) -> None:
    """Write JSON atomically with mkstemp/fsync/chmod/replace under the cache lock."""
    descriptor = paths.open_or_create_private_cache_directory(*components)
    try:
        with cache_lock(descriptor, lock_name):
            content = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
            parent = target.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor_tmp, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor_tmp, "wb", closefd=True) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, target)
                with suppress(OSError):
                    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
            finally:
                if temporary.exists():
                    with suppress(OSError):
                        temporary.unlink()
    finally:
        os.close(descriptor)
