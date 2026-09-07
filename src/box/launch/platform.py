"""Display-backend selection for NW.js launches."""

from __future__ import annotations

import os
from collections.abc import Mapping


def ozone_platform(environ: Mapping[str, str] | None = None) -> str:
    """Select Wayland only for a session that exposes a Wayland display socket."""
    values = os.environ if environ is None else environ
    if values.get("XDG_SESSION_TYPE", "").lower() == "wayland" and values.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11"
