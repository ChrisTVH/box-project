"""TOML configuration reading."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from box.config.models import AppConfig
from box.config.validator import decode_config
from box.errors import ConfigurationError


def read_config(path: Path) -> AppConfig:
    """Read a configuration file, returning defaults when it does not exist."""
    if not path.exists():
        return decode_config({})
    try:
        with path.open("rb") as source:
            data: dict[str, Any] = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc
    return decode_config(data)
