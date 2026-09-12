"""Official NW.js archive download and atomic runtime installation."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, suppress
from http.client import IncompleteRead
from pathlib import Path
from typing import BinaryIO, Protocol, Self, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

from box.errors import RuntimeError
from box.models import RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime import limits
from box.runtime.archive import extract_runtime_at
from box.runtime.authenticity import verify_archive
from box.runtime.http import open_official, validate_source
from box.runtime.platform import normalize_architecture
from box.runtime.security import cache_lock, validate_private_file
from box.runtime.validator import normalize_version, validate_runtime_executable_at
from box.utils.i18n import _

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
    locks = ExitStack()
    try:
        locks.enter_context(cache_lock(runtime_descriptor, spec.directory_name))
        locks.enter_context(cache_lock(download_descriptor, archive_name))
        try:
            os.stat(spec.directory_name, dir_fd=runtime_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            download_archive_at(download_url(spec), archive_name, download_descriptor)
            archive_descriptor = _open_regular_file(archive_name, download_descriptor)
            try:
                verify_archive(archive_descriptor, download_url(spec))
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
        raise RuntimeError(
            _("cannot securely install NW.js runtime: {error}").format(error=exc)
        ) from exc
    finally:
        try:
            if temporary_name is not None:
                with suppress(FileNotFoundError):
                    shutil.rmtree(temporary_name, dir_fd=runtime_descriptor)
        finally:
            locks.close()
            os.close(download_descriptor)
            os.close(runtime_descriptor)


def download_archive(
    url: str,
    destination: Path,
    progress: ProgressReporter | None = None,
    allowed_hosts: frozenset[str] = OFFICIAL_DOWNLOAD_HOSTS,
) -> None:
    """Download a private archive, restarting transient failures without mixing bodies."""
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
    with cache_lock(directory_descriptor, destination_name):
        _download_archive_locked(
            url, destination_name, directory_descriptor, progress, allowed_hosts
        )


def _download_archive_locked(
    url: str,
    destination_name: str,
    directory_descriptor: int,
    progress: ProgressReporter | None,
    allowed_hosts: frozenset[str],
) -> None:
    validate_download_source(url, allowed_hosts)
    _ensure_regular_download_entry(destination_name, directory_descriptor)
    if _entry_exists(destination_name, directory_descriptor):
        return
    temporary_name = f"{destination_name}.part"
    _ensure_regular_download_entry(temporary_name, directory_descriptor)
    reporter = _report_download_progress if progress is None else progress
    budget = limits.Budget(limits.MAX_TRANSFER_BYTES)
    for attempt, delay in enumerate((*DOWNLOAD_RETRY_DELAYS, None), start=1):
        try:
            _download_attempt(
                url, temporary_name, directory_descriptor, reporter, allowed_hosts, budget
            )
            budget.check()
            _chmod_regular_file(temporary_name, directory_descriptor, 0o600)
            os.replace(
                temporary_name,
                destination_name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            return
        except HTTPError as exc:
            exc.close()
            if exc.code not in RETRYABLE_HTTP_STATUSES:
                raise RuntimeError(
                    _("cannot download NW.js from {url}: {error}").format(url=url, error=exc)
                ) from exc
            if delay is None:
                raise RuntimeError(
                    _("cannot download NW.js from {url} after {attempt} attempts: {error}").format(
                        url=url, attempt=attempt, error=exc
                    )
                ) from exc
            time.sleep(min(delay, budget.check()))
        except (IncompleteRead, OSError, URLError) as exc:
            if delay is None:
                raise RuntimeError(
                    _("cannot download NW.js from {url} after {attempt} attempts: {error}").format(
                        url=url, attempt=attempt, error=exc
                    )
                ) from exc
            time.sleep(min(delay, budget.check()))


def _download_attempt(
    url: str,
    temporary_name: str,
    directory_descriptor: int,
    progress: ProgressReporter,
    allowed_hosts: frozenset[str],
    budget: limits.Budget,
) -> None:
    """Restart each attempt: no persisted partial is trusted as a representation.

    Without a persisted strong validator, Range could splice different releases.
    Full requests deliberately trade bandwidth for representation integrity.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; box-rpg)", "Accept-Encoding": "identity"}
    request = Request(url, headers=headers)
    response = cast(
        _DownloadResponse,
        open_official(
            request,
            timeout=min(DOWNLOAD_TIMEOUT_SECONDS, budget.check()),
            allowed_hosts=allowed_hosts,
        ),
    )
    with response:
        budget.check()
        validate_download_source(response.geturl(), allowed_hosts)
        status = response.status
        if status != 200:
            raise RuntimeError(
                _("NW.js download returned unexpected HTTP status {status}").format(status=status)
            )
        if response.headers.get("Content-Encoding", "identity") != "identity":
            raise RuntimeError("unexpected archive content encoding")
        total = _archive_size(response, 0)
        if total is not None and (total < 0 or total > budget.remaining):
            raise RuntimeError("runtime resource byte limit exceeded")
        target_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(target_descriptor, "wb") as target:
            validate_private_file(target.fileno())
            os.ftruncate(target.fileno(), 0)
            _copy_response(response, target, 0, total, progress, budget)


def _copy_response(
    response: _DownloadResponse,
    target: BinaryIO,
    completed: int,
    total: int | None,
    progress: ProgressReporter,
    budget: limits.Budget,
) -> None:
    """Copy an HTTP body and reject a body shorter than its declared length."""
    received = 0
    read = cast(Callable[[int], bytes], getattr(response, "read1", response.read))
    progress(completed, total)
    while True:
        budget.check()
        chunk = read(min(64 * 1024, budget.remaining + 1))
        budget.check(len(chunk))
        if not chunk:
            break
        target.write(chunk)
        received += len(chunk)
        progress(completed + received, total)
    content_length = response.headers.get("Content-Length")
    if content_length is not None and received != int(content_length):
        raise IncompleteRead(b"", int(content_length))


def _archive_size(response: _DownloadResponse, completed: int) -> int | None:
    """Return the complete archive size from HTTP response metadata when available."""
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            return completed + int(content_length)
        except ValueError as exc:
            raise RuntimeError("invalid archive content length") from exc
    return None


def _report_download_progress(completed: int, total: int | None) -> None:
    """Render a compact progress bar only when the invoking terminal is interactive."""
    if total is None or not sys.stderr.isatty():
        return
    visible_completed = min(completed, total)
    percentage = 100 if total == 0 else visible_completed * 100 // total
    filled = 30 if total == 0 else visible_completed * 30 // total
    bar = "#" * filled + "-" * (30 - filled)
    sys.stderr.write(
        "\r"
        + _("Downloading NW.js: [{bar}] {percentage:3d}%").format(bar=bar, percentage=percentage)
    )
    if visible_completed == total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def _ensure_regular_download_entry(name: str, directory_descriptor: int) -> None:
    """Reject symlinks and special files without resolving an unpinned path."""
    try:
        entry = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_nlink != 1:
        raise RuntimeError(_("refusing unsafe download path: {name}").format(name=name))


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
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_descriptor
    )
    try:
        validate_private_file(descriptor)
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
    validate_source(url, allowed_hosts)
