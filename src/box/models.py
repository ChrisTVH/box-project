"""Shared immutable domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class EngineName(StrEnum):
    """Supported game engines."""

    RPG_MAKER_MV = "rpg-maker-mv"
    RPG_MAKER_MZ = "rpg-maker-mz"


@dataclass(frozen=True, slots=True)
class GameInfo:
    """Validated information about an RPG Maker game export."""

    engine: EngineName
    root: Path
    entrypoint: Path
    manifest: Path


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    """An NW.js variant identified by version, architecture and flavor."""

    version: str
    architecture: str
    sdk: bool = False

    @property
    def flavor(self) -> str:
        """Return the normalized runtime flavor."""
        return "sdk" if self.sdk else "standard"

    @property
    def directory_name(self) -> str:
        """Return the managed lower-case runtime directory name."""
        return f"{self.flavor}-{self.version.lower()}"


@dataclass(frozen=True, slots=True)
class RuntimeInfo:
    """An installed NW.js runtime."""

    spec: RuntimeSpec
    root: Path
    executable: Path
