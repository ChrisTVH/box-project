"""Session-local symbolic links to validated game directories."""

from __future__ import annotations

from pathlib import Path

from box.errors import LaunchError


def link_game(session_root: Path, game_root: Path) -> Path:
    """Create the only game reference used by a launch session."""
    link = session_root / "game"
    if link.exists() or link.is_symlink():
        raise LaunchError(f"session game link already exists: {link}")
    try:
        link.symlink_to(game_root, target_is_directory=True)
    except OSError as exc:
        raise LaunchError(f"cannot link game into session: {exc}") from exc
    return link
