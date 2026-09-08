"""Official NW.js archive download and atomic runtime installation."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from http.client import IncompleteRead
from pathlib import Path
from typing import BinaryIO, Protocol, Self, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from box.errors import RuntimeError
from box.models import RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime.archive import extract_runtime
from box.runtime.platform import normalize_architecture
from box.runtime.validator import normalize_version, runtime_executable

OFFICIAL_DOWNLOAD_HOSTS = frozenset({"dl.nwjs.io", "dl.node-webkit.org"})
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_RETRY_DELAYS = (1, 2, 4)
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
ProgressReporter = Callable[[int, int | None], None]


class _DownloadResponse(Protocol):
    """The response methods needed to stream a NW.js archive."""

    status: int
    headers: Mapping[str, str]

    def __enter__(self) -> Self: ...

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None: ...

    def geturl(self) -> str: ...

    def read(self, amount: int = -1) -> bytes: ...


def download_url(spec: RuntimeSpec) -> str:
    """Return the official HTTPS archive URL for a requested runtime."""
    prefix = "nwjs-sdk" if spec.sdk else "nwjs"
    filename = f"{prefix}-{spec.version}-linux-{spec.architecture}.tar.gz"
    return f"https://dl.nwjs.io/{spec.version}/{filename}"


def install_runtime(
    paths: AppPaths, version: str, architecture: str, sdk: bool = False
) -> RuntimeInfo:
    """Download, extract and atomically install an official NW.js runtime."""
    spec = RuntimeSpec(normalize_version(version), normalize_architecture(architecture), sdk)
    paths.ensure()
    target = paths.runtimes_root / f"linux-{spec.architecture}" / spec.directory_name
    paths.ensure_managed_runtime_path(target)
    if target.exists():
        executable = runtime_executable(target)
        return RuntimeInfo(spec, target, executable)
    archive = paths.downloads_root / f"{spec.directory_name}-linux-{spec.architecture}.tar.gz"
    download_archive(download_url(spec), archive)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".install-", dir=target.parent) as temporary_name:
        temporary = Path(temporary_name)
        extracted_root = extract_runtime(archive, temporary)
        runtime_executable(extracted_root)
        os.replace(extracted_root, target)
    executable = runtime_executable(target)
    return RuntimeInfo(spec, target, executable)


def download_archive(
    url: str,
    destination: Path,
    progress: ProgressReporter | None = None,
    allowed_hosts: frozenset[str] = OFFICIAL_DOWNLOAD_HOSTS,
) -> None:
    """Resume a private temporary archive across transient connection failures."""
    _ensure_regular_download_path(destination)
    if destination.exists():
        return
    temporary = destination.with_suffix(destination.suffix + ".part")
    _ensure_regular_download_path(temporary)
    reporter = _report_download_progress if progress is None else progress
    for attempt, delay in enumerate((*DOWNLOAD_RETRY_DELAYS, None), start=1):
        try:
            _download_attempt(url, temporary, reporter, allowed_hosts)
            temporary.chmod(0o600)
            os.replace(temporary, destination)
            return
        except HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_STATUSES:
                raise RuntimeError(f"cannot download NW.js from {url}: {exc}") from exc
            if delay is None:
                raise RuntimeError(
                    f"cannot download NW.js from {url} after {attempt} attempts: {exc}"
                ) from exc
            time.sleep(delay)
        except (IncompleteRead, OSError, URLError) as exc:
            if delay is None:
                raise RuntimeError(
                    f"cannot download NW.js from {url} after {attempt} attempts: {exc}"
                ) from exc
            time.sleep(delay)


def _download_attempt(
    url: str, temporary: Path, progress: ProgressReporter, allowed_hosts: frozenset[str]
) -> None:
    """Request the remaining archive bytes and append them when the server supports Range."""
    offset = temporary.stat().st_size if temporary.exists() else 0
    headers = {"User-Agent": "Mozilla/5.0 (compatible; box-rpg)"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = Request(url, headers=headers)
    response = cast(_DownloadResponse, urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS))
    with response:
        validate_download_source(response.geturl(), allowed_hosts)
        status = response.status
        if offset and status == 206:
            content_range = response.headers.get("Content-Range", "")
            if not content_range.startswith(f"bytes {offset}-"):
                raise RuntimeError("NW.js download returned an invalid resume range")
            mode = "ab"
        elif status == 200:
            mode = "wb"
        else:
            raise RuntimeError(f"NW.js download returned unexpected HTTP status {status}")
        completed = offset if mode == "ab" else 0
        total = _archive_size(response, completed)
        with temporary.open(mode) as target:
            _copy_response(response, target, completed, total, progress)


def _copy_response(
    response: _DownloadResponse,
    target: BinaryIO,
    completed: int,
    total: int | None,
    progress: ProgressReporter,
) -> None:
    """Copy an HTTP body and reject a body shorter than its declared length."""
    received = 0
    progress(completed, total)
    while chunk := response.read(1024 * 1024):
        target.write(chunk)
        received += len(chunk)
        progress(completed + received, total)
    content_length = response.headers.get("Content-Length")
    if content_length is not None and received != int(content_length):
        raise IncompleteRead(b"", int(content_length))


def _archive_size(response: _DownloadResponse, completed: int) -> int | None:
    """Return the complete archive size from HTTP response metadata when available."""
    content_range = response.headers.get("Content-Range")
    if content_range is not None and "/" in content_range:
        total = content_range.rsplit("/", maxsplit=1)[1]
        if total.isdigit():
            return int(total)
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        return completed + int(content_length)
    return None


def _report_download_progress(completed: int, total: int | None) -> None:
    """Render a compact progress bar only when the invoking terminal is interactive."""
    if total is None or not sys.stderr.isatty():
        return
    visible_completed = min(completed, total)
    percentage = 100 if total == 0 else visible_completed * 100 // total
    filled = 30 if total == 0 else visible_completed * 30 // total
    bar = "#" * filled + "-" * (30 - filled)
    sys.stderr.write(f"\rDownloading NW.js: [{bar}] {percentage:3d}%")
    if visible_completed == total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def _ensure_regular_download_path(path: Path) -> None:
    """Reject symlinks and special files in the launcher-managed download cache."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"refusing unsafe download path: {path}")


def validate_download_source(
    url: str, allowed_hosts: frozenset[str] = OFFICIAL_DOWNLOAD_HOSTS
) -> None:
    """Reject archive redirects outside the configured official HTTPS hosts."""
    destination = urlsplit(url)
    if destination.scheme != "https" or destination.hostname not in allowed_hosts:
        raise RuntimeError("archive redirected outside an official HTTPS mirror or host")
