"""Safe discovery and deletion of cached NW.js download archives."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from box.errors import ConfigurationError, RuntimeError
from box.paths import AppPaths

_DOWNLOAD_ARCHIVE_NAME = re.compile(
    r"^(?:nwjs(?:-sdk)?|(?:standard|sdk))-v\d+\.\d+\.\d+-linux-(?:arm|arm64|ia32|x64)\.tar\.gz(?:\.part)?$"
)


class DownloadCatalog:
    """Inspect and remove only recognized NW.js archives in the download cache."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths

    def list(self) -> tuple[Path, ...]:
        """Return recognized regular archive files directly in the download cache."""
        self._paths.ensure()
        root = self._paths.downloads_root
        downloads: list[Path] = []
        for path in root.iterdir():
            if not _is_download_archive(path):
                continue
            try:
                downloads.append(self._paths.ensure_managed_download_path(path))
            except ConfigurationError:
                continue
        return tuple(sorted(downloads))

    def remove(self, archive: Path) -> None:
        """Delete one recognized regular archive from the launcher download cache."""
        managed = self._paths.ensure_managed_download_path(archive)
        if not _is_download_archive(managed):
            raise RuntimeError(f"refusing unsafe NW.js download archive: {archive}")
        descriptor = _open_downloads_root(self._paths)
        try:
            entry = os.stat(managed.name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(entry.st_mode):
                raise RuntimeError(f"refusing unsafe NW.js download archive: {archive}")
            os.unlink(managed.name, dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def remove_all(self) -> int:
        """Delete every recognized regular archive and return the number removed."""
        downloads = self.list()
        for archive in downloads:
            self.remove(archive)
        return len(downloads)


def _is_download_archive(path: Path) -> bool:
    """Return whether a path is a recognized regular NW.js archive, never a symlink."""
    return (
        _DOWNLOAD_ARCHIVE_NAME.fullmatch(path.name) is not None
        and not path.is_symlink()
        and path.is_file()
    )


def _open_downloads_root(paths: AppPaths) -> int:
    """Open the managed downloads directory without following a replacement symlink."""
    try:
        return paths.open_managed_cache_directory("downloads", "nwjs")
    except OSError as exc:
        raise ConfigurationError(f"cannot securely open managed downloads: {exc}") from exc
