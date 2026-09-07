"""Safe game-directory resolution and engine detection."""

from __future__ import annotations

from pathlib import Path

from box.engines.registry import EngineRegistry
from box.errors import GameValidationError
from box.models import GameInfo


def resolve_game_root(path: Path) -> Path:
    """Resolve an existing directory before any game file is used."""
    try:
        root = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise GameValidationError(f"cannot resolve game path {path}: {exc}") from exc
    if not root.is_dir():
        raise GameValidationError(f"game path is not a directory: {path}")
    return root


def detect_game(path: Path, registry: EngineRegistry) -> GameInfo:
    """Resolve a path and detect one supported game engine."""
    root = resolve_game_root(path)
    game = registry.detect(root)
    if game is None:
        raise GameValidationError("unsupported game: expected an RPG Maker MV or MZ NW.js export")
    return game


def ensure_allowed_root(game: GameInfo, allowed_roots: tuple[Path, ...]) -> None:
    """Require a game to reside in an explicitly configured root."""
    if not allowed_roots:
        raise GameValidationError("no allowed_game_roots configured; add one with config set")
    try:
        resolved_roots = tuple(root.resolve(strict=True) for root in allowed_roots)
    except OSError as exc:
        raise GameValidationError(f"configured game root cannot be resolved: {exc}") from exc
    if not any(game.root.is_relative_to(root) for root in resolved_roots):
        raise GameValidationError(f"game path is outside configured roots: {game.root}")
