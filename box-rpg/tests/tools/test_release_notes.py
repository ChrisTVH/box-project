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


def _numbered_lines(notes: str) -> list[str]:
    """Return the numbered changelog lines from generated notes."""
    return [line for line in notes.splitlines() if re.match(r"^\d+\. ", line)]


def test_generate_notes_truncates_to_ten_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the first ten git oneline entries become numbered lines."""
    entries = [f"abcdef{i} commit message number {i}" for i in range(12)]
    stdout = "\n".join(entries) + "\n"

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:2] == ["git", "log"]
        assert "--format=%h %s" in cmd
        assert "-n" in cmd and cmd[cmd.index("-n") + 1] == "10"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(release_notes.subprocess, "run", fake_run)

    notes = release_notes.generate_notes(".", TAG)

    numbered = _numbered_lines(notes)
    assert len(numbered) == 10
    assert numbered[0].startswith("1. ")
    assert numbered[-1].startswith("10. ")
    for index, entry in enumerate(entries[:10], start=1):
        assert numbered[index - 1] == f"{index}. {entry}"


def test_generate_notes_keeps_git_format_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dynamic lines keep the `<hash> <message>` shape without rewriting."""
    entries = [f"abc123{i} fix backend bug {i}" for i in range(3)]
    stdout = "\n".join(entries) + "\n"

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:2] == ["git", "log"]
        assert "--format=%h %s" in cmd
        assert "-n" in cmd and cmd[cmd.index("-n") + 1] == "10"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(release_notes.subprocess, "run", fake_run)

    notes = release_notes.generate_notes(".", TAG)

    numbered = _numbered_lines(notes)
    assert len(numbered) == len(entries)
    for line, entry in zip(numbered, entries, strict=True):
        assert re.match(r"^\d+\. [0-9a-f]{7,} .+", line)
        assert entry in line


def test_generate_notes_contains_static_and_dynamic_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The static description and the changelog header are both present."""
    entries = ["abc1234 first change", "def5678 second change"]
    stdout = "\n".join(entries) + "\n"

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:2] == ["git", "log"]
        assert "--format=%h %s" in cmd
        assert "-n" in cmd and cmd[cmd.index("-n") + 1] == "10"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(release_notes.subprocess, "run", fake_run)

    notes = release_notes.generate_notes(".", TAG)

    for marker in (
        "GTK4",
        "libadwaita",
        "./install.py --install --target",
        "libfuse3",
        "System requirements",
    ):
        assert marker in notes
    assert TAG in notes
    assert "What's changed" in notes
    assert "\n\n## What's changed" in notes
    assert "\n\n1. " in notes
    numbered = _numbered_lines(notes)
    assert numbered[0].startswith("1. ")
    assert entries[0] in numbered[0]


def test_generate_notes_empty_log_keeps_header_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty git log still returns the static block plus the header."""

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:2] == ["git", "log"]
        assert "--format=%h %s" in cmd
        assert "-n" in cmd and cmd[cmd.index("-n") + 1] == "10"
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(release_notes.subprocess, "run", fake_run)

    notes = release_notes.generate_notes(".", TAG)

    assert "GTK4" in notes
    assert f"## What's changed in {TAG}" in notes
    assert notes.endswith(f"## What's changed in {TAG}")
    assert _numbered_lines(notes) == []
