"""XDG path resolution and managed-directory safety checks."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
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
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink():
                raise ConfigurationError(f"managed directory must not be a symlink: {directory}")

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

    def ensure_managed_session_path(self, path: Path) -> Path:
        """Validate a session path is owned by the launcher cache."""
        return self._ensure_managed_child(self.sessions_root, path, "session")

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
            if current.exists() and current.is_symlink():
                raise ConfigurationError(f"managed {label} path contains a symlink: {current}")
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
