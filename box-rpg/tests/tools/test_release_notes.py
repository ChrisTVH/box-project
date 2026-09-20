"""Tests for the shared AppImage release-notes generator."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))

import release_notes

TAG = "26.9.99"
PREV_TAG = "26.9.80"


def _changelog_lines(notes: str) -> list[str]:
    """Return the changelog bullet lines from generated notes."""
    return [line for line in notes.splitlines() if line.startswith("- `")]


def _fake_git(
    monkeypatch: pytest.MonkeyPatch,
    log_stdout: str,
    describe_stdout: str = "",
    describe_code: int = 1,
) -> list[list[str]]:
    """Serve scripted git answers; describe fails by default (no prior tag)."""
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        assert cmd[0] == "git"
        if cmd[1] == "describe":
            assert "--abbrev=0" in cmd
            assert f"--exclude={TAG}" in cmd
            assert cmd[-1] == "HEAD"
            return subprocess.CompletedProcess(
                cmd, describe_code, stdout=describe_stdout, stderr=""
            )
        assert cmd[1] == "log"
        assert "--format=%h %s" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=log_stdout, stderr="")

    monkeypatch.setattr(release_notes.subprocess, "run", fake_run)
    return seen


def test_generate_notes_lists_everything_since_previous_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a prior release, the range covers all of it with no truncation."""
    entries = [f"abcdef{i} commit message number {i}" for i in range(8)]
    seen = _fake_git(
        monkeypatch,
        "\n".join(entries) + "\n",
        describe_stdout=PREV_TAG + "\n",
        describe_code=0,
    )

    notes = release_notes.generate_notes(".", TAG)

    assert any(f"{PREV_TAG}..HEAD" in cmd for cmd in seen)
    assert not any("-n" in cmd for cmd in seen)
    bullets = _changelog_lines(notes)
    assert len(bullets) == 8
    for entry in entries:
        short_hash, _, subject = entry.partition(" ")
        assert f"- `{short_hash}` {subject}" in bullets
    assert "...and more" not in notes


def test_generate_notes_truncates_range_since_previous_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long range since the previous tag caps at ten with an overflow hint."""
    entries = [f"abcdef{i} commit message number {i}" for i in range(12)]
    _fake_git(
        monkeypatch,
        "\n".join(entries) + "\n",
        describe_stdout=PREV_TAG + "\n",
        describe_code=0,
    )

    notes = release_notes.generate_notes(".", TAG)

    bullets = _changelog_lines(notes)
    assert len(bullets) == 10
    for entry in entries[:10]:
        short_hash, _, subject = entry.partition(" ")
        assert f"- `{short_hash}` {subject}" in bullets
    assert "...and more" in notes


def test_generate_notes_ignores_non_release_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nearest tag outside the version scheme falls back to recent commits."""
    entries = ["abc1234 first change", "def5678 second change"]
    _fake_git(
        monkeypatch,
        "\n".join(entries) + "\n",
        describe_stdout="v26.9.80\n",
        describe_code=0,
    )

    notes = release_notes.generate_notes(".", TAG)

    bullets = _changelog_lines(notes)
    assert len(bullets) == 2
    assert "...and more" not in notes


def test_generate_notes_empty_range_keeps_header_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No commits since the previous tag still returns the static header."""
    _fake_git(monkeypatch, "", describe_stdout=PREV_TAG + "\n", describe_code=0)

    notes = release_notes.generate_notes(".", TAG)

    assert f"## What's changed in {TAG}" in notes
    assert notes.endswith(f"## What's changed in {TAG}")
    assert _changelog_lines(notes) == []


def test_generate_notes_truncates_to_ten_commits_without_previous_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no prior release, only the first ten entries become bullets."""
    entries = [f"abcdef{i} commit message number {i}" for i in range(12)]
    seen = _fake_git(monkeypatch, "\n".join(entries) + "\n")

    notes = release_notes.generate_notes(".", TAG)

    assert any("-n" in cmd and cmd[cmd.index("-n") + 1] == "11" for cmd in seen)
    bullets = _changelog_lines(notes)
    assert len(bullets) == 10
    for entry in entries[:10]:
        short_hash, _, subject = entry.partition(" ")
        assert f"- `{short_hash}` {subject}" in bullets
    assert "...and more" in notes


def test_generate_notes_hides_overflow_hint_without_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten or fewer entries list fully with no overflow hint."""
    entries = [f"abc123{i} fix backend bug {i}" for i in range(3)]
    _fake_git(monkeypatch, "\n".join(entries) + "\n")

    notes = release_notes.generate_notes(".", TAG)

    assert len(_changelog_lines(notes)) == 3
    assert "...and more" not in notes


def test_generate_notes_marks_hash_as_code_and_escapes_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bullets carry the `<hash> <message>` shape with hash in code spans."""
    entries = ["abc1230 fix backend bug 0", "def4561 add `quoted` and *starred* bits"]
    _fake_git(monkeypatch, "\n".join(entries) + "\n")

    notes = release_notes.generate_notes(".", TAG)

    bullets = _changelog_lines(notes)
    assert len(bullets) == len(entries)
    assert bullets[0] == "- `abc1230` fix backend bug 0"
    assert re.match(r"^- `[0-9a-f]{7,}` .+", bullets[0])
    assert bullets[1] == r"- `def4561` add \`quoted\` and \*starred\* bits"


def test_generate_notes_contains_static_and_dynamic_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The static description and the changelog header are both present."""
    entries = ["abc1234 first change", "def5678 second change"]
    _fake_git(monkeypatch, "\n".join(entries) + "\n")

    notes = release_notes.generate_notes(".", TAG)

    for marker in (
        "GTK4",
        "libadwaita",
        "./install.py --install --target",
        "libfuse3",
        "System requirements",
        "```sh",
    ):
        assert marker in notes
    assert TAG in notes
    assert "What's changed" in notes
    assert "\n\n## What's changed" in notes
    assert "\n1. Clone the repo" in notes
    bullets = _changelog_lines(notes)
    assert bullets[0].startswith("- `")
    assert entries[0].partition(" ")[2] in bullets[0]


def test_generate_notes_empty_log_keeps_header_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty git log still returns the static block plus the header."""
    _fake_git(monkeypatch, "")

    notes = release_notes.generate_notes(".", TAG)

    assert "GTK4" in notes
    assert f"## What's changed in {TAG}" in notes
    assert notes.endswith(f"## What's changed in {TAG}")
    assert _changelog_lines(notes) == []
