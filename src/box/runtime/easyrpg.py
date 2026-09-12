"""EasyRPG Player x64 version discovery and managed-runtime catalog."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tarfile
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol, cast
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request

from box.errors import ConfigurationError, RuntimeError
from box.paths import AppPaths
from box.runtime.downloader import download_archive_at
from box.runtime.http import open_official, validate_source
from box.runtime.limits import extract_bounded
from box.runtime.platform import current_architecture
from box.runtime.security import cache_lock, validate_private_file, validate_runtime_links
from box.utils.i18n import _

PAGE_SIZE = 10
VERSIONS_INDEX = "https://easyrpg.org/downloads/player/"
MAX_INDEX_BYTES = 4 * 1024 * 1024
_OFFICIAL_HOST = "easyrpg.org"
OFFICIAL_DOWNLOAD_HOSTS = frozenset({_OFFICIAL_HOST})
_ARCHIVE_PATTERN = re.compile(r"^easyrpg-player-\d+(?:\.\d+){1,3}-linux\.tar\.gz(?:\.part)?$")


@dataclass(frozen=True, slots=True)
class AvailableEasyRPGVersions:
    """One page of EasyRPG Player versions published by the upstream project."""

    page: int
    versions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EasyRPGRuntime:
    """A launcher-owned EasyRPG Player x64 runtime directory."""

    version: str
    root: Path


class _EasyRPGPaths(Protocol):
    """The EasyRPG-specific managed-path interface supplied by ``AppPaths``."""

    @property
    def easyrpg_downloads_root(self) -> Path: ...

    @property
    def easyrpg_runtimes_root(self) -> Path: ...

    def ensure_managed_easyrpg_download_path(self, path: Path) -> Path: ...

    def ensure_managed_easyrpg_runtime_path(self, path: Path) -> Path: ...

    def open_managed_cache_directory(self, *components: str) -> int: ...


class _Response(Protocol):
    """The subset of an HTTP response used by the version index client."""

    def __enter__(self) -> _Response: ...

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None: ...

    def geturl(self) -> str: ...

    def read(self, amount: int = -1) -> bytes: ...


def normalize_version(value: str) -> str:
    """Validate an EasyRPG Player release version without changing its form."""
    components = value.split(".")
    if not 2 <= len(components) <= 4 or any(not component.isdecimal() for component in components):
        raise RuntimeError(_("invalid EasyRPG Player version: {value!r}").format(value=value))
    return value


def available_url(page: int) -> str:
    """Validate a client-side request for an EasyRPG Player index page."""
    _validate_page(page)
    return VERSIONS_INDEX


def download_url(version: str) -> str:
    """Return the official Linux x64 archive URL for an EasyRPG Player version."""
    normalized = normalize_version(version)
    filename = _archive_name(normalized)
    return f"{VERSIONS_INDEX}{normalized}/{filename}"


def download_archive_path(paths: AppPaths, version: str) -> Path:
    """Return the validated managed archive path for a future EasyRPG download."""
    easyrpg_paths = cast(_EasyRPGPaths, paths)
    archive = easyrpg_paths.easyrpg_downloads_root / _archive_name(normalize_version(version))
    return easyrpg_paths.ensure_managed_easyrpg_download_path(archive)


def fetch_available_versions(page: int) -> AvailableEasyRPGVersions:
    """Fetch one client-side page from the official EasyRPG Player version index."""
    request = Request(available_url(page), headers={"Accept": "text/html", "User-Agent": "box-rpg"})
    try:
        with cast(
            _Response, open_official(request, timeout=15, allowed_hosts=OFFICIAL_DOWNLOAD_HOSTS)
        ) as response:
            validate_source(response.geturl(), OFFICIAL_DOWNLOAD_HOSTS)
            content = response.read(MAX_INDEX_BYTES + 1)
    except (OSError, URLError) as exc:
        raise RuntimeError(
            _("cannot list EasyRPG Player versions: {error}").format(error=exc)
        ) from exc
    if len(content) > MAX_INDEX_BYTES:
        raise RuntimeError(_("EasyRPG Player version index is too large"))
    return parse_available_versions(content.decode("utf-8", errors="replace"), page)


def parse_available_versions(content: str, page: int) -> AvailableEasyRPGVersions:
    """Parse one ten-item client-side page from a local EasyRPG Player index."""
    _validate_page(page)
    versions = parse_versions(content)
    start = (page - 1) * PAGE_SIZE
    return AvailableEasyRPGVersions(page=page, versions=versions[start : start + PAGE_SIZE])


def parse_versions(content: str) -> tuple[str, ...]:
    """Extract and order numeric EasyRPG Player release directories newest first."""
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


class EasyRPGCatalog:
    """Inspect, select, and remove launcher-owned EasyRPG Player x64 runtimes."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths

    def list(self) -> tuple[EasyRPGRuntime, ...]:
        """Return valid managed EasyRPG Player runtimes from newest to oldest."""
        return tuple(
            runtime for runtime in self.list_managed() if _executable(runtime.root) is not None
        )

    def list_managed(self) -> tuple[EasyRPGRuntime, ...]:
        """Return canonical managed runtime directories, including incomplete installs."""
        self._paths.ensure()
        easyrpg_paths = cast(_EasyRPGPaths, self._paths)
        runtimes: list[EasyRPGRuntime] = []
        for directory in easyrpg_paths.easyrpg_runtimes_root.iterdir():
            runtime = _managed_runtime(directory)
            if runtime is None:
                continue
            try:
                root = easyrpg_paths.ensure_managed_easyrpg_runtime_path(runtime.root)
            except ConfigurationError:
                continue
            runtimes.append(EasyRPGRuntime(runtime.version, root))
        return tuple(
            sorted(runtimes, key=lambda runtime: _version_key(runtime.version), reverse=True)
        )

    def get(self, version: str) -> EasyRPGRuntime:
        """Return one installed EasyRPG Player runtime or raise a clear error."""
        normalized = normalize_version(version)
        easyrpg_paths = cast(_EasyRPGPaths, self._paths)
        root = easyrpg_paths.easyrpg_runtimes_root / normalized
        managed = easyrpg_paths.ensure_managed_easyrpg_runtime_path(root)
        if _executable(managed) is None:
            raise RuntimeError(
                _("EasyRPG Player runtime is not installed: {version}").format(version=normalized)
            )
        return EasyRPGRuntime(normalized, managed)

    def latest(self) -> EasyRPGRuntime:
        """Return the most recent installed EasyRPG Player x64 runtime."""
        runtimes = self.list()
        if not runtimes:
            raise RuntimeError(
                _(
                    "no EasyRPG Player runtime is installed; run "
                    "'box-rpg runtime easyrpg available --interactive'"
                )
            )
        return runtimes[0]

    def remove(self, version: str) -> None:
        """Delete one launcher-owned EasyRPG Player runtime."""
        self.remove_managed(self.get(version))

    def remove_managed(self, runtime: EasyRPGRuntime) -> None:
        """Delete an enumerated runtime after revalidating its exact managed path."""
        version = normalize_version(runtime.version)
        easyrpg_paths = cast(_EasyRPGPaths, self._paths)
        expected = easyrpg_paths.easyrpg_runtimes_root / version
        if runtime.root.absolute() != expected.absolute():
            raise ConfigurationError(
                _("refusing to manage unexpected EasyRPG runtime path: {path}").format(
                    path=runtime.root
                )
            )
        managed = easyrpg_paths.ensure_managed_easyrpg_runtime_path(expected)
        if managed.is_symlink() or not managed.is_dir():
            raise ConfigurationError(
                _("managed EasyRPG runtime directory is missing or unsafe: {path}").format(
                    path=expected
                )
            )
        descriptor = easyrpg_paths.open_managed_cache_directory("runtimes", "easyrpg")
        locks = ExitStack()
        try:
            locks.enter_context(cache_lock(descriptor, managed.name))
            entry = os.stat(managed.name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode):
                raise ConfigurationError(
                    _("managed EasyRPG runtime directory is missing or unsafe: {path}").format(
                        path=managed
                    )
                )
            shutil.rmtree(managed.name, dir_fd=descriptor)
        finally:
            locks.close()
            os.close(descriptor)


class EasyRPGDownloadCatalog:
    """Inspect and remove only recognized EasyRPG Player archives."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths

    def list(self) -> tuple[Path, ...]:
        """Return direct regular EasyRPG archives and partial downloads."""
        self._paths.ensure()
        paths = cast(_EasyRPGPaths, self._paths)
        archives: list[Path] = []
        for archive in paths.easyrpg_downloads_root.iterdir():
            if _is_archive(archive):
                try:
                    archives.append(paths.ensure_managed_easyrpg_download_path(archive))
                except ConfigurationError:
                    continue
        return tuple(sorted(archives))

    def owns(self, archive: Path) -> bool:
        """Return whether a path is directly inside the EasyRPG downloads directory."""
        return archive.parent == cast(_EasyRPGPaths, self._paths).easyrpg_downloads_root.resolve(
            strict=True
        )

    def remove(self, archive: Path) -> None:
        """Delete one revalidated EasyRPG archive through a pinned directory descriptor."""
        paths = cast(_EasyRPGPaths, self._paths)
        managed = paths.ensure_managed_easyrpg_download_path(archive)
        if not _is_archive(managed):
            raise RuntimeError(
                _("refusing unsafe EasyRPG download archive: {archive}").format(archive=archive)
            )
        descriptor = paths.open_managed_cache_directory("downloads", "easyrpg")
        locks = ExitStack()
        try:
            locks.enter_context(cache_lock(descriptor, managed.name))
            entry = os.stat(managed.name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(entry.st_mode):
                raise RuntimeError(
                    _("refusing unsafe EasyRPG download archive: {archive}").format(archive=archive)
                )
            os.unlink(managed.name, dir_fd=descriptor)
        finally:
            locks.close()
            os.close(descriptor)


def install_runtime(paths: AppPaths, version: str) -> EasyRPGRuntime:
    """Download and atomically install one official EasyRPG Player x64 runtime."""
    if current_architecture() != "x64":
        raise RuntimeError(_("EasyRPG Player managed downloads currently support x64 only"))
    normalized = normalize_version(version)
    paths.ensure()
    target = paths.easyrpg_runtimes_root / normalized
    paths.ensure_managed_easyrpg_runtime_path(target)
    runtime_descriptor = paths.open_managed_cache_directory("runtimes", "easyrpg")
    download_descriptor = paths.open_managed_cache_directory("downloads", "easyrpg")
    locks = ExitStack()
    try:
        locks.enter_context(cache_lock(runtime_descriptor, normalized))
        locks.enter_context(cache_lock(download_descriptor, _archive_name(normalized)))
        try:
            os.stat(normalized, dir_fd=runtime_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return EasyRPGCatalog(paths).get(normalized)
        archive_name = _archive_name(normalized)
        download_archive_at(
            download_url(normalized),
            archive_name,
            download_descriptor,
            allowed_hosts=OFFICIAL_DOWNLOAD_HOSTS,
        )
        archive = Path(f"/proc/self/fd/{download_descriptor}") / archive_name
        with tempfile.TemporaryDirectory(
            prefix=".install-", dir=f"/proc/self/fd/{runtime_descriptor}"
        ) as temporary_name:
            extracted = extract_runtime(archive, Path(temporary_name))
            if _executable(extracted) is None:
                raise RuntimeError(
                    _("EasyRPG archive does not contain an executable easyrpg-player")
                )
            os.replace(extracted, normalized, dst_dir_fd=runtime_descriptor)
        return EasyRPGCatalog(paths).get(normalized)
    finally:
        locks.close()
        os.close(download_descriptor)
        os.close(runtime_descriptor)


def _archive_name(version: str) -> str:
    """Return the fixed x64 Linux archive filename for a validated version."""
    return f"easyrpg-player-{version}-linux.tar.gz"


def _is_archive(path: Path) -> bool:
    """Return whether a path is a direct regular EasyRPG archive, never a symlink."""
    return (
        _ARCHIVE_PATTERN.fullmatch(path.name) is not None
        and not path.is_symlink()
        and path.is_file()
    )


def _validate_page(page: int) -> None:
    """Reject invalid client-side page numbers."""
    if page < 1:
        raise RuntimeError(_("EasyRPG Player version page must be at least 1"))


def _version_key(version: str) -> tuple[int, int, int, int]:
    """Return a four-component numeric key for deterministic release ordering."""
    components = [int(component) for component in version.split(".")]
    padded = [*components, 0, 0, 0, 0]
    return padded[0], padded[1], padded[2], padded[3]


def _managed_runtime(directory: Path) -> EasyRPGRuntime | None:
    """Build a runtime model only for a direct canonical directory."""
    if directory.is_symlink() or not directory.is_dir():
        return None
    try:
        version = normalize_version(directory.name)
    except RuntimeError:
        return None
    if directory.name != version:
        return None
    return EasyRPGRuntime(version, directory)


def executable(runtime: EasyRPGRuntime) -> Path:
    """Return the validated EasyRPG Player executable for one installed runtime."""
    player = _executable(runtime.root)
    if player is None:
        raise RuntimeError(
            _("EasyRPG Player runtime is not installed: {version}").format(version=runtime.version)
        )
    return player


def _executable(root: Path) -> Path | None:
    """Return a direct executable player file without following symlinks."""
    player = root / "easyrpg-player"
    if player.is_symlink() or not player.is_file() or not os.access(player, os.X_OK):
        return None
    return player


def extract_runtime(archive_path: Path, destination: Path) -> Path:
    """Safely extract a player archive with either a root directory or root files."""
    try:
        descriptor = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as archive_file:
            validate_private_file(archive_file.fileno())
            extract_bounded(archive_file, destination, _prepare_player_layout)
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise RuntimeError(
            _("cannot stage EasyRPG Player archive {archive}: {error}").format(
                archive=archive_path, error=exc
            )
        ) from exc
    return next(destination.iterdir())


def _prepare_player_layout(destination: Path) -> None:
    """Normalize the layout inside disposable staging, before publication."""
    entries = tuple(destination.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        validate_runtime_links(entries[0])
        return
    staged = destination / "runtime"
    staged.mkdir(mode=0o700)
    for entry in entries:
        os.replace(entry, staged / entry.name)
    validate_runtime_links(staged)


class _VersionIndexParser(HTMLParser):
    """Collect version directory links from the EasyRPG Player index."""

    def __init__(self) -> None:
        super().__init__()
        self.directories: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Collect the final component of directory links only."""
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href is None:
            return
        path = urlsplit(href).path
        if not path.endswith("/"):
            return
        directory = path.removesuffix("/").rsplit("/", maxsplit=1)[-1]
        if directory:
            self.directories.append(directory)
