"""Persistent NW.js profile management."""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

from box.errors import ConfigurationError, LaunchError
from box.games.identity import game_id
from box.models import GameInfo
from box.paths import AppPaths
from box.utils.i18n import _

_PROFILE_ID = re.compile(r"^[0-9a-f]{16}$")


class ProfileCatalog:
    """Create, inspect, and remove only launcher-owned game profiles."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths

    def create_for_game(self, game: GameInfo, game_root: Path | None = None) -> Path:
        """Return the persistent private profile directory for one game."""
        try:
            identifier = game_id(game.root if game_root is None else game_root)
        except (OSError, RuntimeError) as exc:
            root = game.root if game_root is None else game_root
            raise LaunchError(
                _("cannot identify game root {root}: {error}").format(root=root, error=exc)
            ) from exc
        return self._create(identifier)

    def list(self) -> tuple[Path, ...]:
        """Return valid direct profile directories ordered by opaque identifier."""
        self._paths.ensure()
        profiles: list[Path] = []
        for profile in self._paths.profiles_root.iterdir():
            if not _is_profile_directory(profile):
                continue
            try:
                profiles.append(self._validate(profile))
            except ConfigurationError:
                continue
        return tuple(sorted(profiles))

    def remove(self, profile: Path) -> None:
        """Delete one validated persistent profile directory."""
        managed = self._validate(profile)
        descriptor = _open_profiles_root(self._paths)
        try:
            metadata = os.stat(managed.name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ConfigurationError(
                    _("game profile directory is missing or unsafe: {path}").format(path=managed)
                )
            shutil.rmtree(managed.name, dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def _create(self, identifier: str) -> Path:
        if _PROFILE_ID.fullmatch(identifier) is None:
            raise LaunchError(
                _("invalid game profile identifier: {identifier}").format(identifier=identifier)
            )
        descriptor = self._paths.open_or_create_private_cache_directory("profiles", identifier)
        os.close(descriptor)
        return self._validate(self._paths.profiles_root / identifier)

    def _validate(self, profile: Path) -> Path:
        if _PROFILE_ID.fullmatch(profile.name) is None:
            raise ConfigurationError(
                _("invalid game profile directory: {path}").format(path=profile)
            )
        expected = self._paths.profiles_root / profile.name
        if profile.absolute() != expected.absolute():
            raise ConfigurationError(
                _("refusing to manage unexpected game profile path: {path}").format(path=profile)
            )
        managed = self._paths.ensure_managed_profile_path(expected)
        if not _is_profile_directory(managed):
            raise ConfigurationError(
                _("game profile directory is missing or unsafe: {path}").format(path=expected)
            )
        return managed


def _is_profile_directory(path: Path) -> bool:
    """Return whether a path is a direct non-symlink profile directory."""
    return _PROFILE_ID.fullmatch(path.name) is not None and path.is_dir() and not path.is_symlink()


def _open_profiles_root(paths: AppPaths) -> int:
    """Open the managed profiles directory without following replacement symlinks."""
    try:
        return paths.open_managed_cache_directory("profiles")
    except OSError as exc:
        raise ConfigurationError(
            _("cannot securely open managed profiles: {error}").format(error=exc)
        ) from exc
