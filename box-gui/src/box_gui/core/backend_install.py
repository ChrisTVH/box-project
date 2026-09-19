"""Backend clone + install flow driven from the setup page.

Clones the monorepo at the embedded tag (never HEAD) into a temporary
directory, then runs its ``install.py --install --yes --target cli`` with
the same interpreter running the GUI, streaming every install.py
stdout/stderr line to the caller. Tool output is surfaced verbatim without
reinterpretation; only the flow control lives here (clone fallback, a single
--break-system-packages retry on PEP 668 refusal, exit-code and version
verification).

Nothing here touches Gtk/Adw or the network beyond the git clone the user
explicitly started with the Install button.
"""

from __future__ import annotations

import os
import re
import shutil
import site
import stat
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
    "is_externally_managed_failure",
    "query_installed_version",
    "resolve_expected_commit",
    "resolve_tag_commit",
    "run_command_streaming",
    "user_site_problems",
    "verify_clone",
]

CLONE_URLS: tuple[str, str] = (
    "https://gitlab.com/christvh/box-project",
    "https://github.com/ChrisTVH/box-project",
)
INSTALL_TARGET = "cli"

_BACKEND_DISTRIBUTION = "box-rpg"
_INSTALL_SCRIPT_NAME = "install.py"
_COMMAND_TIMEOUT_S = 3600
_VERIFY_TIMEOUT_S = 60
_LS_REMOTE_TIMEOUT_S = 15.0
_EXTERNALLY_MANAGED_MARKER = "externally-managed-environment"
_BAD_SIGNATURE_RE = re.compile(r"\b(BAD|EXP|REV)\b")
# git rev-parse must yield a full object id (SHA-1 or SHA-256) before any
# pin comparison or return; anything else fails closed.
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
# Lightweight (non-annotated) tags carry no tag object: verify-tag reports
# them as "not a tag" style errors and they must fail closed, never take the
# unsigned-annotated transitional path.
_NON_ANNOTATED_MARKERS = (
    "NOT A TAG",
    "NON-TAG",
    "NOT ANNOTATED",
    "NOT AN ANNOTATED TAG",
    "LIGHTWEIGHT",
)
# Transitional unsigned annotated tags carry a tag object without a
# signature yet; only these note and continue.
_UNSIGNED_TAG_MARKERS = (
    "NO SIGNATURE",
    "CANNOT VERIFY",
    "UNSIGNED",
    "NOT SIGNED",
)


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


def build_install_command(
    python: str, install_script: Path, *, break_system_packages: bool = False
) -> list[str]:
    """Build the backend-owned installer invocation for the CLI target."""
    command = [python, str(install_script), "--install", "--yes", "--target", INSTALL_TARGET]
    if break_system_packages:
        command.append("--break-system-packages")
    return command


def is_externally_managed_failure(lines: Sequence[str]) -> bool:
    """Return True when pip refused with a PEP 668 external-management error.

    Matches only pip's stable machine-readable token, never the localized
    human-readable explanation around it.
    """
    return any(_EXTERNALLY_MANAGED_MARKER in line.lower() for line in lines)


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


def user_site_problems(home: Path | None = None, user_site: Path | None = None) -> tuple[str, ...]:
    """List user-site ancestors violating the installer ownership rule.

    Mirrors the read-only half of install.py's user-site bootstrap guard:
    every existing directory from the home directory down to the user
    site-packages must be owned by the current user with no group or
    other write permission, and the site must live inside the home
    directory. Missing directories are fine (pip creates them). Returns
    the offending paths, empty when the install can proceed.
    """
    base = home.resolve() if home is not None else Path.home().resolve()
    site_dir = (
        user_site.resolve() if user_site is not None else Path(site.getusersitepackages()).resolve()
    )
    if (
        not base.is_absolute()
        or not site_dir.is_absolute()
        or site_dir == base
        or base not in site_dir.parents
    ):
        return (str(site_dir),)
    problems: list[str] = []
    current = Path("/")
    for component in site_dir.parts[1:]:
        current = current / component
        if not current.is_relative_to(base):
            continue
        try:
            entry = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError:
            problems.append(str(current))
            break
        if not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
            problems.append(str(current))
            break
        if entry.st_uid != os.geteuid() or entry.st_mode & 0o022:
            problems.append(str(current))
    return tuple(problems)


def resolve_tag_commit(url: str, tag: str, *, timeout: float = _LS_REMOTE_TIMEOUT_S) -> str | None:
    """Resolve one tag to its commit via ``git ls-remote <url> <tag>``.

    Runs git without a shell and prefers the peeled ``^{}`` line for
    annotated tags, falling back to the plain tag line. Returns the hex
    commit (40 or 64 chars) or None when the tag is absent, the output is
    malformed, or git exits non-zero. Transport errors (OSError,
    SubprocessError) propagate for the caller to treat as fallback.
    """
    proc = subprocess.run(
        ["git", "ls-remote", url, tag, f"refs/tags/{tag}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        return None
    peeled: str | None = None
    plain: str | None = None
    wanted = (f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        commit = fields[0].strip()
        ref = fields[1].strip()
        if ref not in wanted:
            continue
        if _COMMIT_RE.fullmatch(commit) is None:
            continue
        if ref.endswith("^{}"):
            peeled = commit.lower()
        else:
            plain = commit.lower()
    return peeled or plain


def resolve_expected_commit(
    tag: str,
    clone_urls: Sequence[str] = CLONE_URLS,
    *,
    timeout: float = _LS_REMOTE_TIMEOUT_S,
) -> str | None:
    """Resolve a tag across clone remotes, returning the first hex commit.

    Tries each URL in order with :func:`resolve_tag_commit` and returns
    the first valid commit, or None when no remote yields one. Transport
    failures on one remote fall through to the next; all-failed resolves
    as None so the caller can install without a pin rather than guessing.
    """
    for url in clone_urls:
        try:
            commit = resolve_tag_commit(url, tag, timeout=timeout)
        except OSError, subprocess.SubprocessError:
            continue
        if commit is not None:
            return commit
    return None


def verify_clone(
    repo_dir: Path,
    tag: str,
    expected_commit: str | None,
    *,
    on_status: Callable[[str], None] | None = None,
) -> str:
    """Verify a fresh clone resolves to the expected commit with a usable tag.

    Resolves ``tag`` with ``git rev-parse <tag>^{commit}`` (no shell, with
    timeout), validates the output as hex (40 or 64 chars), and fails
    closed when ``expected_commit`` is pinned and the tag moved or the
    mirror drifted. Then best-effort ``git verify-tag``: success passes
    silently, an unsigned annotated transitional tag records a status note
    and continues, while a lightweight (non-annotated) tag, BAD/EXP/REV
    key errors, or any other verification failure fail closed. Returns
    the resolved commit hash.
    """
    resolved_lines: list[str] = []
    try:
        rev_code = run_command_streaming(
            ["git", "rev-parse", f"{tag}^{{commit}}"],
            resolved_lines.append,
            cwd=repo_dir,
            timeout=_VERIFY_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendInstallError(
            _("Could not resolve tag {tag} in the cloned repository: {error}").format(
                tag=tag, error=str(exc) or exc.__class__.__name__
            )
        ) from exc
    resolved = next((line.strip() for line in resolved_lines if line.strip()), "")
    if rev_code != 0 or not resolved:
        detail = "\n".join(resolved_lines).strip() or f"git rev-parse exited with code {rev_code}"
        raise BackendInstallError(
            _("Could not resolve tag {tag} in the cloned repository: {detail}").format(
                tag=tag, detail=detail
            )
        )
    # Validate the hash before any pin comparison or return: only full
    # SHA-1 (40) or SHA-256 (64) hex counts as a resolved commit.
    if _COMMIT_RE.fullmatch(resolved) is None:
        raise BackendInstallError(
            _("Could not resolve tag {tag} in the cloned repository: invalid commit hash.").format(
                tag=tag
            )
        )
    resolved = resolved.lower()
    if expected_commit is not None and resolved != expected_commit.strip().lower():
        raise BackendInstallError(
            _(
                "Cloned tag {tag} resolved to {resolved} but expected {expected}; refusing to install (tag moved or mirror drift)."
            ).format(tag=tag, resolved=resolved, expected=expected_commit.strip())
        )
    verify_lines: list[str] = []
    try:
        verify_code = run_command_streaming(
            ["git", "verify-tag", tag],
            verify_lines.append,
            cwd=repo_dir,
            timeout=_VERIFY_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendInstallError(
            _("Could not verify tag {tag}: {error}").format(
                tag=tag, error=str(exc) or exc.__class__.__name__
            )
        ) from exc
    if verify_code == 0:
        return resolved
    detail = "\n".join(verify_lines).strip() or f"git verify-tag exited with code {verify_code}"
    upper_detail = detail.upper()
    is_bad_signature = (
        _BAD_SIGNATURE_RE.search(upper_detail) is not None
        or "EXPIRED" in upper_detail
        or "REVOKED" in upper_detail
        or "INVALID SIGNATURE" in upper_detail
    )
    if is_bad_signature:
        raise BackendInstallError(
            _("Tag signature verification failed for {tag}: {detail}").format(
                tag=tag, detail=detail
            )
        )
    # Lightweight (non-annotated) tags have no tag object to verify: fail
    # closed instead of taking the unsigned-annotated transitional path.
    if any(marker in upper_detail for marker in _NON_ANNOTATED_MARKERS):
        raise BackendInstallError(
            _("Tag {tag} is not an annotated tag; refusing to install: {detail}").format(
                tag=tag, detail=detail
            )
        )
    if any(marker in upper_detail for marker in _UNSIGNED_TAG_MARKERS):
        # Unsigned transitional tags carry no signature yet; note it and continue.
        if on_status is not None:
            on_status(
                _("Tag {tag} is not signed; continuing without signature verification.").format(
                    tag=tag
                )
            )
        return resolved
    raise BackendInstallError(
        _("Could not verify tag {tag}: {detail}").format(tag=tag, detail=detail)
    )


def install_backend(
    tag: str,
    *,
    python: str = sys.executable,
    on_line: Callable[[str], None],
    on_status: Callable[[str], None] | None = None,
    clone_urls: Sequence[str] = CLONE_URLS,
    expected_commit: str | None = None,
) -> InstallOutcome:
    """Clone the repo at tag, install the CLI backend, and verify it.

    Clone progress goes to ``on_status`` so ``on_line`` carries EXCLUSIVELY
    live install.py output lines. Every failure raises BackendInstallError
    with the verbatim tool text; success returns only after the installed
    version is confirmed to equal the tag. When ``expected_commit`` is given,
    the clone is verified against that pin before install.py runs.
    """
    notify: Callable[[str], None] = on_status if on_status is not None else (lambda _m: None)
    site_problems = user_site_problems()
    if site_problems:
        raise BackendInstallError(
            _(
                "Cannot install to your Python user site: {paths}. Each directory must be owned by you with no group or other write permission (for example: chown USERNAME PATH and chmod go-w PATH); adjust them and retry."
            ).format(paths=", ".join(site_problems))
        )
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
        # Verify the clone before trusting it: pin check first, then tag signature.
        verify_clone(repository, tag, expected_commit, on_status=notify)
        notify(_("Installing box-rpg {tag} …").format(tag=tag))
        install_lines: list[str] = []

        def _stream(line: str) -> None:
            install_lines.append(line)
            on_line(line)

        code = run_command_streaming(build_install_command(python, script), _stream)
        if code != 0 and is_externally_managed_failure(install_lines):
            # Externally managed Pythons (PEP 668) need the separate
            # --break-system-packages consent install.py requires; retry
            # exactly once with it, streaming the second attempt too.
            notify(_("Retrying with --break-system-packages …"))
            install_lines.clear()
            code = run_command_streaming(
                build_install_command(python, script, break_system_packages=True), _stream
            )
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
