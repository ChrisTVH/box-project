"""Shared AppImage release-notes generator.

Builds the GitHub/GitLab release notes for the box-rpg-maker AppImage:
a static description of the frontend-only artifact plus the most recent
commit titles from git history.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MAX_COMMITS = 10


def _find_repo_root(start: str | Path) -> Path:
    """Walk up from ``start`` until a directory containing ``.git`` is found."""
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError(
        f"no .git directory found at or above {current}; "
        "pass the monorepo root (or a path inside it) as repo_root"
    )


def _recent_commits(repo_root: str | Path, limit: int = MAX_COMMITS) -> list[str]:
    """Return up to ``limit`` recent ``git log`` oneline entries."""
    root = _find_repo_root(repo_root)
    try:
        proc = subprocess.run(
            ["git", "log", "--format=%h %s", "-n", str(limit)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot run git in {root}: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed in {root}: {proc.stderr.strip()}")
    lines = [line.rstrip() for line in proc.stdout.splitlines() if line.strip()]
    return lines[:limit]


def generate_notes(repo_root: str | Path, tag: str) -> str:
    """Build the release notes for ``tag`` from the static text plus git log."""
    commits = _recent_commits(repo_root)
    static = (
        f"Standalone AppImage of the box-gui frontend (GTK4 + libadwaita)"
        f" — box-rpg-maker {tag}.\n"
        "\n"
        "Install it either way:\n"
        "\n"
        "a) Clone the repo and install the backend first:\n"
        "   ./install.py --install --target cli"
        " (use gui or all for the other targets).\n"
        "\n"
        "b) Or just run the AppImage: it detects a missing or stale box-rpg backend\n"
        "   and guides its setup itself (exact version match required).\n"
        "\n"
        "System requirements (needed on the host; the AppImage does not provide them):\n"
        "\n"
        "- Python 3.14+\n"
        "- GTK4 / libadwaita 1.5+\n"
        "- libfuse3"
    )
    header = f"## What's changed in {tag}"
    if not commits:
        return f"{static}\n\n{header}"
    numbered = "\n".join(f"{index}. {entry}" for index, entry in enumerate(commits, start=1))
    return f"{static}\n\n{header}\n\n{numbered}"


def main(argv: list[str]) -> int:
    """Print the release notes for the tag in ``argv``."""
    if len(argv) < 2:
        print(f"Usage: {argv[0]} <tag> [repo-root]", file=sys.stderr)
        return 2
    tag = argv[1]
    repo: str | Path = argv[2] if len(argv) > 2 else "."
    print(generate_notes(repo, tag))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
