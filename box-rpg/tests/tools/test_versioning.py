"""Tests for the dynamic version computation in tools/versioning.py.

Covers the exact-tag preference (including shallow tag clones), the
fail-closed shallow guard, and the unchanged monthly-count behavior.
All repos are created locally in tmp_path; no network is ever used.
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

# The helper lives in the monorepo tools/ directory, which shares its
# lookup needs with test_appimage.py: prefer the real tools/ directory so
# `import versioning` never resolves to a same-named test helper.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))

import versioning

_COMMIT_YEAR = 2026
_COMMIT_MONTH = 9
_IDENTITY = (
    "-c",
    "user.name=Box Tests",
    "-c",
    "user.email=box-tests@example.invalid",
    "-c",
    "commit.gpgsign=false",
)


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run git offline with a repo-local identity, ignoring ambient config."""
    full_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    full_env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    if env is not None:
        full_env.update(env)
    proc = subprocess.run(
        ["git", *_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=full_env,
    )
    return proc.stdout.strip()


def _commit(repo: Path, name: str, day: int) -> None:
    """Add one file dated to the shared September 2026 fixture month."""
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", name)
    stamp = f"{_COMMIT_YEAR}-{_COMMIT_MONTH:02d}-{day:02d} 12:00:00 +0000"
    dates = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    _git(repo, "commit", "-m", f"add {name}", env=dates)


def _label(count: int) -> str:
    """Build a scheme-valid tag for the fixture month (e.g. ``26.9.86``)."""
    return f"{_COMMIT_YEAR % 100:02d}.{_COMMIT_MONTH}.{count}"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A fresh local git repo on main with a repo-local identity."""
    root = tmp_path / "origin"
    root.mkdir()
    _git(root, "init", "-b", "main")
    return root


def _clone(tmp_path: Path, source: Path, dest_name: str, *extra: str) -> Path:
    """Clone over file:// so --depth/--branch behave like a remote clone."""
    dest = tmp_path / dest_name
    _git(
        tmp_path,
        "-c",
        "protocol.file.allow=always",
        "clone",
        *extra,
        source.as_uri(),
        str(dest),
    )
    return dest


def test_shallow_tag_clone_reports_tag_instead_of_count(git_repo: Path, tmp_path: Path) -> None:
    """Regression: `clone --depth 1 --branch <tag>` reports the tag.

    The shallow checkout only sees one commit, so the blind monthly count
    used to return `<year>.<month>.1` and desync the installer gate.
    """
    for day in range(1, 6):
        _commit(git_repo, f"file{day}.txt", day)
    tag = _label(86)
    _git(git_repo, "tag", tag)
    assert versioning.compute_version(git_repo) == tag

    clone = _clone(tmp_path, git_repo, "shallow", "--depth", "1", "--branch", tag)

    assert _git(clone, "rev-parse", "--is-shallow-repository") == "true"
    assert len(_git(clone, "log", "--oneline").splitlines()) == 1
    assert versioning.compute_version(clone) == tag


def test_exact_tag_wins_over_count_in_full_clone(git_repo: Path, tmp_path: Path) -> None:
    """A full clone checked out at a release tag reports the tag, not the count."""
    for day in range(1, 6):
        _commit(git_repo, f"file{day}.txt", day)
    tag = _label(86)
    _git(git_repo, "tag", tag)

    clone = _clone(tmp_path, git_repo, "full", "--branch", tag)

    assert _git(clone, "rev-parse", "--is-shallow-repository") == "false"
    assert versioning.compute_version(clone) == tag


def test_shallow_clone_without_tag_fails_closed(git_repo: Path, tmp_path: Path) -> None:
    """A shallow clone with no release tag raises instead of guessing a count."""
    for day in range(1, 4):
        _commit(git_repo, f"file{day}.txt", day)

    clone = _clone(tmp_path, git_repo, "shallow", "--depth", "1", "--branch", "main")

    assert _git(clone, "rev-parse", "--is-shallow-repository") == "true"
    with pytest.raises(RuntimeError, match="shallow"):
        versioning.compute_version(clone)


def test_highest_numeric_tag_wins_on_head(git_repo: Path) -> None:
    """Several tags on HEAD resolve to the numeric maximum, not the lexical one."""
    _commit(git_repo, "file.txt", 3)
    _git(git_repo, "tag", _label(10))
    _git(git_repo, "tag", _label(9))
    assert versioning.compute_version(git_repo) == _label(10)


def test_invalid_tags_fall_back_to_month_count(git_repo: Path) -> None:
    """Tags outside the year.month.count scheme never win over the count."""
    for day in range(1, 4):
        _commit(git_repo, f"file{day}.txt", day)
    for bogus in ("v26.9.3", "26.9", "latest", "26.9.3.1"):
        _git(git_repo, "tag", bogus)
    assert versioning.compute_version(git_repo, now=datetime(2026, 9, 17)) == "26.9.3"


def test_month_count_in_regular_repo(git_repo: Path) -> None:
    """Untagged history still reports the monthly commit count."""
    for day in range(1, 5):
        _commit(git_repo, f"file{day}.txt", day)
    assert versioning.compute_version(git_repo, now=datetime(2026, 9, 17)) == "26.9.4"
    assert versioning.compute_version(git_repo, now=datetime(2026, 10, 2)) == "26.10.0"


def test_missing_git_directory_fails_closed(tmp_path: Path) -> None:
    """Outside any checkout the computation fails instead of guessing."""
    with pytest.raises(RuntimeError, match=r"no \.git"):
        versioning.compute_version(tmp_path)
