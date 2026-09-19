"""Online discovery of stable NW.js versions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any, Protocol, cast
from urllib.error import URLError
from urllib.request import Request

from box.errors import RuntimeError
from box.models import RuntimeSpec
from box.paths import AppPaths
from box.runtime.downloader import download_url
from box.runtime.http import open_official, validate_source
from box.runtime.validator import normalize_version
from box.utils.i18n import _

PAGE_SIZE = 10
VERSIONS_INDEX = "https://dl.nwjs.io/"
MAX_INDEX_BYTES = 4 * 1024 * 1024
OFFICIAL_DOWNLOAD_HOSTS = frozenset({"dl.nwjs.io", "dl.node-webkit.org"})
_CONTENT_RANGE_PATTERN = re.compile(r"^bytes\s+0-0/(\d+)\s*$", re.IGNORECASE)


class _Response(Protocol):
    """The subset of an HTTP response used by the online version client."""

    status: int
    headers: Mapping[str, str]

    def __enter__(self) -> _Response: ...

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None: ...

    def close(self) -> None: ...

    def geturl(self) -> str: ...

    def read(self, amount: int = -1) -> bytes: ...


def _empty_sizes() -> dict[str, int | None]:
    """Return a fresh empty size mapping for dataclass defaults."""
    return {}


@dataclass(frozen=True, slots=True)
class AvailableVersions:
    """One page of stable NW.js versions published by the upstream project."""

    page: int
    versions: tuple[str, ...]
    sizes: dict[str, int | None] = field(default_factory=_empty_sizes)


def available_url(page: int) -> str:
    """Validate a client-side page request for the official versions index."""
    if page < 1:
        raise RuntimeError(_("runtime version page must be at least 1"))
    return VERSIONS_INDEX


def fetch_available_versions(
    page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
) -> AvailableVersions:
    """Fetch one client-side page from the official NW.js versions index."""
    available_url(page)
    flavor = "sdk" if sdk else "standard"
    if paths is None:
        return _fetch_network(page, architecture, sdk)
    from box.runtime import listings

    cached = listings.load_nwjs_listing(paths, architecture, flavor, page, normalize_version)
    if cached is not None and listings.is_fresh(cached.fetched_at):
        return AvailableVersions(page=page, versions=cached.versions, sizes=dict(cached.sizes))
    try:
        fresh = _fetch_network(page, architecture, sdk)
    except (OSError, URLError, RuntimeError) as exc:
        if cached is not None:
            return AvailableVersions(page=page, versions=cached.versions, sizes=dict(cached.sizes))
        raise exc
    with suppress(Exception):
        listings.save_nwjs_listing(paths, architecture, flavor, page, fresh.versions, fresh.sizes)
    return fresh


def _fetch_network(page: int, architecture: str, sdk: bool) -> AvailableVersions:
    """Fetch one page without consulting the disk cache."""
    request = Request(
        available_url(page),
        headers={"Accept": "text/html", "User-Agent": "box-rpg"},
    )
    try:
        with _open_official(request) as response:
            content = response.read(MAX_INDEX_BYTES + 1)
    except (OSError, URLError) as exc:
        raise RuntimeError(_("cannot list NW.js versions: {error}").format(error=exc)) from exc
    if len(content) > MAX_INDEX_BYTES:
        raise RuntimeError(_("NW.js version index is too large"))
    versions = parse_versions(content.decode("utf-8", errors="replace"))
    required = page * PAGE_SIZE
    installable: list[str] = []
    probed: dict[str, int | None] = {}
    for version in versions:
        exists, total = runtime_archive_available(RuntimeSpec(version, architecture, sdk))
        if exists:
            installable.append(version)
            probed[version] = total
        if len(installable) == required:
            break
    start = (page - 1) * PAGE_SIZE
    page_versions = tuple(installable[start:required])
    sizes = {version: probed[version] for version in page_versions}
    return AvailableVersions(page=page, versions=page_versions, sizes=sizes)


def parse_versions(content: str) -> tuple[str, ...]:
    """Extract stable version directories and sort them from newest to oldest."""
    parser = _VersionIndexParser()
    parser.feed(content)
    versions: list[str] = []
    for directory in parser.directories:
        try:
            version = normalize_version(directory)
        except RuntimeError:
            continue
        if version not in versions:
            versions.append(version)
    return tuple(sorted(versions, key=_version_key, reverse=True))


@lru_cache
def runtime_archive_available(spec: RuntimeSpec) -> tuple[bool, int | None]:
    """Probe an official archive with a Range request, returning existence and size."""
    request = Request(
        download_url(spec),
        headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0 (compatible; box-rpg)"},
    )
    try:
        with _open_official(request) as response:
            response.read(1)
            return True, _probe_total_size(response)
    except OSError, URLError, RuntimeError:
        return False, None


def _probe_total_size(response: Any) -> int | None:
    """Return the total archive size honoring the response status code."""
    status = getattr(response, "status", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    if status == 206:
        content_range = _header_value(headers, "Content-Range")
        if content_range is None:
            return None
        match = _CONTENT_RANGE_PATTERN.match(content_range.strip())
        if match is None:
            return None
        try:
            total = int(match.group(1))
        except ValueError:
            return None
        return total if total >= 0 else None
    if status == 200:
        content_length = _header_value(headers, "Content-Length")
        if content_length is None:
            return None
        try:
            total = int(content_length.strip())
        except ValueError:
            return None
        return total if total >= 0 else None
    return None


def _header_value(headers: Any, name: str) -> str | None:
    """Return one header value using a case-insensitive lookup."""
    getter = getattr(headers, "get", None)
    if callable(getter):
        try:
            value = getter(name)
        except Exception:
            value = None
        if isinstance(value, str):
            return value
    try:
        items = headers.items()
    except Exception:
        return None
    wanted = name.lower()
    try:
        for key, value in items:
            if isinstance(key, str) and isinstance(value, str) and key.lower() == wanted:
                return value
    except Exception:
        return None
    return None


def _open_official(request: Request) -> _Response:
    """Validate the initial URL and every redirect before contacting official hosts."""
    response = cast(
        _Response, open_official(request, timeout=15, allowed_hosts=OFFICIAL_DOWNLOAD_HOSTS)
    )
    try:
        validate_source(response.geturl(), OFFICIAL_DOWNLOAD_HOSTS)
    except RuntimeError:
        response.close()
        raise
    return response


def _version_key(version: str) -> tuple[int, int, int]:
    """Return the numeric components of a stable NW.js version."""
    major, minor, patch = version.removeprefix("v").split(".")
    return int(major), int(minor), int(patch)


class _VersionIndexParser(HTMLParser):
    """Collect directory links from Apache's NW.js version index."""

    def __init__(self) -> None:
        super().__init__()
        self.directories: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Collect href values whose link names identify version directories."""
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href is not None and href.startswith("v") and href.endswith("/"):
            self.directories.append(href.removesuffix("/"))
