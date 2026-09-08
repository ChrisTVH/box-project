"""High-level configuration persistence operations."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from box.config.models import AppConfig
from box.config.reader import read_config
from box.config.writer import write_config
from box.errors import ConfigurationError
from box.paths import AppPaths
from box.utils.i18n import _


class ConfigRepository:
    """Read and update the single user configuration file."""

    def __init__(self, paths: AppPaths) -> None:
        self._path = paths.config_file

    def load(self) -> AppConfig:
        """Return stored configuration or safe defaults."""
        return read_config(self._path)

    def save(self, config: AppConfig) -> None:
        """Persist configuration atomically."""
        write_config(self._path, config)

    def add_allowed_root(self, root: Path) -> AppConfig:
        """Add a resolved game root if it is not already configured."""
        config = self.prune_missing_allowed_roots()
        try:
            resolved = root.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ConfigurationError(
                _("cannot resolve allowed game root {root}: {error}").format(root=root, error=exc)
            ) from exc
        if not resolved.is_dir():
            raise ConfigurationError(
                _("allowed game root is not a directory: {root}").format(root=root)
            )
        roots = config.allowed_game_roots
        updated = (
            config if resolved in roots else replace(config, allowed_game_roots=(*roots, resolved))
        )
        self.save(updated)
        return updated

    def prune_missing_allowed_roots(self) -> AppConfig:
        """Remove configured roots that no longer exist without deleting game data."""
        config = self.load()
        roots: list[Path] = []
        for root in config.allowed_game_roots:
            try:
                root.resolve(strict=True)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ConfigurationError(
                    _("cannot inspect configured game root {root}: {error}").format(
                        root=root, error=exc
                    )
                ) from exc
            roots.append(root)
        updated = replace(config, allowed_game_roots=tuple(roots))
        if updated != config:
            self.save(updated)
        return updated

    def remove_allowed_root(self, root: Path) -> AppConfig:
        """Remove one exact configured game root without accessing the game path."""
        config = self.load()
        updated = replace(
            config,
            allowed_game_roots=tuple(
                configured_root
                for configured_root in config.allowed_game_roots
                if configured_root != root
            ),
        )
        self.save(updated)
        return updated

    def clear_allowed_roots(self) -> AppConfig:
        """Remove all configured game roots without accessing their paths."""
        config = replace(self.load(), allowed_game_roots=())
        self.save(config)
        return config

    def set_preferred_runtime(self, version: str | None) -> AppConfig:
        """Set or clear the preferred runtime version."""
        config = replace(self.load(), preferred_runtime=version)
        self.save(config)
        return config
