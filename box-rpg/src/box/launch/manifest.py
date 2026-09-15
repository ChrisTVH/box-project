"""Generation of session-owned NW.js manifests."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import cast

from box.errors import LaunchError
from box.models import GameInfo
from box.utils.i18n import _

_MANIFEST_LIMIT = 1024 * 1024
_WINDOW_BOOLEANS = frozenset({"fullscreen", "resizable", "frame", "show", "kiosk"})
_WINDOW_DIMENSIONS = frozenset(
    {"width", "height", "min_width", "min_height", "max_width", "max_height"}
)


def _open_directory(path: Path) -> int:
    """Open a directory without following symlinks in any component."""
    path = path.absolute()
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part == "..":
                raise ValueError("directory traversal is not allowed")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_regular_metadata(path: Path, limit: int, *, root_descriptor: int | None = None) -> bytes:
    """Read bounded metadata through no-follow descriptors, rejecting special files.

    With a root descriptor the path must be relative and cannot contain traversal.
    Otherwise every component of the absolute path is opened without symlinks.
    """
    if limit < 0:
        raise ValueError("metadata limit must be nonnegative")
    if root_descriptor is None:
        path = path.absolute()
        parts = path.parts[1:]
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    else:
        if path.is_absolute():
            raise ValueError("metadata path must be relative")
        parts = path.parts
        directory = os.dup(root_descriptor)
    try:
        if not parts or any(part in {".", ".."} for part in parts):
            raise ValueError("invalid metadata path")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("metadata must be a regular file")
            if metadata.st_size > limit:
                raise ValueError("metadata exceeds size limit")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError("metadata exceeds size limit")
            return data
    finally:
        os.close(directory)


def _safe_window(value: object) -> dict[str, object]:
    """Keep typed presentation settings, not file paths or runtime capabilities."""
    if not isinstance(value, dict):
        return {}
    window: dict[str, object] = {}
    for key, setting in cast(dict[str, object], value).items():
        if (
            (key in _WINDOW_BOOLEANS and type(setting) is bool)
            or (key in _WINDOW_DIMENSIONS and type(setting) is int and 0 < setting <= 16384)
            or (key == "title" and isinstance(setting, str))
            or (key == "position" and isinstance(setting, str) and setting in ("center", "mouse"))
        ):
            window[key] = setting
    return window


def write_manifest(
    session_root: Path,
    game: GameInfo,
    *,
    session_descriptor: int | None = None,
    game_descriptor: int | None = None,
) -> Path:
    """Create a wrapper manifest while preserving safe display settings."""
    if game.manifest is None or game.entrypoint is None:
        raise LaunchError(_("a NW.js launch session requires a web game manifest and entrypoint"))
    owns_session_descriptor = session_descriptor is None
    owns_game_descriptor = game_descriptor is None
    try:
        if session_descriptor is None:
            session_descriptor = os.open(session_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if game_descriptor is None:
            game_descriptor = _open_directory(game.root)
        source = _read_game_manifest(game, game_descriptor)
        payload: dict[str, object] = {
            "name": _manifest_name(source, game.root),
            "main": f"game/{game.entrypoint.relative_to(game.root).as_posix()}",
        }
        window = _safe_window(source.get("window"))
        # The game's own presentation settings decide how the window starts;
        # nothing else about its window is trusted.
        payload["window"] = window
        # Game-controlled Chromium/V8 switches can disable security or load code.
        # No switches are forwarded; presentation settings have a typed allowlist.
        try:
            descriptor = os.open(
                "package.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=session_descriptor,
            )
        except OSError as exc:
            raise LaunchError(
                _("cannot write session manifest: {error}").format(error=exc)
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as manifest_file:
                manifest_file.write(json.dumps(payload, indent=2) + "\n")
        except OSError as exc:
            raise LaunchError(
                _("cannot write session manifest: {error}").format(error=exc)
            ) from exc
    except OSError as exc:
        raise LaunchError(_("cannot create session manifest: {error}").format(error=exc)) from exc
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
            raise ValueError(_("game manifest must be in the game root"))
        data = read_regular_metadata(relative, _MANIFEST_LIMIT, root_descriptor=game_descriptor)
        # utf-8-sig tolerates the BOM some editors prepend; it is identical
        # to utf-8 otherwise and byte limits still apply before decoding.
        value = json.loads(data.decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError) as exc:
        raise LaunchError(
            _("cannot read game manifest {manifest}: {error}").format(
                manifest=game.manifest, error=exc
            )
        ) from exc
    if not isinstance(value, dict):
        raise LaunchError(
            _("game manifest is not a JSON object: {manifest}").format(manifest=game.manifest)
        )
    return cast(dict[str, object], value)
