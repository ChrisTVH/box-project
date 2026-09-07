"""Engine adapter registration and dispatch."""

from __future__ import annotations

from pathlib import Path

from box.engines.base import EngineAdapter
from box.engines.mv import RpgMakerMvAdapter
from box.engines.mz import RpgMakerMzAdapter
from box.models import GameInfo


class EngineRegistry:
    """A deterministic registry of engine adapters."""

    def __init__(self, adapters: tuple[EngineAdapter, ...]) -> None:
        self._adapters = adapters

    def detect(self, root: Path) -> GameInfo | None:
        """Return the first matching engine, or None when unsupported."""
        for adapter in self._adapters:
            if game := adapter.detect(root):
                return game
        return None


def default_registry() -> EngineRegistry:
    """Return adapters supported by this release."""
    return EngineRegistry((RpgMakerMvAdapter(), RpgMakerMzAdapter()))
