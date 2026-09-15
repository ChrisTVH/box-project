"""Display-only formatting helpers; never alter stored data."""

from __future__ import annotations

from pathlib import Path

__all__ = ["PATH_DISPLAY_WIDTH", "abbreviate_display_path"]


PATH_DISPLAY_WIDTH = 48


def abbreviate_display_path(
    path: Path,
    available: int = PATH_DISPLAY_WIDTH,
    home: Path | None = None,
) -> str:
    """Shrink a path through fixed steps until it fits the available width.

    Mirrors box.utils.terminal.abbreviate_prompt_path for GTK rows: the
    ladder keeps the full path first, then collapses intermediate
    directories to initials, then drops leading components behind an
    ellipsis, and finally truncates the last component. Only the display
    changes; stored roots always stay absolute. Pass absolute paths;
    relative ones are returned unchanged.
    """
    if home is None:
        home = Path.home()
    if not path.is_absolute():
        return path.as_posix()
    text = path.as_posix()
    head = ""
    rest = text
    for candidate in (home.as_posix(), home.resolve().as_posix()):
        if text == candidate:
            return "~" if available >= 1 else ""
        if text.startswith(candidate + "/"):
            head = "~"
            rest = text[len(candidate) + 1 :]
            break
    else:
        if text.startswith("/"):
            rest = text[1:]
    joiner = "~/" if head else "/"
    parts = rest.split("/") if rest else []
    middles, final = parts[:-1], parts[-1] if parts else ""
    initials = [part[:1] for part in middles]
    ladder = [joiner + "/".join([*middles, final])]
    ladder.append(joiner + "/".join([*initials, final]))
    ladder.append("…" + ladder[1][len(joiner) :])
    for index in range(1, len(initials) + 1):
        ladder.append("…/" + "/".join([*initials[index:], final]))
    keep = available - len("…/")
    if keep < len(final):
        ladder.append("…/" + final[: max(keep, 1)] if available >= len("…/") + 1 else "…")
    previous_length: int | None = None
    for candidate in ladder:
        if previous_length is not None and len(candidate) >= previous_length:
            continue
        previous_length = len(candidate)
        if len(candidate) <= available:
            return candidate
    return ladder[-1]
