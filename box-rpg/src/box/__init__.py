"""box-rpg package."""

from __future__ import annotations

try:
    # Pinned at build time by tools/build_backend.py; missing on plain checkouts.
    from box._version import __version__  # pyright: ignore[reportMissingImports]
except ImportError:
    try:
        from tools.versioning import compute_version
    except ImportError:
        # No build artifact and no monorepo tooling on sys.path (sdist without
        # tools, relocated sources): stay importable with the dev fallback.
        __version__ = "0.0.dev0"
    else:
        try:
            __version__ = compute_version(__file__)
        except Exception:
            # No git history (exported archive) or git missing: same fallback,
            # never raise on `import box`.
            __version__ = "0.0.dev0"

__all__ = ["__version__"]
