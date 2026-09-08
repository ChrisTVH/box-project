"""Generation of session-owned NW.js manifests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

from box.errors import LaunchError
from box.models import GameInfo

_FORWARDED_FIELDS = ("window", "chromium-args", "js-flags")


def write_manifest(
    session_root: Path,
    game: GameInfo,
    *,
    session_descriptor: int | None = None,
    game_descriptor: int | None = None,
) -> Path:
    """Create a wrapper manifest while preserving safe display settings."""
    if game.manifest is None or game.entrypoint is None:
        raise LaunchError("a NW.js launch session requires a web game manifest and entrypoint")
    owns_session_descriptor = session_descriptor is None
    owns_game_descriptor = game_descriptor is None
    try:
        if session_descriptor is None:
            session_descriptor = os.open(session_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if game_descriptor is None:
            game_descriptor = os.open(game.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        source = _read_game_manifest(game, game_descriptor)
        payload: dict[str, object] = {
            "name": _manifest_name(source, game.root),
            "main": f"game/{game.entrypoint.relative_to(game.root).as_posix()}",
        }
        for field in _FORWARDED_FIELDS:
            value = source.get(field)
            if isinstance(value, (str, dict)):
                payload[field] = value
        try:
            descriptor = os.open(
                "package.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=session_descriptor,
            )
        except OSError as exc:
            raise LaunchError(f"cannot write session manifest: {exc}") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as manifest_file:
                manifest_file.write(json.dumps(payload, indent=2) + "\n")
        except OSError as exc:
            raise LaunchError(f"cannot write session manifest: {exc}") from exc
    except OSError as exc:
        raise LaunchError(f"cannot create session manifest: {exc}") from exc
    finally:
        if owns_game_descriptor and game_descriptor is not None:
            os.close(game_descriptor)
        if owns_session_descriptor and session_descriptor is not None:
            os.close(session_descriptor)
    manifest = session_root / "package.json"
    return manifest


def _manifest_name(source: dict[str, object], game_root: Path) -> str:
    """Return an NW.js-compatible application name for the session manifest."""
    name = source.get("name")
    if isinstance(name, str) and name.strip():
        return name
    return game_root.name


def _read_game_manifest(game: GameInfo, game_descriptor: int) -> dict[str, object]:
    assert game.manifest is not None
    try:
        relative = game.manifest.relative_to(game.root)
        if len(relative.parts) != 1:
            raise ValueError("game manifest must be in the game root")
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=game_descriptor)
        with os.fdopen(descriptor, "r", encoding="utf-8") as manifest_file:
            value = json.load(manifest_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise LaunchError(f"cannot read game manifest {game.manifest}: {exc}") from exc
    if not isinstance(value, dict):
        raise LaunchError(f"game manifest is not a JSON object: {game.manifest}")
    return cast(dict[str, object], value)
