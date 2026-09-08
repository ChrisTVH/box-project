"""Online discovery of stable NW.js versions."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from html.parser import HTMLParser
from typing import Protocol, cast
from urllib.error import URLError
from urllib.request import Request

from box.errors import RuntimeError
from box.models import RuntimeSpec
from box.runtime.downloader import download_url
from box.runtime.http import open_official, validate_source
from box.runtime.validator import normalize_version
from box.utils.i18n import _

PAGE_SIZE = 5
VERSIONS_INDEX = "https://dl.nwjs.io/"
MAX_INDEX_BYTES = 4 * 1024 * 1024
OFFICIAL_DOWNLOAD_HOSTS = frozenset({"dl.nwjs.io", "dl.node-webkit.org"})


class _Response(Protocol):
    """The subset of an HTTP response used by the online version client."""

    def __enter__(self) -> _Response: ...

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None: ...

    def close(self) -> None: ...

    def geturl(self) -> str: ...

    def read(self, amount: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class AvailableVersions:
    """One page of stable NW.js versions published by the upstream project."""

    page: int
    versions: tuple[str, ...]


def available_url(page: int) -> str:
    """Validate a client-side page request for the official versions index."""
    if page < 1:
        raise RuntimeError(_("runtime version page must be at least 1"))
    return VERSIONS_INDEX


def fetch_available_versions(page: int, architecture: str, sdk: bool) -> AvailableVersions:
    """Fetch one client-side page from the official NW.js versions index."""
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
    for version in versions:
        if runtime_archive_available(RuntimeSpec(version, architecture, sdk)):
            installable.append(version)
        if len(installable) == required:
            break
    start = (page - 1) * PAGE_SIZE
    return AvailableVersions(page=page, versions=tuple(installable[start:required]))


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
def runtime_archive_available(spec: RuntimeSpec) -> bool:
    """Return whether an official archive exists for an NW.js variant."""
    request = Request(
        download_url(spec),
        headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0 (compatible; box-rpg)"},
    )
    try:
        with _open_official(request) as response:
            response.read(1)
            return True
    except OSError, URLError:
        return False


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
