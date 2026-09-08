"""Local game and NW.js version inspection."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from box.models import GameInfo, RuntimeInfo
from box.runtime.easyrpg import EasyRPGRuntime
from box.runtime.easyrpg import executable as easyrpg_executable

_CORE_VERSION = re.compile(r"RPGMAKER_VERSION\s*=\s*['\"]([^'\"]+)")


@dataclass(frozen=True, slots=True)
class VersionReport:
    """Versions discoverable without sending game data elsewhere."""

    engine: str
    engine_version: str | None
    nwjs: str | None
    easyrpg_player: str | None = None


def collect_versions(game: GameInfo, runtime: RuntimeInfo) -> VersionReport:
    """Collect local engine and NW.js version information."""
    if game.entrypoint is None:
        return VersionReport(game.engine.value, None, runtime.spec.version)
    core_name = "rpg_core.js" if game.engine.value.endswith("mv") else "rmmz_core.js"
    core_path = game.entrypoint.parent / "js" / core_name
    engine_version = _read_core_version(core_path)
    nwjs = _nwjs_version(runtime)
    return VersionReport(game.engine.value, engine_version, nwjs)


def collect_easyrpg_versions(game: GameInfo, runtime: EasyRPGRuntime) -> VersionReport:
    """Collect the managed EasyRPG Player version without executing game files."""
    player = easyrpg_executable(runtime)
    return VersionReport(game.engine.value, None, None, _binary_version(player, runtime.version))


def _read_core_version(path: Path) -> str | None:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _CORE_VERSION.search(content)
    return match.group(1) if match else None


def _nwjs_version(runtime: RuntimeInfo) -> str:
    return _binary_version(runtime.executable, runtime.spec.version)


def _binary_version(executable: Path, fallback: str) -> str:
    """Read a runtime version without raising when the binary cannot start."""
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError, subprocess.TimeoutExpired:
        return fallback
    output = result.stdout.strip() or result.stderr.strip()
    return output or fallback
