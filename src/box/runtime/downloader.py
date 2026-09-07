"""Official NW.js archive download and atomic runtime installation."""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Mapping
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


def download_archive(url: str, destination: Path) -> None:
    """Resume a private temporary archive across transient connection failures."""
    _ensure_regular_download_path(destination)
    if destination.exists():
        return
    temporary = destination.with_suffix(destination.suffix + ".part")
    _ensure_regular_download_path(temporary)
    for attempt, delay in enumerate((*DOWNLOAD_RETRY_DELAYS, None), start=1):
        try:
            _download_attempt(url, temporary)
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


def _download_attempt(url: str, temporary: Path) -> None:
    """Request the remaining archive bytes and append them when the server supports Range."""
    offset = temporary.stat().st_size if temporary.exists() else 0
    headers = {"User-Agent": "Mozilla/5.0 (compatible; box-rpg)"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = Request(url, headers=headers)
    response = cast(_DownloadResponse, urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS))
    with response:
        validate_download_source(response.geturl())
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
        with temporary.open(mode) as target:
            _copy_response(response, target)


def _copy_response(response: _DownloadResponse, target: BinaryIO) -> None:
    """Copy an HTTP body and reject a body shorter than its declared length."""
    received = 0
    while chunk := response.read(1024 * 1024):
        target.write(chunk)
        received += len(chunk)
    content_length = response.headers.get("Content-Length")
    if content_length is not None and received != int(content_length):
        raise IncompleteRead(b"", int(content_length))


def _ensure_regular_download_path(path: Path) -> None:
    """Reject symlinks and special files in the launcher-managed download cache."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"refusing unsafe download path: {path}")


def validate_download_source(url: str) -> None:
    """Reject NW.js archive redirects outside official HTTPS mirrors."""
    destination = urlsplit(url)
    if destination.scheme != "https" or destination.hostname not in OFFICIAL_DOWNLOAD_HOSTS:
        raise RuntimeError("NW.js archive redirected outside an official HTTPS mirror")
