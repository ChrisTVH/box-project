"""Local game and NW.js version inspection."""

from __future__ import annotations

import os
import re
import selectors
import signal
import stat
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from box.launch.manifest import read_regular_metadata
from box.launch.process import runtime_environment
from box.launch.sandbox import Sandbox
from box.models import GameInfo, RuntimeInfo
from box.paths import open_directory_without_symlinks
from box.runtime.easyrpg import EasyRPGRuntime
from box.runtime.easyrpg import executable as easyrpg_executable
from box.utils.terminal import safe_terminal_text

_CORE_VERSION = re.compile(r"RPGMAKER_VERSION\s*=\s*['\"]([^'\"]+)")
_CORE_LIMIT = 4 * 1024 * 1024
_VERSION_OUTPUT_LIMIT = 64 * 1024
_VERSION_TIMEOUT = 10
_BUNDLED_VERSION = re.compile(rb"process\.versions\['(?:node-webkit|nw)'\]\s*=\s*'([^']{1,64})'")
_BUNDLED_NUMBER = re.compile(r"\d+\.\d+\.\d+")
_BUNDLED_DLL = "nw.dll"
_BUNDLED_SCAN_CHUNK = 1024 * 1024
_BUNDLED_SCAN_OVERLAP = 256
_BUNDLED_SCAN_BUDGET = 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class VersionReport:
    """Versions discoverable without sending game data elsewhere."""

    engine: str
    engine_version: str | None
    nwjs: str | None
    easyrpg_player: str | None = None
    game_nwjs: str | None = None


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


def detect_bundled_nwjs_version(
    game_root: Path,
    source_root: Path | None = None,
    source_exe: Path | None = None,
) -> str | None:
    """Detect the NW.js version shipped with a Windows game export.

    Probes the source ``nw.dll`` first (plain Windows exports keep it
    beside the executable), then the effective tree (EVB profiles restore
    packed virtual files there), and finally the packed or restored
    executable itself. Reads are bounded, chunked, symlink-safe, and never
    executed; anything unreadable yields None.
    """
    candidates: list[Path] = []
    for root in (source_root, game_root):
        if root is not None:
            candidates.append(root / _BUNDLED_DLL)
    if source_exe is not None:
        restored = game_root / source_exe.name
        candidates.append(restored)
        candidates.append(source_exe)
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            version = _scan_file_for_bundled_version(candidate)
        except OSError, ValueError:
            continue
        if version is not None:
            return version
    return None


def _read_core_version(path: Path) -> str | None:
    try:
        content = read_regular_metadata(path, _CORE_LIMIT).decode("utf-8", errors="replace")
    except OSError, ValueError:
        return None
    match = _CORE_VERSION.search(content)
    return safe_terminal_text(match.group(1)) if match else None


def _scan_file_for_bundled_version(path: Path) -> str | None:
    """Search one file for the embedded NW.js version stamp without symlinks."""
    absolute = path.absolute()
    parent = absolute.parent
    name = absolute.name
    if not name or name in {".", ".."}:
        return None
    try:
        directory = open_directory_without_symlinks(parent)
    except OSError:
        return None
    try:
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except OSError:
            return None
        try:
            entry = os.fstat(descriptor)
            if not stat.S_ISREG(entry.st_mode):
                return None
            if entry.st_size <= 0 or entry.st_size > _BUNDLED_SCAN_BUDGET:
                return None
            os.set_blocking(descriptor, True)
            return _scan_descriptor(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _scan_descriptor(descriptor: int) -> str | None:
    """Stream a descriptor in chunks, keeping overlap for split stamps."""
    overlap = b""
    total = 0
    while True:
        try:
            chunk = os.read(descriptor, _BUNDLED_SCAN_CHUNK)
        except OSError:
            return None
        if not chunk:
            return None
        total += len(chunk)
        if total > _BUNDLED_SCAN_BUDGET:
            return None
        window = overlap + chunk
        offset = 0
        while (match := _BUNDLED_VERSION.search(window, offset)) is not None:
            version = _normalize_bundled_version(match.group(1))
            if version is not None:
                return version
            offset = match.end()
        overlap = window[-_BUNDLED_SCAN_OVERLAP:] if len(window) > _BUNDLED_SCAN_OVERLAP else window


def _normalize_bundled_version(raw: bytes) -> str | None:
    """Normalize an embedded stamp to the v-prefixed managed version form."""
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if _BUNDLED_NUMBER.fullmatch(text) is None:
        return None
    return safe_terminal_text(f"v{text}") or None


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
