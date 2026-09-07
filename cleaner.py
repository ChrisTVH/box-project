#!/usr/bin/env python3
"""Clean generated artifacts from the repository.

Removes caches, virtual environments and build outputs (the patterns
ignored by .gitignore). By default it only lists what would be removed;
pass --apply to actually delete (with a confirmation prompt) and --yes to
skip the prompt.
"""

from __future__ import annotations

import argparse
import fnmatch
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
EXCLUDED = {".git", "cleaner.py"}

CACHE_NAMES = {".pytest_cache", ".ruff_cache", "__pycache__"}
CACHE_FILES = {"*.pyc"}
VENV_NAMES = {".venv", "venv"}
BUILD_DIRS = {"dist", "build"}
BUILD_PATTERNS = {"*.egg-info"}


def _category(name: str, is_dir: bool) -> str | None:
    """Return the category a path name belongs to, or None."""
    if is_dir:
        if name in CACHE_NAMES:
            return "caches"
        if name in VENV_NAMES:
            return "venvs"
        if name in BUILD_DIRS or any(fnmatch.fnmatch(name, p) for p in BUILD_PATTERNS):
            return "build"
        return None
    if any(fnmatch.fnmatch(name, pattern) for pattern in CACHE_FILES):
        return "caches"
    return None


def _walk(targets: dict[str, list[Path]], root: Path, current: Path) -> None:
    """Recursively collect removable paths, pruning dirs that are targets."""
    for child in sorted(current.iterdir()):
        if child.name in EXCLUDED or child.is_symlink():
            continue
        category = _category(child.name, child.is_dir())
        if category is not None:
            targets[category].append(child)
            continue
        if child.is_dir():
            _walk(targets, root, child)


def collect_targets(root: Path) -> dict[str, list[Path]]:
    targets: dict[str, list[Path]] = {"caches": [], "venvs": [], "build": []}
    _walk(targets, root, root)
    return targets


def print_targets(root: Path, targets: dict[str, list[Path]]) -> None:
    labels = {
        "caches": "Python caches",
        "venvs": "Virtual environments",
        "build": "Build artifacts",
    }
    for category, items in targets.items():
        print(f"{labels[category]}:")
        for item in items:
            print(f"  {item.relative_to(root)}")
        if not items:
            print("  (none)")
        print()


def remove(targets: dict[str, list[Path]]) -> bool:
    ok = True
    root = REPO_ROOT.resolve()
    for items in targets.values():
        for path in items:
            try:
                resolved = path.resolve()
                if path.is_symlink() or resolved == root or not resolved.is_relative_to(root):
                    raise PermissionError(path)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                print(f"removed {path}")
            except OSError as exc:
                ok = False
                print(f"error removing {path}: {exc}", file=sys.stderr)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete (default is a dry run)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (implies --apply)",
    )
    args = parser.parse_args()

    targets = collect_targets(REPO_ROOT)
    total = sum(len(items) for items in targets.values())
    print_targets(REPO_ROOT, targets)

    if not args.apply and not args.yes:
        print(f"{total} item(s) found. Run with --apply to remove them.")
        return 0

    if not args.yes:
        answer = input(f"Remove {total} item(s)? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 0

    ok = remove(targets)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
