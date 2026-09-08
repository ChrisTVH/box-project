"""Formatting of local diagnostic reports."""

from __future__ import annotations

import json

from box.diagnostics.environment import Environment
from box.diagnostics.versions import VersionReport


def render_report(environment: Environment, versions: VersionReport) -> str:
    """Render a portable JSON report without game content or file listings."""
    versions_payload: dict[str, str | None] = {
        "engine": versions.engine,
        "engine_version": versions.engine_version,
        "nwjs": versions.nwjs,
    }
    if versions.easyrpg_player is not None:
        versions_payload["easyrpg_player"] = versions.easyrpg_player
    payload = {
        "environment": {
            "system": environment.system,
            "release": environment.release,
            "machine": environment.machine,
        },
        "versions": versions_payload,
    }
    return json.dumps(payload, indent=2) + "\n"
