"""Toolkit-free update check helpers: intervals, tag comparison, discovery.

Nothing here touches Gtk/Adw: callers run discovery on a worker thread and
marshal results back to the main loop.
"""

from __future__ import annotations

import re
import subprocess

__all__ = [
    "DEFAULT_INTERVAL",
    "INTERVAL_SECONDS",
    "UpdatesError",
    "asset_urls",
    "compare_tags",
    "discover_latest_tag",
    "is_due",
    "should_prompt",
]

INTERVAL_SECONDS: dict[str, int] = {
    "off": 0,
    "12h": 43200,
    "daily": 86400,
    "2d": 172800,
    "3d": 259200,
    "weekly": 604800,
}

DEFAULT_INTERVAL = "weekly"

_GITHUB_REMOTE = "https://github.com/ChrisTVH/box-project"
_GITLAB_REMOTE = "https://gitlab.com/christvh/box-project"

_GITHUB_SOURCE = "github"
_GITLAB_SOURCE = "gitlab"

_TAG_PATTERN = re.compile(r"\d+\.\d+\.\d+")

_APPIMAGE_NAME = "box-rpg-maker.appimage"
_GITHUB_RELEASE_BASE = "https://github.com/ChrisTVH/box-project/releases/download"


class UpdatesError(ValueError):
    """Raised when no release tag can be discovered from any remote."""


def is_due(now: float, interval: str, last_check_at: float | None) -> bool:
    """Return whether an update check is due for the given interval.

    Unknown intervals behave as off (never due). A missing last check
    means due unless checks are off. A last check in the future (clock
    skew) means not due.
    """
    seconds = INTERVAL_SECONDS.get(interval)
    if seconds is None:
        return False
    if seconds <= 0:
        return False
    if last_check_at is None:
        return True
    if last_check_at > now:
        return False
    return (now - last_check_at) >= seconds


def _parse_tag(tag: str) -> tuple[int, int, int]:
    """Parse a year.month.commit-count tag into numeric parts."""
    cleaned = tag.strip()
    if _TAG_PATTERN.fullmatch(cleaned) is None:
        raise ValueError(f"invalid version tag: {tag!r}")
    year, month, count = (int(part) for part in cleaned.split("."))
    return (year, month, count)


def compare_tags(current: str, latest: str) -> int:
    """Compare two version tags numerically, returning -1, 0, or 1.

    Returns -1 when current is older than latest, 0 when equal, and 1
    when current is newer. Invalid tags raise ValueError.
    """
    current_parts = _parse_tag(current)
    latest_parts = _parse_tag(latest)
    if current_parts < latest_parts:
        return -1
    if current_parts > latest_parts:
        return 1
    return 0


def should_prompt(latest: str, current: str | None, skipped: str | None) -> bool:
    """Return whether the UI should prompt for the latest tag.

    True only when latest is newer than current and latest is not the
    skipped tag. A missing current version always counts as older (so
    any valid latest prompts unless skipped). Invalid tags raise
    ValueError through the tag comparison.
    """
    if skipped is not None and latest == skipped:
        return False
    if current is None:
        _parse_tag(latest)
        return True
    return compare_tags(current, latest) < 0


def _max_tag(tags: list[str]) -> str | None:
    """Return the highest tag by numeric parts, or None when empty."""
    if not tags:
        return None
    return max(tags, key=_parse_tag)


def _ls_remote_tags(remote: str, timeout: float) -> str | None:
    """Return the highest valid tag from git ls-remote, or None.

    Dereferenced ``^{}`` lines repeat the same tag, so the suffix is
    stripped before validation. Lines without ``refs/tags/`` or with a
    non-numeric tag are ignored. A non-zero git exit means "no tags"
    so the caller can try the next remote. OSError and subprocess
    errors propagate for the caller to handle as a fallback trigger.
    """
    proc = subprocess.run(
        ["git", "ls-remote", "--tags", remote],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        return None
    candidates: list[str] = []
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        ref = fields[1].strip()
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/") :].removesuffix("^{}")
        if _TAG_PATTERN.fullmatch(tag) is None:
            continue
        candidates.append(tag)
    return _max_tag(candidates)


def discover_latest_tag(*, timeout: float = 15.0) -> tuple[str, str]:
    """Discover the latest release tag, trying GitHub first then GitLab.

    Returns a ``(tag, source)`` pair where source is ``"github"`` or
    ``"gitlab"``. Any OSError or subprocess failure on the GitHub probe
    falls back to GitLab. Raises UpdatesError when neither remote
    yields a usable tag. Runs git without a shell and only depends on
    the standard library (User-Agent does not apply to git).
    """
    github_error: Exception | None = None
    try:
        github_tag = _ls_remote_tags(_GITHUB_REMOTE, timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        github_error = exc
        github_tag = None
    if github_tag is not None:
        return (github_tag, _GITHUB_SOURCE)
    try:
        gitlab_tag = _ls_remote_tags(_GITLAB_REMOTE, timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdatesError(f"cannot discover latest tag: {exc}") from exc
    if gitlab_tag is not None:
        return (gitlab_tag, _GITLAB_SOURCE)
    if github_error is not None:
        raise UpdatesError(
            f"cannot discover latest tag: GitHub probe failed ({github_error}) "
            "and GitLab yielded no tag"
        ) from github_error
    raise UpdatesError("cannot discover latest tag: no release tags found")


def asset_urls(tag: str, source: str = _GITHUB_SOURCE) -> tuple[str, str]:
    """Return the deterministic GitHub asset URLs for a release tag.

    The pair is the AppImage plus its ``.sha256`` checksum under the
    GitHub release download path. The ``source`` is the discovery origin
    from :func:`discover_latest_tag` (``"github"`` or ``"gitlab"``):
    GitLab currently serves discovery fallback only and binary download
    still requires the GitHub release, so both sources resolve to the
    same GitHub URLs by design. GitLab asset API resolution is
    intentionally not implemented. Unknown sources raise ValueError.
    """
    # GitLab releases do not share this deterministic URL layout and have
    # no asset API wired here, so only GitHub URLs are built by design.
    if source not in (_GITHUB_SOURCE, _GITLAB_SOURCE):
        raise ValueError(f"unknown discovery source: {source!r}")
    cleaned = tag.strip()
    _parse_tag(cleaned)
    appimage_url = f"{_GITHUB_RELEASE_BASE}/{cleaned}/{_APPIMAGE_NAME}"
    return (appimage_url, f"{appimage_url}.sha256")
