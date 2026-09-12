"""Render untrusted values without terminal controls; never alter stored data."""

from __future__ import annotations

import unicodedata
from pathlib import Path


def safe_terminal_text(value: object) -> str:
    """Escape C0/C1 controls and Unicode formatting controls for single-line output."""
    return "".join(
        f"\\x{ord(char):02x}"
        if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
        else f"\\u{ord(char):04x}"
        if unicodedata.category(char) in {"Cf", "Zl", "Zp", "Cs"}
        else char
        for char in str(value)
    )


def abbreviate_prompt_path(path: Path, available: int, home: Path) -> str:
    """Shrink a game path through fixed steps until it fits available columns.

    The ladder keeps the full path first, then collapses intermediate
    directories to initials, then drops leading components behind an
    ellipsis, and finally truncates the last component. Only the display
    changes; the stored root always stays absolute.
    """
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
