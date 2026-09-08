"""Validation and decoding for TOML configuration values."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from typing import Any, cast

from box.config.models import AppConfig
from box.errors import ConfigurationError


def decode_config(data: dict[str, Any]) -> AppConfig:
    """Validate parsed TOML data and convert it to an application config."""
    version: object = data.get("schema_version", 1)
    roots: object = data.get("allowed_game_roots", [])
    preferred_runtime: object = data.get("preferred_runtime")
    prefer_sdk: object = data.get("prefer_sdk", False)
    if type(version) is not int or version != 1:
        raise ConfigurationError(f"unsupported configuration schema version: {version!r}")
    if not isinstance(roots, list):
        raise ConfigurationError("allowed_game_roots must be an array of paths")
    root_values = cast(list[object], roots)
    normalized_roots_list: list[Path] = []
    for root in root_values:
        if not isinstance(root, str):
            raise ConfigurationError("allowed_game_roots must be an array of paths")
        candidate = Path(root).expanduser()
        if not candidate.is_absolute():
            raise ConfigurationError("allowed_game_roots must contain absolute paths")
        normalized_roots_list.append(Path(os.path.abspath(candidate)))
    if preferred_runtime is not None and not isinstance(preferred_runtime, str):
        raise ConfigurationError("preferred_runtime must be a string or omitted")
    if not isinstance(prefer_sdk, bool):
        raise ConfigurationError("prefer_sdk must be a boolean")
    normalized_roots = tuple(normalized_roots_list)
    return AppConfig(
        allowed_game_roots=normalized_roots,
        preferred_runtime=preferred_runtime,
        prefer_sdk=prefer_sdk,
        schema_version=version,
    )


def encode_config(config: AppConfig) -> str:
    """Serialize a validated configuration using the small TOML subset we own."""
    roots = ", ".join(_toml_string(str(path)) for path in config.allowed_game_roots)
    preferred = "" if config.preferred_runtime is None else _toml_string(config.preferred_runtime)
    lines = [
        f"schema_version = {config.schema_version}",
        f"allowed_game_roots = [{roots}]",
        f"prefer_sdk = {'true' if config.prefer_sdk else 'false'}",
    ]
    if preferred:
        lines.append(f"preferred_runtime = {preferred}")
    return "\n".join(lines) + "\n"


def _toml_string(value: str) -> str:
    """Encode a plain TOML basic string."""
    escaped: list[str] = []
    for character in value:
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif character == "\b":
            escaped.append("\\b")
        elif character == "\t":
            escaped.append("\\t")
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\f":
            escaped.append("\\f")
        elif character == "\r":
            escaped.append("\\r")
        elif unicodedata.category(character) == "Cc":
            escaped.append(f"\\u{ord(character):04X}")
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'
