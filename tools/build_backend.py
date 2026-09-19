"""Shared PEP 517 build backend pinning the monorepo dynamic version.

Thin wrapper over ``setuptools.build_meta``: before any build hook runs, the
``year.month.commit-count`` version is computed from the monorepo git history
(``tools/versioning.py``) and pinned into the generated ``_version.py`` files
of both packages (``box-rpg`` and ``box-gui``), so setuptools
``[tool.setuptools.dynamic] version = {attr = ...}`` always resolves.

Phase 2 reuses this backend unchanged for ``box-gui``: both version files are
resolved from the monorepo root, never from the calling package directory, so
stamping from either ``box-rpg/`` or ``box-gui/`` pins the same number.

NOTE on wiring: ``pyproject.toml`` deliberately keeps
``build-backend = "setuptools.build_meta"`` instead of this module. PEP 517
``backend-path`` entries must live *inside* the project source tree -- pip
(and ``build``, via pyproject-hooks ``norm_and_check``) rejects anything
outside with ``ValueError: paths must be inside source tree`` (verified), so
``backend-path = ["../.."]`` can never expose the monorepo-level ``tools``
package to an isolated build. The staged installer sources
(``install.copy_build_source``) and sdists likewise ship no ``tools/``.
Instead, stamp the files explicitly before building::

    python3 tools/build_backend.py   # from the monorepo root

and let the stock setuptools backend read the pinned ``_version.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import setuptools.build_meta as _base
from setuptools.build_meta import *  # noqa: F403 -- re-export every other hook


def _find_monorepo_root() -> Path:
    """Return the directory holding ``.git``, searching above this file then cwd.

    Searching above ``__file__`` first makes the result independent of the
    calling package's working directory (``pip`` may build from ``box-rpg/``
    or ``box-gui/``); the ``cwd`` fallback covers relocated checkouts.
    """
    for start in (__file__, os.getcwd()):
        candidate = Path(start).resolve()
        if candidate.is_file():
            candidate = candidate.parent
        for parent in (candidate, *candidate.parents):
            if (parent / ".git").exists():
                return parent
    raise RuntimeError(
        "cannot locate the monorepo root (no .git above the backend or cwd); "
        "builds require a git checkout"
    )


def _generated_version_files(root: Path) -> tuple[Path, Path]:
    """Return the ``_version.py`` paths pinned on every build, ``box-rpg`` first."""
    return (
        root / "box-rpg" / "src" / "box" / "_version.py",
        root / "box-gui" / "src" / "box_gui" / "_version.py",
    )


def _ensure_version_files() -> str:
    """Compute the monorepo version and pin it into both generated files."""
    try:
        from tools.versioning import compute_version, write_version_file
    except ImportError as exc:
        # The backend also stays importable when `tools/` itself is on
        # sys.path (backend-path pointing straight at it); load the sibling
        # versioning module by path instead.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "tools.versioning", Path(__file__).resolve().parent / "versioning.py"
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load tools/versioning.py next to the build backend") from exc
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        compute_version = module.compute_version
        write_version_file = module.write_version_file
    root = _find_monorepo_root()
    version = compute_version(root)
    for dest in _generated_version_files(root):
        write_version_file(dest, version)
    return version


def get_requires_for_build_wheel(
    config_settings: dict[str, str] | None = None,
) -> list[str]:
    """Pin the version files, then delegate to setuptools."""
    _ensure_version_files()
    return _base.get_requires_for_build_wheel(config_settings)


def get_requires_for_build_sdist(
    config_settings: dict[str, str] | None = None,
) -> list[str]:
    """Pin the version files, then delegate to setuptools."""
    _ensure_version_files()
    return _base.get_requires_for_build_sdist(config_settings)


def get_requires_for_build_editable(
    config_settings: dict[str, str] | None = None,
) -> list[str]:
    """Pin the version files, then delegate to setuptools."""
    _ensure_version_files()
    return _base.get_requires_for_build_editable(config_settings)


def prepare_metadata_for_build_wheel(
    metadata_directory: str,
    config_settings: dict[str, str] | None = None,
) -> str:
    """Pin the version files so the dynamic attr resolves, then delegate."""
    _ensure_version_files()
    return _base.prepare_metadata_for_build_wheel(metadata_directory, config_settings)


def prepare_metadata_for_build_editable(
    metadata_directory: str,
    config_settings: dict[str, str] | None = None,
) -> str:
    """Pin the version files so the dynamic attr resolves, then delegate."""
    _ensure_version_files()
    return _base.prepare_metadata_for_build_editable(metadata_directory, config_settings)


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, str] | None = None,
    metadata_directory: str | None = None,
) -> str:
    """Pin the version files, then delegate to setuptools."""
    _ensure_version_files()
    return _base.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_sdist(
    sdist_directory: str,
    config_settings: dict[str, str] | None = None,
) -> str:
    """Pin the version files, then delegate to setuptools."""
    _ensure_version_files()
    return _base.build_sdist(sdist_directory, config_settings)


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, str] | None = None,
    metadata_directory: str | None = None,
) -> str:
    """Pin the version files, then delegate to setuptools."""
    _ensure_version_files()
    return _base.build_editable(wheel_directory, config_settings, metadata_directory)


def main(argv: list[str]) -> int:
    """Stamp both generated ``_version.py`` files and print the version.

    Run from the monorepo root before building (``python3
    tools/build_backend.py``); ``argv`` is accepted for ``-m`` symmetry with
    ``tools/versioning.py`` and ignored.
    """
    _ = argv
    print(_ensure_version_files())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
