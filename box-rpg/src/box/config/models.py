"""Configuration models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppConfig:
    """User preferences stored in the global configuration file."""

    allowed_game_roots: tuple[Path, ...] = field(default_factory=tuple)
    preferred_runtime: str | None = None
    prefer_sdk: bool = False
    schema_version: int = 1
