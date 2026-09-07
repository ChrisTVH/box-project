"""Formatting of local diagnostic reports."""

from __future__ import annotations

import json

from box.diagnostics.environment import Environment
from box.diagnostics.versions import VersionReport


def render_report(environment: Environment, versions: VersionReport) -> str:
    """Render a portable JSON report without game content or file listings."""
    payload = {
        "environment": {
            "system": environment.system,
            "release": environment.release,
            "machine": environment.machine,
        },
        "versions": {
            "engine": versions.engine,
            "engine_version": versions.engine_version,
            "nwjs": versions.nwjs,
        },
    }
    return json.dumps(payload, indent=2) + "\n"
