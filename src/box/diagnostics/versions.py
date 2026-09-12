"""Local game and NW.js version inspection."""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from box.launch.manifest import read_regular_metadata
from box.launch.process import runtime_environment
from box.launch.sandbox import Sandbox
from box.models import GameInfo, RuntimeInfo
from box.runtime.easyrpg import EasyRPGRuntime
from box.runtime.easyrpg import executable as easyrpg_executable
from box.utils.terminal import safe_terminal_text

_CORE_VERSION = re.compile(r"RPGMAKER_VERSION\s*=\s*['\"]([^'\"]+)")
_CORE_LIMIT = 4 * 1024 * 1024
_VERSION_OUTPUT_LIMIT = 64 * 1024
_VERSION_TIMEOUT = 10


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
        return VersionReport(game.engine.value, None, safe_terminal_text(runtime.spec.version))
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
        content = read_regular_metadata(path, _CORE_LIMIT).decode("utf-8", errors="replace")
    except OSError, ValueError:
        return None
    match = _CORE_VERSION.search(content)
    return safe_terminal_text(match.group(1)) if match else None


def _nwjs_version(runtime: RuntimeInfo) -> str:
    return _binary_version(runtime.executable, runtime.spec.version)


def _binary_version(executable: Path, fallback: str) -> str:
    """Execute the runtime intentionally, bounding elapsed time and captured bytes.

    A separate process group allows cleanup of children that retain output pipes.
    This limits diagnostic capture, not the runtime's own memory or capabilities.
    """
    fallback = safe_terminal_text(fallback)
    with Sandbox() as sandbox:
        command = sandbox.command([sandbox.runtime(executable), "--version"])
        return _sandboxed_version(command, sandbox.pass_fds, fallback)


def _sandboxed_version(command: list[str], pass_fds: tuple[int, ...], fallback: str) -> str:
    """Bound sandbox output and lifetime without ever retrying on the host."""
    try:
        process = subprocess.Popen(
            command,
            pass_fds=pass_fds,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=runtime_environment(),
            start_new_session=True,
        )
    except OSError:
        return fallback
    assert process.stdout is not None and process.stderr is not None
    outputs = {process.stdout.fileno(): bytearray(), process.stderr.fileno(): bytearray()}
    total = 0
    deadline = time.monotonic() + _VERSION_TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream.fileno(), selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return fallback
                for key, _events in selector.select(remaining):
                    chunk = os.read(key.fd, min(8192, _VERSION_OUTPUT_LIMIT - total + 1))
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    total += len(chunk)
                    if total > _VERSION_OUTPUT_LIMIT:
                        return fallback
                    outputs[key.fd].extend(chunk)
            process.wait(timeout=max(0, deadline - time.monotonic()))
        stdout, stderr = (
            bytes(output).decode("utf-8", errors="replace").strip() for output in outputs.values()
        )
        if process.returncode != 0:
            return fallback
        return safe_terminal_text(stdout or stderr) or fallback
    except OSError, subprocess.TimeoutExpired:
        return fallback
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.stdout.close()
        process.stderr.close()
        process.wait()
