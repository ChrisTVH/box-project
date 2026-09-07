"""Construction of NW.js process commands."""

from __future__ import annotations

from pathlib import Path

from box.models import RuntimeInfo


def build_command(runtime: RuntimeInfo, session_root: Path) -> list[str]:
    """Return the command that opens a session directory with NW.js."""
    return [str(runtime.executable), str(session_root)]
