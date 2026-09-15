"""RPG Maker MV export detection."""

from __future__ import annotations

from pathlib import Path

from box.engines.base import is_game_artifact
from box.models import EngineName, GameInfo


class RpgMakerMvAdapter:
    """Detect standard RPG Maker MV web exports."""

    name = EngineName.RPG_MAKER_MV

    def detect(self, root: Path) -> GameInfo | None:
        """Identify an MV game by its web root and plugin registry."""
        entrypoint = root / "www" / "index.html"
        plugins = root / "www" / "js" / "plugins.js"
        manifest = root / "package.json"
        if all(
            (
                is_game_artifact(root, entrypoint),
                is_game_artifact(root, plugins),
                is_game_artifact(root, manifest),
            )
        ):
            return GameInfo(self.name, root, entrypoint, manifest)
        return None
