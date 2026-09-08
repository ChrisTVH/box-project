"""TOML configuration reading."""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from typing import Any

from box.config.models import AppConfig
from box.config.validator import decode_config
from box.errors import ConfigurationError
from box.paths import open_directory_without_symlinks
from box.utils.i18n import _


def read_config(path: Path) -> AppConfig:
    """Read a configuration file, returning defaults when it does not exist."""
    try:
        parent = open_directory_without_symlinks(path.parent)
        try:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        finally:
            os.close(parent)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022
            ):
                raise ConfigurationError(
                    _("configuration has unsafe type, ownership or permissions")
                )
            limit = 1024 * 1024
            if metadata.st_size > limit:
                raise ConfigurationError(_("configuration exceeds byte limit"))
            content = source.read(limit + 1)
            if len(content) > limit:
                raise ConfigurationError(_("configuration exceeds byte limit"))
            data: dict[str, Any] = tomllib.loads(content.decode("utf-8"))
    except FileNotFoundError:
        return decode_config({})
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(
            _("cannot read configuration {path}: {error}").format(path=path, error=exc)
        ) from exc
    return decode_config(data)
