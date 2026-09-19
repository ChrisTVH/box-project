"""Unit tests for the toolkit-free update helpers (no GTK dependency)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from box_gui.core import updates
from box_gui.core.updates import (
    UpdatesError,
    asset_urls,
    compare_tags,
    discover_latest_tag,
    is_due,
    should_prompt,
)


def test_off_never_due() -> None:
    """The off interval never prompts, even without a previous check."""
    assert is_due(1_000_000.0, "off", None) is False
    assert is_due(1_000_000.0, "off", 0.0) is False
    assert is_due(1_000_000.0, "off", 1_000_000.0) is False


def test_unknown_interval_behaves_as_off() -> None:
    """Unknown interval names never prompt, like off."""
    assert is_due(1_000_000.0, "bogus", None) is False
    assert is_due(1_000_000.0, "", None) is False
    assert is_due(1_000_000.0, "weekly ", 0.0) is False


def test_missing_last_check_means_due() -> None:
    """A missing last check is due for every enabled interval."""
    now = 1_000_000.0
    for interval in ("12h", "daily", "2d", "3d", "weekly"):
        assert is_due(now, interval, None) is True


def test_due_when_elapsed_meets_threshold() -> None:
    """Due exactly at the boundary, not one second before."""
    now = 1_000_000.0
    assert is_due(now, "daily", now - 86400.0) is True
    assert is_due(now, "daily", now - 86400.0 + 1.0) is False
    assert is_due(now, "weekly", now - 604800.0) is True
    assert is_due(now, "weekly", now - 100.0) is False


def test_future_last_check_means_not_due() -> None:
    """Clock skew (last check in the future) never reports due."""
    now = 1_000_000.0
    assert is_due(now, "daily", now + 100.0) is False
    assert is_due(now, "weekly", now + 0.5) is False


def test_compare_tags_equal() -> None:
    """Identical numeric parts compare equal."""
    assert compare_tags("26.9.43", "26.9.43") == 0


def test_compare_tags_orders_numerically() -> None:
    """Comparison is numeric per part, not lexicographic."""
    assert compare_tags("26.9.3", "26.9.43") == -1
    assert compare_tags("26.9.43", "26.9.3") == 1
    assert compare_tags("26.9.43", "26.10.1") == -1
    assert compare_tags("25.12.99", "26.1.1") == -1
    assert compare_tags("26.10.1", "26.9.99") == 1


@pytest.mark.parametrize(
    "bad_tag",
    ["", "  ", "v26.9.43", "26.9", "26.9.43.1", "a.b.c", "26.9.x", "26-9-43"],
)
def test_compare_tags_rejects_invalid(bad_tag: str) -> None:
    """Tags outside year.month.commit-count raise ValueError."""
    with pytest.raises(ValueError):
        compare_tags(bad_tag, "26.9.43")
    with pytest.raises(ValueError):
        compare_tags("26.9.43", bad_tag)


def test_should_prompt_when_newer_and_not_skipped() -> None:
    """A newer latest tag prompts when it was not skipped."""
    assert should_prompt("26.9.44", "26.9.43", None) is True
    assert should_prompt("26.9.44", "26.9.43", "26.9.40") is True


def test_should_prompt_matrix() -> None:
    """Equal or older latest tags never prompt."""
    assert should_prompt("26.9.43", "26.9.43", None) is False
    assert should_prompt("26.9.42", "26.9.43", None) is False
    assert should_prompt("25.1.1", "26.9.43", None) is False


def test_should_prompt_skipped_wins() -> None:
    """The skipped tag suppresses the prompt even when newer."""
    assert should_prompt("26.9.44", "26.9.43", "26.9.44") is False
    assert should_prompt("26.9.44", None, "26.9.44") is False


def test_should_prompt_missing_current_prompts() -> None:
    """Without a current version any valid latest prompts unless skipped."""
    assert should_prompt("26.9.43", None, None) is True
    with pytest.raises(ValueError):
        should_prompt("not-a-tag", None, None)


def test_asset_urls_are_deterministic() -> None:
    """Asset URLs follow the GitHub release download layout."""
    appimage, checksum = asset_urls("26.9.43")
    assert appimage == (
        "https://github.com/ChrisTVH/box-project/releases/download/26.9.43/box-rpg-maker.appimage"
    )
    assert checksum == f"{appimage}.sha256"


def test_asset_urls_reject_invalid_tag() -> None:
    """Invalid tags raise instead of building a broken URL."""
    with pytest.raises(ValueError):
        asset_urls("not-a-tag")


def test_asset_urls_gitlab_source_still_uses_github() -> None:
    """GitLab discovery falls back to the GitHub release for downloads."""
    github_pair = asset_urls("26.9.43", "github")
    gitlab_pair = asset_urls("26.9.43", "gitlab")
    assert gitlab_pair == github_pair
    assert gitlab_pair[0].startswith("https://github.com/")


def test_asset_urls_reject_unknown_source() -> None:
    """Unknown discovery sources raise instead of guessing a URL layout."""
    with pytest.raises(ValueError):
        asset_urls("26.9.43", "forgejo")


class _FakeCompleted:
    """Minimal stub of CompletedProcess for ls-remote output."""

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


_LS_REMOTE_OUTPUT = (
    "aaa111\trefs/tags/26.8.5\n"
    "bbb222\trefs/tags/26.8.5^{}\n"
    "ccc333\trefs/tags/26.9.43\n"
    "ddd444\trefs/tags/26.9.43^{}\n"
    "eee555\trefs/tags/not-a-version\n"
    "fff666\trefs/heads/main\n"
    "ggg777\trefs/tags/26.9.3\n"
)


def test_discover_prefers_github_max_tag(monkeypatch: Any) -> None:
    """GitHub output is parsed, derefs stripped, max taken numerically."""
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(command)
        assert command[:3] == ["git", "ls-remote", "--tags"]
        assert kwargs.get("timeout") == 15.0
        return _FakeCompleted(_LS_REMOTE_OUTPUT)

    monkeypatch.setattr(updates.subprocess, "run", fake_run)

    assert discover_latest_tag() == ("26.9.43", "github")
    assert len(calls) == 1
    assert "github.com" in calls[0][3]


def test_discover_falls_back_to_gitlab_on_github_error(monkeypatch: Any) -> None:
    """Any GitHub OSError falls back to the GitLab remote."""
    seen: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompleted:
        remote = command[3]
        seen.append(remote)
        if "github.com" in remote:
            raise OSError("network down")
        return _FakeCompleted("abc123\trefs/tags/26.9.1\n")

    monkeypatch.setattr(updates.subprocess, "run", fake_run)

    assert discover_latest_tag() == ("26.9.1", "gitlab")
    assert any("github.com" in remote for remote in seen)
    assert any("gitlab.com" in remote for remote in seen)


def test_discover_falls_back_on_subprocess_failure(monkeypatch: Any) -> None:
    """Subprocess timeouts also trigger the GitLab fallback."""
    seen: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompleted:
        remote = command[3]
        seen.append(remote)
        if "github.com" in remote:
            raise subprocess.TimeoutExpired(command, 15.0)
        return _FakeCompleted("abc123\trefs/tags/26.9.2\n")

    monkeypatch.setattr(updates.subprocess, "run", fake_run)

    assert discover_latest_tag(timeout=15.0) == ("26.9.2", "gitlab")


def test_discover_falls_back_when_github_has_no_tags(monkeypatch: Any) -> None:
    """Empty GitHub output still tries GitLab before giving up."""

    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompleted:
        if "github.com" in command[3]:
            return _FakeCompleted("")
        return _FakeCompleted("abc123\trefs/tags/26.9.7\n")

    monkeypatch.setattr(updates.subprocess, "run", fake_run)

    assert discover_latest_tag() == ("26.9.7", "gitlab")


def test_discover_raises_when_neither_yields_tag(monkeypatch: Any) -> None:
    """No usable tags anywhere raise UpdatesError (a ValueError)."""

    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted("abc123\trefs/heads/main\n")

    monkeypatch.setattr(updates.subprocess, "run", fake_run)

    with pytest.raises(UpdatesError):
        discover_latest_tag()
    with pytest.raises(ValueError):
        discover_latest_tag()


def test_discover_raises_when_both_remotes_fail(monkeypatch: Any) -> None:
    """Transport failures on both remotes raise UpdatesError."""

    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompleted:
        raise OSError("offline")

    monkeypatch.setattr(updates.subprocess, "run", fake_run)

    with pytest.raises(UpdatesError):
        discover_latest_tag()
