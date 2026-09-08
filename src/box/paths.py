"""XDG path resolution and managed-directory safety checks."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from errno import ELOOP
from pathlib import Path

from box.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class AppPaths:
    """User-owned paths used by box-rpg."""

    config_root: Path
    cache_root: Path

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> AppPaths:
        """Build paths from XDG variables with standard user-directory fallbacks."""
        values = os.environ if environ is None else environ
        home_value = values.get("HOME")
        if not home_value:
            raise ConfigurationError("HOME is required to resolve XDG paths")
        home = Path(home_value).expanduser()
        config_home = Path(values.get("XDG_CONFIG_HOME", home / ".config"))
        cache_home = Path(values.get("XDG_CACHE_HOME", home / ".cache"))
        return cls(config_root=config_home / "box-rpg", cache_root=cache_home / "box-rpg")

    @property
    def config_file(self) -> Path:
        """Return the global TOML configuration path."""
        return self.config_root / "config.toml"

    @property
    def downloads_root(self) -> Path:
        """Return the directory containing downloaded archives."""
        return self.cache_root / "downloads" / "nwjs"

    @property
    def easyrpg_downloads_root(self) -> Path:
        """Return the directory containing downloaded EasyRPG Player archives."""
        return self.cache_root / "downloads" / "easyrpg"

    @property
    def runtimes_root(self) -> Path:
        """Return the root for launcher-owned NW.js runtimes."""
        return self.cache_root / "runtimes" / "nwjs"

    @property
    def easyrpg_runtimes_root(self) -> Path:
        """Return the root for launcher-owned EasyRPG Player runtimes."""
        return self.cache_root / "runtimes" / "easyrpg"

    @property
    def sessions_root(self) -> Path:
        """Return the root for ephemeral game sessions."""
        return self.cache_root / "sessions"

    @property
    def reports_root(self) -> Path:
        """Return the root for local diagnostic reports."""
        return self.cache_root / "reports"

    @property
    def profiles_root(self) -> Path:
        """Return the root for persistent NW.js game profiles."""
        return self.cache_root / "profiles"

    def ensure(self) -> None:
        """Create the required user-owned directories with private permissions."""
        for directory in (
            self.config_root,
            self.cache_root,
            self.cache_root / "downloads",
            self.downloads_root,
            self.easyrpg_downloads_root,
            self.cache_root / "runtimes",
            self.runtimes_root,
            self.easyrpg_runtimes_root,
            self.cache_root / "sessions",
            self.sessions_root,
            self.reports_root,
            self.profiles_root,
        ):
            _ensure_private_directory_without_symlinks(directory)

    def ensure_managed_runtime_path(self, path: Path) -> Path:
        """Validate a runtime path is a lower-case directory owned by the launcher."""
        return self._ensure_managed_child(self.runtimes_root, path, "runtime")

    def ensure_managed_download_path(self, path: Path) -> Path:
        """Validate a direct download-cache file path owned by the launcher."""
        managed = self._ensure_managed_child(self.downloads_root, path, "download")
        downloads_root = self.downloads_root.resolve(strict=True)
        if managed.parent != downloads_root:
            raise ConfigurationError(f"refusing to manage nested download path: {path}")
        return managed

    def ensure_managed_easyrpg_download_path(self, path: Path) -> Path:
        """Validate a direct EasyRPG download-cache file path owned by the launcher."""
        managed = self._ensure_managed_child(self.easyrpg_downloads_root, path, "EasyRPG download")
        if managed.parent != self.easyrpg_downloads_root.resolve(strict=True):
            raise ConfigurationError(f"refusing to manage nested EasyRPG download path: {path}")
        return managed

    def open_managed_cache_directory(self, *components: str) -> int:
        """Open a cache subdirectory from the filesystem root without following symlinks."""
        self.ensure()
        descriptor = _open_directory_without_symlinks(self.cache_root)
        try:
            for component in components:
                if Path(component).name != component or component in {"", ".", ".."}:
                    raise ConfigurationError(f"invalid managed cache component: {component}")
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def open_or_create_private_cache_directory(self, *components: str) -> int:
        """Open or create private cache subdirectories without following symlinks."""
        self.ensure()
        descriptor = _open_directory_without_symlinks(self.cache_root)
        try:
            for component in components:
                if Path(component).name != component or component in {"", ".", ".."}:
                    raise ConfigurationError(f"invalid managed cache component: {component}")
                child = _open_or_create_private_directory(descriptor, component)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def ensure_managed_session_path(self, path: Path) -> Path:
        """Validate a session path is owned by the launcher cache."""
        return self._ensure_managed_child(self.sessions_root, path, "session")

    def ensure_managed_profile_path(self, path: Path) -> Path:
        """Validate one direct persistent game-profile directory."""
        managed = self._ensure_managed_child(self.profiles_root, path, "game profile")
        if managed.parent != self.profiles_root.resolve(strict=True):
            raise ConfigurationError(f"refusing to manage nested game profile path: {path}")
        return managed

    def ensure_managed_easyrpg_runtime_path(self, path: Path) -> Path:
        """Validate an EasyRPG runtime path is owned by the launcher cache."""
        return self._ensure_managed_child(self.easyrpg_runtimes_root, path, "EasyRPG runtime")

    def _ensure_managed_child(self, root: Path, path: Path, label: str) -> Path:
        """Reject traversal and symlink escapes from a launcher-owned root."""
        self.ensure()
        root_absolute = Path(os.path.abspath(root))
        candidate = Path(os.path.abspath(path))
        try:
            relative = candidate.relative_to(root_absolute)
        except ValueError as exc:
            raise ConfigurationError(f"refusing to manage {label} outside cache: {path}") from exc
        if not relative.parts:
            raise ConfigurationError(f"refusing to manage cache root as a {label}: {path}")
        current = root_absolute
        if current.is_symlink():
            raise ConfigurationError(f"managed directory must not be a symlink: {current}")
        for component in relative.parts:
            current /= component
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise ConfigurationError(f"managed {label} path contains a symlink: {current}")
            if stat.S_ISDIR(metadata.st_mode) and (
                metadata.st_uid != os.getuid() or metadata.st_mode & 0o022
            ):
                raise ConfigurationError(
                    f"managed {label} directory has unsafe ownership or permissions: {current}"
                )
        cache = self.cache_root.resolve(strict=True)
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(cache):
            raise ConfigurationError(f"refusing to manage {label} outside cache: {path}")
        if any(component != component.lower() for component in relative.parts):
            raise ConfigurationError(f"managed {label} path contains upper-case components: {path}")
        return resolved


def _open_directory_without_symlinks(path: Path) -> int:
    """Open every absolute directory component through descriptor-relative operations."""
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_or_create_private_directory(parent_descriptor: int, name: str) -> int:
    """Open or create one private directory through a parent descriptor."""
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ConfigurationError(f"managed directory is unsafe: {name}")
    except FileNotFoundError:
        with suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_descriptor
        )
    except OSError as exc:
        raise ConfigurationError(f"cannot safely open managed directory: {name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise ConfigurationError(
                f"managed directory has unsafe ownership or permissions: {name}"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
                raise ConfigurationError(
                    f"managed directory has unsafe ownership or permissions: {name}"
                )
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _ensure_private_directory_without_symlinks(path: Path) -> None:
    """Create a managed directory while rejecting symlinked XDG ancestors."""
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            try:
                metadata = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ConfigurationError(f"managed directory must not be a symlink: {path}")
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid():
            raise ConfigurationError(f"managed directory is not owned by the current user: {path}")
        if metadata.st_mode & 0o022:
            raise ConfigurationError(f"managed directory has unsafe permissions: {path}")
    except OSError as exc:
        if exc.errno == ELOOP:
            raise ConfigurationError(f"managed directory must not be a symlink: {path}") from exc
        raise ConfigurationError(f"cannot safely create managed directory {path}: {exc}") from exc
    finally:
        os.close(descriptor)
