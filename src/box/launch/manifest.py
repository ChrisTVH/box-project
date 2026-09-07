"""Generation of session-owned NW.js manifests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from box.errors import LaunchError
from box.models import GameInfo

_FORWARDED_FIELDS = ("window", "chromium-args", "js-flags")


def write_manifest(session_root: Path, game: GameInfo) -> Path:
    """Create a wrapper manifest while preserving safe display settings."""
    source = _read_game_manifest(game.manifest)
    payload: dict[str, object] = {
        "name": source.get("name", game.root.name),
        "main": f"game/{game.entrypoint.relative_to(game.root).as_posix()}",
    }
    for field in _FORWARDED_FIELDS:
        value = source.get(field)
        if isinstance(value, (str, dict)):
            payload[field] = value
    manifest = session_root / "package.json"
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return manifest


def _read_game_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchError(f"cannot read game manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LaunchError(f"game manifest is not a JSON object: {path}")
    return cast(dict[str, object], value)
