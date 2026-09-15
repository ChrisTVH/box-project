"""RPG Maker 2000/2003 project detection."""

from __future__ import annotations

from pathlib import Path

from box.engines.base import is_game_artifact
from box.models import EngineName, GameInfo


class RpgMaker2000_2003Adapter:
    """Detect RPG_RT projects supported by EasyRPG Player."""

    name = EngineName.RPG_MAKER_2000_2003

    def detect(self, root: Path) -> GameInfo | None:
        """Identify a project from the required RPG_RT data files."""
        if all(
            is_game_artifact(root, root / filename)
            for filename in ("RPG_RT.ini", "RPG_RT.ldb", "RPG_RT.lmt")
        ):
            return GameInfo(self.name, root)
        return None
