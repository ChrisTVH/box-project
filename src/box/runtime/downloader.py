"""Official NW.js archive download and atomic runtime installation."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from http.client import IncompleteRead
from pathlib import Path
from typing import BinaryIO, Protocol, Self, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from box.errors import RuntimeError
from box.models import RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime.archive import extract_runtime_at
from box.runtime.platform import normalize_architecture
from box.runtime.validator import normalize_version, validate_runtime_executable_at

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
    runtime_descriptor = _open_runtime_directory(paths, spec.architecture)
    download_descriptor = _open_downloads_directory(paths)
    archive_name = f"{spec.directory_name}-linux-{spec.architecture}.tar.gz"
    temporary_name: str | None = None
    try:
        try:
            os.stat(spec.directory_name, dir_fd=runtime_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            download_archive_at(download_url(spec), archive_name, download_descriptor)
            archive_descriptor = _open_regular_file(archive_name, download_descriptor)
            try:
                temporary_name = Path(
                    tempfile.mkdtemp(prefix=".install-", dir=f"/proc/self/fd/{runtime_descriptor}")
                ).name
                temporary_descriptor = os.open(
                    temporary_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=runtime_descriptor,
                )
                try:
                    extracted_name = extract_runtime_at(archive_descriptor, temporary_descriptor)
                    extracted_descriptor = os.open(
                        extracted_name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=temporary_descriptor,
                    )
                    try:
                        validate_runtime_executable_at(extracted_descriptor)
                    finally:
                        os.close(extracted_descriptor)
                    os.replace(
                        extracted_name,
                        spec.directory_name,
                        src_dir_fd=temporary_descriptor,
                        dst_dir_fd=runtime_descriptor,
                    )
                finally:
                    os.close(temporary_descriptor)
            finally:
                os.close(archive_descriptor)
        return _runtime_info_at(spec, target, runtime_descriptor)
    except OSError as exc:
        raise RuntimeError(f"cannot securely install NW.js runtime: {exc}") from exc
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                shutil.rmtree(temporary_name, dir_fd=runtime_descriptor)
        os.close(download_descriptor)
        os.close(runtime_descriptor)


def download_archive(
    url: str,
    destination: Path,
    progress: ProgressReporter | None = None,
    allowed_hosts: frozenset[str] = OFFICIAL_DOWNLOAD_HOSTS,
) -> None:
    """Resume a private temporary archive across transient connection failures."""
    descriptor = _open_directory_without_symlinks(destination.parent)
    try:
        download_archive_at(url, destination.name, descriptor, progress, allowed_hosts)
    finally:
        os.close(descriptor)


def download_archive_at(
    url: str,
    destination_name: str,
    directory_descriptor: int,
    progress: ProgressReporter | None = None,
    allowed_hosts: frozenset[str] = OFFICIAL_DOWNLOAD_HOSTS,
) -> None:
    """Download an archive through a pinned containing-directory descriptor."""
    _ensure_regular_download_entry(destination_name, directory_descriptor)
    if _entry_exists(destination_name, directory_descriptor):
        return
    temporary_name = f"{destination_name}.part"
    _ensure_regular_download_entry(temporary_name, directory_descriptor)
    reporter = _report_download_progress if progress is None else progress
    for attempt, delay in enumerate((*DOWNLOAD_RETRY_DELAYS, None), start=1):
        try:
            _download_attempt(url, temporary_name, directory_descriptor, reporter, allowed_hosts)
            _chmod_regular_file(temporary_name, directory_descriptor, 0o600)
            os.replace(
                temporary_name,
                destination_name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
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
    url: str,
    temporary_name: str,
    directory_descriptor: int,
    progress: ProgressReporter,
    allowed_hosts: frozenset[str],
) -> None:
    """Request the remaining archive bytes and append them when the server supports Range."""
    try:
        offset = os.stat(temporary_name, dir_fd=directory_descriptor, follow_symlinks=False).st_size
    except FileNotFoundError:
        offset = 0
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
        target_descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_NOFOLLOW
            | (os.O_APPEND if mode == "ab" else os.O_TRUNC),
            0o600,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(target_descriptor, mode) as target:
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


def _ensure_regular_download_entry(name: str, directory_descriptor: int) -> None:
    """Reject symlinks and special files without resolving an unpinned path."""
    try:
        entry = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(entry.st_mode):
        raise RuntimeError(f"refusing unsafe download path: {name}")


def _entry_exists(name: str, directory_descriptor: int) -> bool:
    """Return whether a validated direct directory entry exists."""
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _chmod_regular_file(name: str, directory_descriptor: int, mode: int) -> None:
    """Set private permissions on a direct regular file without following links."""
    descriptor = _open_regular_file(name, directory_descriptor)
    try:
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _open_regular_file(name: str, directory_descriptor: int) -> int:
    """Open one direct regular file without following a replacement symlink."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"refusing unsafe download path: {name}")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_directory_without_symlinks(path: Path) -> int:
    """Open every absolute directory component without following replacement links."""
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_runtime_directory(paths: AppPaths, architecture: str) -> int:
    """Create and pin the managed NW.js platform directory."""
    root_descriptor = paths.open_managed_cache_directory("runtimes", "nwjs")
    platform_name = f"linux-{architecture}"
    try:
        with suppress(FileExistsError):
            os.mkdir(platform_name, mode=0o700, dir_fd=root_descriptor)
        return os.open(
            platform_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_descriptor,
        )
    finally:
        os.close(root_descriptor)


def _open_downloads_directory(paths: AppPaths) -> int:
    """Pin the managed NW.js downloads directory."""
    return paths.open_managed_cache_directory("downloads", "nwjs")


def _runtime_info_at(spec: RuntimeSpec, target: Path, directory_descriptor: int) -> RuntimeInfo:
    """Validate an installed runtime through its pinned parent directory descriptor."""
    root_descriptor = os.open(
        spec.directory_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=directory_descriptor,
    )
    try:
        validate_runtime_executable_at(root_descriptor)
    finally:
        os.close(root_descriptor)
    return RuntimeInfo(spec, target, target / "nw")


def validate_download_source(
    url: str, allowed_hosts: frozenset[str] = OFFICIAL_DOWNLOAD_HOSTS
) -> None:
    """Reject archive redirects outside the configured official HTTPS hosts."""
    destination = urlsplit(url)
    if destination.scheme != "https" or destination.hostname not in allowed_hosts:
        raise RuntimeError("archive redirected outside an official HTTPS mirror or host")
