"""Construction of NW.js process commands."""

from __future__ import annotations

from pathlib import Path

from box.models import RuntimeInfo


def build_command(
    runtime: RuntimeInfo, session_root: Path, profile_root: Path, display: str = "wayland"
) -> list[str]:
    """Return the command that opens a session directory with NW.js.

    Fullscreen comes from the generated session manifest, which NW.js honors;
    Chromium switches do not override the manifest window.
    """
    return [
        str(runtime.executable),
        f"--ozone-platform={display}",
        f"--user-data-dir={profile_root / 'user-data'}",
        f"--disk-cache-dir={profile_root / 'disk-cache'}",
        str(session_root),
    ]
