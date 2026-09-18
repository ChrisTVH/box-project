"""Backend gate: exact backend version check plus dependency probes.

The startup gate compares the installed ``box-rpg`` version against the
expected one (embedded AppImage tag, else the aligned frontend version)
with an EXACT match: anything else routes to the setup page instead of
guessing. Dependency probes duplicate the tiny ``install.py`` checks with
stdlib/subprocess only, so this module stays inside the stable surface
(``box.api``/``box.models``/``box.errors``/``box.paths``/``box.config.models``
plus blessed extras) and never imports the backend-owned root installer
script, ``box.launch.sandbox``, or ``box.cli``.

Nothing here touches Gtk/Adw: probing runs on a worker thread and the page
marshals results back to the main loop.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Literal

from box_gui.core.app_info import get_expected_backend_version
from box_gui.i18n import _

__all__ = [
    "BackendState",
    "BackendStatus",
    "DependencyStatus",
    "check_backend",
    "probe_dependencies",
]

BackendState = Literal["compatible", "missing", "mismatch", "error"]

_BACKEND_DISTRIBUTION = "box-rpg"
_BWRAP = Path("/usr/bin/bwrap")
_PROBE_TIMEOUT_S = 15


@dataclass(frozen=True)
class BackendStatus:
    """Outcome of the startup gate check."""

    state: BackendState
    expected_version: str
    installed_version: str | None
    message: str

    @property
    def needs_setup(self) -> bool:
        """Return True unless the installed backend matches exactly."""
        return self.state != "compatible"


@dataclass(frozen=True)
class DependencyStatus:
    """One setup-page dependency row: availability plus its requirement."""

    key: str
    label: str
    available: bool
    required: bool
    detail: str


def _import_box() -> None:
    """Import the backend package, raising when it is missing or broken."""
    importlib.import_module("box")


def _installed_box_version() -> str | None:
    """Return the installed box-rpg distribution version, if determinable."""
    try:
        return importlib.metadata.version(_BACKEND_DISTRIBUTION)
    except PackageNotFoundError:
        return None


def check_backend(expected_version: str | None = None) -> BackendStatus:
    """Check the installed backend against the expected version exactly.

    Missing or unimportable backends report ``missing``; any other
    detection failure (permissions, broken environment) reports ``error``
    with the raw message so the UI fails closed instead of guessing.
    """
    expected = expected_version or get_expected_backend_version()
    try:
        _import_box()
    except ImportError:
        return BackendStatus(
            state="missing",
            expected_version=expected,
            installed_version=None,
            message=_("The box-rpg backend is not installed. Install it to open your library."),
        )
    except Exception as exc:
        return BackendStatus(
            state="error",
            expected_version=expected,
            installed_version=None,
            message=str(exc) or exc.__class__.__name__,
        )
    try:
        installed = _installed_box_version()
    except Exception as exc:
        return BackendStatus(
            state="error",
            expected_version=expected,
            installed_version=None,
            message=str(exc) or exc.__class__.__name__,
        )
    if installed is None:
        return BackendStatus(
            state="missing",
            expected_version=expected,
            installed_version=None,
            message=_(
                "The box-rpg backend is installed but its version cannot be determined. "
                "Reinstall it to open your library."
            ),
        )
    if installed == expected:
        return BackendStatus(
            state="compatible",
            expected_version=expected,
            installed_version=installed,
            message=_("box-rpg {version} is ready.").format(version=installed),
        )
    return BackendStatus(
        state="mismatch",
        expected_version=expected,
        installed_version=installed,
        message=_(
            "Installed box-rpg {installed} does not match the expected version {expected}. "
            "Install the expected version to open your library."
        ).format(installed=installed, expected=expected),
    )


def _tool_runs(command: list[str]) -> bool:
    """Return whether a helper tool executes successfully."""
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_S,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return result.returncode == 0


def _probe_python() -> DependencyStatus:
    """Report the interpreter version; always true as a startup precondition."""
    info = sys.version_info
    return DependencyStatus(
        key="python",
        label=_("Python 3.14+"),
        available=info >= (3, 14),
        required=True,
        detail=f"{info.major}.{info.minor}.{info.micro}",
    )


def _probe_pip() -> DependencyStatus:
    """Report whether the running interpreter can invoke pip for the install."""
    available = _tool_runs([sys.executable, "-I", "-m", "pip", "--version"])
    return DependencyStatus(
        key="pip",
        label="pip",
        available=available,
        required=True,
        detail=_("available") if available else _("missing"),
    )


def _bwrap_problem() -> str | None:
    """Mirror install.bwrap_problem: None when Bubblewrap can launch games."""
    if not (_BWRAP.is_file() and os.access(_BWRAP, os.X_OK)):
        return "missing"
    if not _tool_runs([str(_BWRAP), "--version"]):
        return "broken"
    if not _tool_runs(
        [
            str(_BWRAP),
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--",
            "/usr/bin/true",
        ]
    ):
        return "userns"
    return None


def _probe_bwrap() -> DependencyStatus:
    """Report Bubblewrap sandbox support through the install-equivalent probe."""
    problem = _bwrap_problem()
    if problem is None:
        return DependencyStatus(
            key="bwrap",
            label="Bubblewrap",
            available=True,
            required=True,
            detail=_("ready to launch games"),
        )
    details = {
        "missing": _("missing"),
        "broken": _("installed but does not run"),
        "userns": _("user namespaces are blocked"),
    }
    return DependencyStatus(
        key="bwrap",
        label="Bubblewrap",
        available=False,
        required=True,
        detail=details.get(problem, problem),
    )


def _is_tool_usable(path: str, name: str) -> bool:
    """Return True for an executable absolute path or a PATH lookup hit."""
    candidate = Path(path)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return True
    return shutil.which(name) is not None


def _is_gamemode_available_on_host() -> bool:
    """Report GameMode support straight from the host wrapper binaries.

    Mirrors box.launch.gamemode availability (gamemoderun plus its D-Bus
    proxy) for setups where the backend itself is not installed yet, so
    the setup page never reports a present wrapper as missing.
    """
    return _is_tool_usable("/usr/bin/gamemoderun", "gamemoderun") and _is_tool_usable(
        "/usr/bin/xdg-dbus-proxy", "xdg-dbus-proxy"
    )


def _is_gamemode_available() -> bool:
    """Return True when the backend reports a usable GameMode wrapper.

    Any failure (old backend without the probe, missing gamemoderun,
    unexpected errors) means unavailable, never a crash. Without an
    installed backend the host binaries are probed directly instead.
    """
    try:
        from box.api import launch as launch_api
    except ImportError:
        return _is_gamemode_available_on_host()
    probe = getattr(launch_api, "is_gamemode_available", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:
        return False


def _probe_gamemode() -> DependencyStatus:
    """Report optional GameMode support; never required, never a crash."""
    available = _is_gamemode_available()
    return DependencyStatus(
        key="gamemode",
        label="GameMode",
        available=available,
        required=False,
        detail=_("available") if available else _("not available"),
    )


def _probe_icoextract() -> DependencyStatus:
    """Report optional executable icon extraction; never required, never a crash."""
    available = importlib.util.find_spec("icoextract") is not None
    return DependencyStatus(
        key="icoextract",
        label="icoextract",
        available=available,
        required=False,
        detail=_("available") if available else _("not available"),
    )


def probe_dependencies() -> tuple[DependencyStatus, ...]:
    """Probe every setup-page dependency, degrading gracefully per probe.

    Individual probe failures report that dependency as unavailable with
    the raw detail instead of aborting the whole detection.
    """
    probes = (_probe_python, _probe_pip, _probe_bwrap, _probe_gamemode, _probe_icoextract)
    results: list[DependencyStatus] = []
    for probe in probes:
        try:
            results.append(probe())
        except Exception as exc:
            results.append(
                DependencyStatus(
                    key=getattr(probe, "__name__", "unknown"),
                    label=getattr(probe, "__name__", "unknown"),
                    available=False,
                    required=True,
                    detail=str(exc) or exc.__class__.__name__,
                )
            )
    return tuple(results)
