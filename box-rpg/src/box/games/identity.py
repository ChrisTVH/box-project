"""Stable local identifiers for game-specific cache directories."""

from __future__ import annotations

import hashlib
from pathlib import Path


def game_id(root: Path) -> str:
    """Return a stable opaque identifier derived from a resolved game path."""
    digest = hashlib.sha256(str(root.resolve(strict=True)).encode("utf-8")).hexdigest()
    return digest[:16]
