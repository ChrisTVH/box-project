"""Construction of NW.js process commands."""

from __future__ import annotations

from pathlib import Path

from box.launch.platform import ozone_platform
from box.models import RuntimeInfo


def build_command(runtime: RuntimeInfo, session_root: Path, profile_root: Path) -> list[str]:
    """Return the command that opens a session directory with NW.js."""
    return [
        str(runtime.executable),
        f"--ozone-platform={ozone_platform()}",
        f"--user-data-dir={profile_root / 'user-data'}",
        f"--disk-cache-dir={profile_root / 'disk-cache'}",
        str(session_root),
    ]
