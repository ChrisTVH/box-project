"""Diagnose command implementation."""

from __future__ import annotations

from pathlib import Path

from box.api.diagnose import diagnose
from box.config.repository import ConfigRepository
from box.diagnostics.report import render_report
from box.paths import AppPaths


def execute(
    paths: AppPaths,
    repository: ConfigRepository,
    game_path: Path,
    version: str | None,
    sdk: bool,
) -> int:
    """Print a local diagnostic report without network transmission."""
    result = diagnose(paths, repository, game_path, version, sdk)
    print(render_report(result.environment, result.versions), end="")
    return 0
