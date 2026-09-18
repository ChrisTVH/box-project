"""Box RPG Maker frontend package."""

from __future__ import annotations

try:
    from box_gui._version import __version__  # pyright: ignore[reportMissingImports]
except ImportError:
    try:
        # Lazy import so installed layouts without tools/ still import.
        from tools.versioning import compute_version  # pyright: ignore[reportMissingImports]

        # Resolve from the package location (monorepo root) so box-gui
        # reports the same number as box-rpg whatever the cwd is.
        __version__ = compute_version(__file__)
    except Exception:
        __version__ = "0.0.dev0"

__all__ = ["__version__"]
