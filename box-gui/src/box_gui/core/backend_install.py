"""Backend clone + install flow driven from the setup page.

Clones the monorepo at the embedded tag (never HEAD) into a temporary
directory, then runs its ``install.py --install --yes --target cli`` with
the same interpreter running the GUI, streaming every install.py
stdout/stderr line to the caller. Tool output is surfaced verbatim without
reinterpretation; only the flow control (clone fallback, exit-code and
version verification) lives here.

Nothing here touches Gtk/Adw or the network beyond the git clone the user
explicitly started with the Install button.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from box_gui.i18n import _

__all__ = [
    "CLONE_URLS",
    "INSTALL_TARGET",
    "BackendInstallError",
    "InstallOutcome",
    "InstallPhase",
    "build_clone_command",
    "build_install_command",
    "install_backend",
    "install_button_label",
    "query_installed_version",
    "run_command_streaming",
]

CLONE_URLS: tuple[str, str] = (
    "https://gitlab.com/christvh/box-project",
    "https://github.com/ChrisTVH/box-project",
)
INSTALL_TARGET = "cli"

_BACKEND_DISTRIBUTION = "box-rpg"
_INSTALL_SCRIPT_NAME = "install.py"
_COMMAND_TIMEOUT_S = 3600


class BackendInstallError(Exception):
    """Clone, install, or verification failure carrying verbatim tool output."""


class InstallPhase(Enum):
    """Setup-page action button phases; the label follows the phase."""

    CHECKING = "checking"
    READY = "ready"
    INSTALLING = "installing"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True)
class InstallOutcome:
    """Verified backend install: the installed version equals the tag."""

    tag: str
    installed_version: str


def install_button_label(phase: InstallPhase, version: str) -> str:
    """Return the single action button label for one install phase."""
    if phase is InstallPhase.INSTALLING:
        return _("Installing…")
    if phase is InstallPhase.FAILED:
        return _("Retry")
    if phase is InstallPhase.SUCCEEDED:
        return _("Installed")
    return _("Install box-rpg {version}").format(version=version)


def build_clone_command(url: str, tag: str, destination: Path) -> list[str]:
    """Build the shallow tag clone command; never HEAD, never a full clone."""
    return ["git", "clone", "--branch", tag, "--depth", "1", url, str(destination)]


def build_install_command(python: str, install_script: Path) -> list[str]:
    """Build the backend-owned installer invocation for the CLI target."""
    return [python, str(install_script), "--install", "--yes", "--target", INSTALL_TARGET]


def run_command_streaming(
    command: Sequence[str],
    on_line: Callable[[str], None],
    *,
    cwd: Path | None = None,
    timeout: float = _COMMAND_TIMEOUT_S,
) -> int:
    """Run one command, forwarding each stdout/stderr line, returning its code.

    Stderr merges into stdout so the caller sees one verbatim live stream.
    OSError (missing executable, permission errors) propagates for the
    caller to surface verbatim. A daemon watchdog kills the child after
    ``timeout`` seconds and raises TimeoutExpired: reading the pipe to EOF
    alone cannot bound a hung child, while ``wait(timeout=...)`` only
    applies after the stream ends.
    """
    proc = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(cwd) if cwd is not None else None,
    )
    expired = threading.Event()

    def _kill_hung() -> None:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                return
            expired.set()

    timer = threading.Timer(timeout, _kill_hung)
    timer.daemon = True
    timer.start()
    try:
        stdout = proc.stdout
        assert stdout is not None
        with stdout:
            for line in stdout:
                on_line(line.rstrip("\n"))
        code = proc.wait()
    finally:
        timer.cancel()
    if expired.is_set():
        raise subprocess.TimeoutExpired(list(command), timeout)
    return code


def query_installed_version(python: str) -> str | None:
    """Return the installed box-rpg version via a fresh interpreter probe.

    A subprocess (without -I, so user-site installs stay visible) mirrors
    the installed_version concept: the running process may hold a stale
    import, so the confirmation reads distribution metadata anew.
    """
    try:
        result = subprocess.run(
            [
                python,
                "-c",
                "from importlib.metadata import version; print(version('box-rpg'))",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip().split()
    return text[0] if text else None


def install_backend(
    tag: str,
    *,
    python: str = sys.executable,
    on_line: Callable[[str], None],
    on_status: Callable[[str], None] | None = None,
    clone_urls: Sequence[str] = CLONE_URLS,
) -> InstallOutcome:
    """Clone the repo at tag, install the CLI backend, and verify it.

    Clone progress goes to ``on_status`` so ``on_line`` carries EXCLUSIVELY
    live install.py output lines. Every failure raises BackendInstallError
    with the verbatim tool text; success returns only after the installed
    version is confirmed to equal the tag.
    """
    notify: Callable[[str], None] = on_status if on_status is not None else (lambda _m: None)
    with tempfile.TemporaryDirectory(prefix="box-rpg-backend-") as directory:
        repository = Path(directory) / "box-project"
        clone_errors: list[str] = []
        cloned = False
        for url in clone_urls:
            notify(_("Cloning {url} …").format(url=url))
            # Each attempt needs a pristine destination: a partial
            # directory left by a failed clone would make git refuse
            # the fallback remote with "already exists".
            shutil.rmtree(repository, ignore_errors=True)
            captured: list[str] = []
            try:
                code = run_command_streaming(
                    build_clone_command(url, tag, repository), captured.append
                )
            except (OSError, subprocess.SubprocessError) as exc:
                clone_errors.append(f"{url}: {str(exc) or exc.__class__.__name__}")
                continue
            script = repository / _INSTALL_SCRIPT_NAME
            if code == 0 and script.is_file():
                cloned = True
                break
            detail = "\n".join(captured).strip() or f"git clone exited with code {code}"
            clone_errors.append(f"{url}: {detail}")
        if not cloned:
            raise BackendInstallError(
                _("Could not clone the repository at {tag}: {errors}").format(
                    tag=tag, errors=" | ".join(clone_errors)
                )
            )
        script = repository / _INSTALL_SCRIPT_NAME
        notify(_("Installing box-rpg {tag} …").format(tag=tag))
        code = run_command_streaming(build_install_command(python, script), on_line)
        if code != 0:
            raise BackendInstallError(_("Install failed with exit code {code}.").format(code=code))
        installed = query_installed_version(python)
        if installed is None:
            raise BackendInstallError(_("Could not confirm the installed box-rpg version."))
        if installed != tag:
            raise BackendInstallError(
                _(
                    "Installed box-rpg {installed} does not match the expected version {tag}."
                ).format(installed=installed, tag=tag)
            )
        return InstallOutcome(tag=tag, installed_version=installed)
