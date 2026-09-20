"""Shared AppImage release-notes generator.

Builds the GitHub/GitLab release notes for the box-rpg-maker AppImage:
a static description of the frontend-only artifact plus the most recent
commit titles from git history.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

MAX_COMMITS = 10

_TAG_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_ESCAPABLE = ("\\", "`", "*", "_", "[", "]", "#")


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


def _previous_release_tag(repo_root: str | Path, current_tag: str) -> str | None:
    """Return the nearest release tag before ``current_tag``, if any.

    Looks back from HEAD excluding ``current_tag`` itself, so the changelog
    spans the commits between releases. A missing tag history (first
    release ever, tagless checkout) is not an error: the caller falls back
    to the most recent commits instead.
    """
    root = _find_repo_root(repo_root)
    try:
        proc = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", f"--exclude={current_tag}", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot run git in {root}: {exc}") from exc
    if proc.returncode != 0:
        return None
    tag = proc.stdout.strip()
    return tag if _TAG_PATTERN.fullmatch(tag) else None


def _commits_since(repo_root: str | Path, base_tag: str) -> list[str]:
    """Return every ``git log`` oneline entry in ``base_tag..HEAD``."""
    root = _find_repo_root(repo_root)
    try:
        proc = subprocess.run(
            ["git", "log", "--format=%h %s", f"{base_tag}..HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot run git in {root}: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed in {root}: {proc.stderr.strip()}")
    return [line.rstrip() for line in proc.stdout.splitlines() if line.strip()]


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


def _escape_markdown(text: str) -> str:
    """Escape markdown metacharacters so commit subjects render literally."""
    out: list[str] = []
    for index, char in enumerate(text):
        if char in _ESCAPABLE and (index == 0 or text[index - 1] != "\\"):
            out.append("\\")
        out.append(char)
    return "".join(out)


def _changelog_lines(commits: list[str]) -> list[str]:
    """Format commits as bullets with the short hash in code spans."""
    lines: list[str] = []
    for entry in commits:
        short_hash, _, subject = entry.partition(" ")
        if subject:
            lines.append(f"- `{short_hash}` {_escape_markdown(subject)}")
        else:
            lines.append(f"- `{short_hash}`")
    return lines


def generate_notes(repo_root: str | Path, tag: str) -> str:
    """Build the release notes for ``tag`` from the static text plus git log.

    The changelog spans the commits since the previous release tag, capped
    at the most recent ``MAX_COMMITS`` to avoid flooding the release notes.
    Only when no previous tag exists (first release ever, tagless checkout)
    it falls back to the most recent commits, hinting at truncation.
    """
    static = (
        f"Standalone AppImage of the box-gui frontend (GTK4 + libadwaita)"
        f" — box-rpg-maker {tag}.\n"
        "\n"
        "### Install it either way\n"
        "\n"
        "1. Clone the repo and install the backend first:\n"
        "\n"
        "   ```sh\n"
        "   ./install.py --install --target cli\n"
        "   ```\n"
        "\n"
        "   (use `gui` or `all` for the other targets.)\n"
        "2. Or just run the AppImage: it detects a missing or stale box-rpg backend\n"
        "   and guides its setup itself (exact version match required).\n"
        "\n"
        "### System requirements\n"
        "\n"
        "Needed on the host (the AppImage does not provide them):\n"
        "\n"
        "- Python 3.14+\n"
        "- GTK4 / libadwaita 1.5+\n"
        "- libfuse3"
    )
    header = f"## What's changed in {tag}"
    base_tag = _previous_release_tag(repo_root, tag)
    if base_tag is None:
        # No earlier release to span from: show the most recent commits and
        # say so when the list is truncated (releases rarely land every
        # MAX_COMMITS commits, so fetch one extra entry just to tell).
        recent = _recent_commits(repo_root, MAX_COMMITS + 1)
        shown = recent[:MAX_COMMITS]
        truncated = len(recent) > MAX_COMMITS
    else:
        commits = _commits_since(repo_root, base_tag)
        shown = commits[:MAX_COMMITS]
        truncated = len(commits) > MAX_COMMITS
    if not shown:
        return f"{static}\n\n{header}"
    changelog = "\n".join(_changelog_lines(shown))
    if truncated:
        changelog += f"\n\n_...and more — showing only the {MAX_COMMITS} most recent commits._"
    return f"{static}\n\n{header}\n\n{changelog}"


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
