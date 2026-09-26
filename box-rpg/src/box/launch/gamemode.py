"""GameMode wrapper detection for sandboxed launches."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from box.errors import LaunchError
from box.utils.i18n import _

GAMEMODERUN = Path("/usr/bin/gamemoderun")
XDG_DBUS_PROXY = Path("/usr/bin/xdg-dbus-proxy")
BUSCTL = Path("/usr/bin/busctl")
GAMEMODE_PROXY_SOCKET_NAME = "gamemode-proxy"
GAMEMODE_BUS_NAME = "com.feralinteractive.GameMode"
GAMEMODE_OBJECT_PATH = "/com/feralinteractive/GameMode"
GAMEMODE_INTERFACE = "com.feralinteractive.GameMode"
HOST_DBUS_TIMEOUT = 5.0


def _is_gamemoderun_available() -> bool:
    """Report whether the in-sandbox GameMode wrapper is usable."""
    if GAMEMODERUN.is_file() and os.access(GAMEMODERUN, os.X_OK):
        return True
    return shutil.which("gamemoderun") is not None


def is_proxy_available() -> bool:
    """Report whether the host D-Bus proxy binary is usable."""
    if XDG_DBUS_PROXY.is_file() and os.access(XDG_DBUS_PROXY, os.X_OK):
        return True
    return shutil.which("xdg-dbus-proxy") is not None


def is_available() -> bool:
    """Report whether the GameMode wrapper and its proxy are both usable."""
    return _is_gamemoderun_available() and is_proxy_available()


def require_gamemode() -> str:
    """Return the in-sandbox wrapper path or fail closed when unavailable."""
    if not _is_gamemoderun_available():
        raise LaunchError(
            _(
                "GameMode requires gamemoderun (/usr/bin/gamemoderun); "
                "install GameMode or retry without --gamemode"
            )
        )
    if not is_proxy_available():
        raise LaunchError(
            _(
                "GameMode requires xdg-dbus-proxy (/usr/bin/xdg-dbus-proxy); "
                "install xdg-dbus-proxy or retry without --gamemode"
            )
        )
    return str(GAMEMODERUN)


def resolve_session_bus_address(env: Mapping[str, str]) -> str:
    """Resolve the host session bus address without touching the process environment."""
    address = env.get("DBUS_SESSION_BUS_ADDRESS", "")
    if address:
        return address
    runtime_dir = env.get("XDG_RUNTIME_DIR", "")
    if runtime_dir:
        return f"unix:path={runtime_dir}/bus"
    raise LaunchError(
        _(
            "GameMode requires a session bus address ($DBUS_SESSION_BUS_ADDRESS "
            "or $XDG_RUNTIME_DIR/bus); retry without --gamemode"
        )
    )


def proxy_argv(address: str, socket_path: Path | str) -> list[str]:
    """Build the filtered proxy command for exactly the GameMode bus name."""
    return [
        str(XDG_DBUS_PROXY),
        address,
        str(socket_path),
        "--filter",
        f"--talk={GAMEMODE_BUS_NAME}",
    ]


def is_bus_client_available() -> bool:
    """Report whether the host D-Bus client binary is usable."""
    # Fixed absolute path only, like the BWRAP guard: no PATH lookup, so a
    # hostile PATH cannot redirect the host registration call.
    return BUSCTL.is_file() and os.access(BUSCTL, os.X_OK)


def require_bus_client() -> str:
    """Return the host bus client path or fail closed when unavailable."""
    if not is_bus_client_available():
        raise LaunchError(
            _(
                "GameMode requires busctl (/usr/bin/busctl); "
                "install systemd or retry without --gamemode"
            )
        )
    return str(BUSCTL)


def _validate_host_pid(pid: int) -> int:
    """Reject non-positive PIDs; bool is rejected via the exact type check."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("GameMode host PID must be a positive integer")
    return pid


def register_argv(pid: int) -> list[str]:
    """Build the host RegisterGame busctl command for one host PID."""
    _validate_host_pid(pid)
    return [
        str(BUSCTL),
        "--user",
        "call",
        GAMEMODE_BUS_NAME,
        GAMEMODE_OBJECT_PATH,
        GAMEMODE_INTERFACE,
        "RegisterGame",
        "i",
        str(pid),
    ]


def unregister_argv(pid: int) -> list[str]:
    """Build the host UnregisterGame busctl command for one host PID."""
    _validate_host_pid(pid)
    return [
        str(BUSCTL),
        "--user",
        "call",
        GAMEMODE_BUS_NAME,
        GAMEMODE_OBJECT_PATH,
        GAMEMODE_INTERFACE,
        "UnregisterGame",
        "i",
        str(pid),
    ]


def register_host_game(pid: int, *, timeout: float = HOST_DBUS_TIMEOUT) -> None:
    """Register one host PID with gamemoded; fail closed on any error.

    Runs busctl with no shell, closed fds, DEVNULL stdin/stderr, and
    captured stdout inside a short bounded timeout; gamemoded is
    D-Bus-activatable so startup never blocks beyond that timeout.
    Any failure raises LaunchError.
    """
    _validate_host_pid(pid)
    require_bus_client()
    try:
        completed = subprocess.run(
            register_argv(pid),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise LaunchError(
            _("cannot register game with GameMode; retry without --gamemode")
        ) from exc
    except subprocess.SubprocessError as exc:
        raise LaunchError(
            _("cannot register game with GameMode; retry without --gamemode")
        ) from exc
    if completed.returncode != 0:
        raise LaunchError(_("cannot register game with GameMode; retry without --gamemode"))
    # gamemoded always answers RegisterGame with D-Bus success carrying an
    # int32 status (i 0 registered, i -1 accepted-not-registered, i -2
    # rejected), so busctl exits 0 even when unboosted. Fail closed on an
    # explicit non-zero status; missing output (old mocks, older busctl)
    # stays backward compatible and counts as success.
    output = completed.stdout or ""
    match = re.search(r"\bi\s+(-?\d+)", output)
    if match is not None and int(match.group(1)) != 0:
        raise LaunchError(_("cannot register game with GameMode; retry without --gamemode"))


def unregister_host_game(pid: int, *, timeout: float = HOST_DBUS_TIMEOUT) -> bool:
    """Best-effort host UnregisterGame; never raises, True on success."""
    try:
        _validate_host_pid(pid)
    except ValueError:
        return False
    if not is_bus_client_available():
        return False
    try:
        completed = subprocess.run(
            unregister_argv(pid),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return False
    return completed.returncode == 0
