"""Safe game-directory resolution and engine detection."""

from __future__ import annotations

from pathlib import Path

from box.engines.registry import EngineRegistry
from box.errors import GameValidationError
from box.models import GameInfo
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
        game = registry.detect(root)
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
    current = Path(root.anchor)
    for component in root.parts[1:]:
        current /= component
        if current.is_symlink():
            raise GameValidationError(
                _("configured game root contains a symlink: {path}").format(path=current)
            )
    return root.resolve(strict=True)
