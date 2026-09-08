"""Render untrusted values without terminal controls; never alter stored data."""

from __future__ import annotations

import unicodedata


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
