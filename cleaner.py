#!/usr/bin/env python3
"""Clean generated artifacts from the repository.

Removes ignored Python bytecode and empty root build/environment directories.
Nonempty build outputs, environments and tool caches require manual review:
their names alone do not prove ownership. By default lists what would be removed;
pass --apply to actually delete (with a confirmation prompt) and --yes to
skip the prompt.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

from install import open_directory, read_regular, remove_matching

REPO_ROOT = Path(__file__).resolve().parent
VENV_NAMES = {".venv", "venv"}
BUILD_DIRS = {"dist", "build"}
PROJECT_MARKERS = {".git", "pyproject.toml", "setup.py", "package.json", "Cargo.toml"}


def _protected(root: Path) -> set[Path]:
    """Fail closed unless Git identifies tracked and nonignored paths."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    )
    return {Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name}


def _eligible(root: Path, relative: Path, protected: set[Path]) -> str | None:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return None
    if any(path.is_relative_to(relative) or relative.is_relative_to(path) for path in protected):
        return None
    # Only root caches and the launcher's source/test trees are in scope.
    if len(relative.parts) > 1 and relative.parts[0] not in {"src", "tests", "__pycache__"}:
        return None
    if any(part in VENV_NAMES | BUILD_DIRS for part in relative.parts[:-1]):
        return None
    for ancestor in relative.parents:
        if ancestor == Path("."):
            continue
        with open_directory(root / ancestor) as descriptor:
            if PROJECT_MARKERS.intersection(os.listdir(descriptor)):
                return None
    with open_directory((root / relative).parent) as parent:
        info = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            if len(relative.parts) != 1:
                return None
            category = "venvs" if relative.name in VENV_NAMES else "build"
            if relative.name not in VENV_NAMES | BUILD_DIRS:
                return None
            with open_directory(root / relative) as directory:
                return None if os.listdir(directory) else category
        if (
            stat.S_ISREG(info.st_mode)
            and relative.suffix == ".pyc"
            and relative.parent.name == "__pycache__"
        ):
            data = read_regular(parent, relative.name)
            if len(data) >= 16 and data[:4] == importlib.util.MAGIC_NUMBER:
                return "caches"
    return None


def _walk(root: Path, relative: Path, protected: set[Path], targets: dict[str, list[Path]]) -> None:
    with open_directory(root / relative) as descriptor:
        names = sorted(os.listdir(descriptor))
        if relative != Path(".") and PROJECT_MARKERS.intersection(names):
            return
        for name in names:
            child = relative / name
            category = _eligible(root, child, protected)
            if category:
                targets[category].append(root / child)
            elif (
                (relative == Path(".") and name in {"src", "tests", "__pycache__"})
                or (
                    relative != Path(".")
                    and name not in VENV_NAMES | BUILD_DIRS
                    and not name.startswith(".")
                )
            ) and stat.S_ISDIR(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode):
                _walk(root, child, protected, targets)


def collect_targets(root: Path) -> dict[str, list[Path]]:
    targets: dict[str, list[Path]] = {"caches": [], "venvs": [], "build": []}
    try:
        _walk(root, Path("."), _protected(root), targets)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"error collecting targets: {exc}", file=sys.stderr)
        return {"caches": [], "venvs": [], "build": []}
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
    root = REPO_ROOT
    for items in targets.values():
        for path in items:
            try:
                relative = path.relative_to(root)
                if _eligible(root, relative, _protected(root)) is None:
                    raise PermissionError(path)
                with open_directory(path.parent) as parent:
                    info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        # Unlike rmtree, rmdir cannot consume newly added foreign files.
                        os.rmdir(path.name, dir_fd=parent)
                    else:
                        expected = read_regular(parent, path.name)
                        if len(expected) < 16 or expected[:4] != importlib.util.MAGIC_NUMBER:
                            raise PermissionError(path)
                        remove_matching(parent, path.name, expected)
                print(f"removed {path}")
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
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
