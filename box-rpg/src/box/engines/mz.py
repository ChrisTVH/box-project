"""RPG Maker MZ export detection."""

from __future__ import annotations

from pathlib import Path

from box.engines.base import is_game_artifact
from box.models import EngineName, GameInfo


class RpgMakerMzAdapter:
    """Detect standard RPG Maker MZ web exports."""

    name = EngineName.RPG_MAKER_MZ

    def detect(self, root: Path) -> GameInfo | None:
        """Identify an MZ game by its root entrypoint, data and plugin registry."""
        entrypoint = root / "index.html"
        plugins = root / "js" / "plugins.js"
        core = root / "js" / "rmmz_core.js"
        manifest = root / "package.json"
        if all(
            (
                is_game_artifact(root, entrypoint),
                is_game_artifact(root, plugins),
                is_game_artifact(root, core),
                is_game_artifact(root, manifest),
                is_game_artifact(root, root / "data", directory=True),
            )
        ):
            return GameInfo(self.name, root, entrypoint, manifest)
        return None
