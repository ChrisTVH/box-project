"""Safe game-directory resolution and engine detection."""

from __future__ import annotations

import os
from pathlib import Path

from box.engines.registry import EngineRegistry
from box.errors import GameValidationError
from box.games.files import (
    DetectedGameInfo,
    directory_identity,
    open_game_directory,
    validate_game_descriptor,
)
from box.models import GameInfo
from box.paths import open_directory_without_symlinks
from box.utils.i18n import _


def resolve_game_root(path: Path) -> Path:
    """Resolve an existing directory before any game file is used."""
    try:
        root = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GameValidationError(
            _("cannot resolve game path {path}: {error}").format(path=path, error=exc)
        ) from exc
    if not root.is_dir():
        raise GameValidationError(_("game path is not a directory: {path}").format(path=path))
    return root


def detect_game(path: Path, registry: EngineRegistry) -> GameInfo:
    """Resolve a path and detect one supported game engine."""
    root = resolve_game_root(path)
    try:
        descriptor = open_directory_without_symlinks(root)
        try:
            game = registry.detect(root)
            if game is not None:
                game = DetectedGameInfo(
                    game.engine,
                    game.root,
                    game.entrypoint,
                    game.manifest,
                    root_identity=directory_identity(descriptor),
                )
                validate_game_descriptor(game, descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise GameValidationError(
            _("cannot inspect game path {path}: {error}").format(path=path, error=exc)
        ) from exc
    if game is None:
        raise GameValidationError(
            _("unsupported game: expected an RPG Maker MV/MZ export or RPG Maker 2000/2003 project")
        )
    return game


def ensure_allowed_root(game: GameInfo, allowed_roots: tuple[Path, ...]) -> None:
    """Require a game to reside in an explicitly configured root."""
    with open_game_directory(game) as descriptor:
        _ensure_allowed_path(game, allowed_roots)
        validate_game_descriptor(game, descriptor)


def _ensure_allowed_path(game: GameInfo, allowed_roots: tuple[Path, ...]) -> None:
    """Validate configured roots before comparing their canonical paths."""
    if not allowed_roots:
        raise GameValidationError(
            _("no {key} configured; add one with config set").format(key="allowed_game_roots")
        )
    try:
        resolved_roots = tuple(_resolve_configured_root(root) for root in allowed_roots)
    except (OSError, RuntimeError) as exc:
        raise GameValidationError(
            _("configured game root cannot be resolved: {error}").format(error=exc)
        ) from exc
    if not any(game.root.is_relative_to(root) for root in resolved_roots):
        raise GameValidationError(
            _("game path is outside configured roots: {path}").format(path=game.root)
        )


def _resolve_configured_root(root: Path) -> Path:
    """Resolve a configured root only when none of its components are symlinks."""
    if not root.is_absolute() or ".." in root.parts:
        raise GameValidationError(_("configured game root must be absolute without traversal"))
    try:
        descriptor = open_directory_without_symlinks(root)
    except OSError as exc:
        raise GameValidationError(
            _("configured game root is missing or contains a symlink: {path}").format(path=root)
        ) from exc
    try:
        if Path(os.readlink(f"/proc/self/fd/{descriptor}")) != root:
            raise GameValidationError(_("configured game root moved during authorization"))
        return root
    finally:
        os.close(descriptor)
