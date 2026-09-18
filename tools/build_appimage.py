#!/usr/bin/env python3
"""Build the box-rpg-maker AppImage from the box-gui checkout (Python pure).

The artifact is ``box-rpg-maker.appimage``: a type 2 AppImage packing ONLY
the ``box-gui`` frontend (GTK4 + libadwaita) with its resources (``res/``,
in-package ``locale/``, desktop entry, icons). The ``box-rpg`` backend is
never bundled; the AppImage runs on the host system Python (>= 3.14) with
no Python interpreter vendored either. A missing or stale backend never
fails the startup gate here: the app's own gate (BackendSetupPage) owns
those cases and guides the install.

Runtime choice: the AppDir is converted with a recent ``appimagetool``
(continuous build) whose embedded type 2 runtime supports FUSE3, so host
systems need ``libfuse3`` and never ``libfuse2``. ``box-rpg`` already needs
``libfuse3`` for ``--ci-mount``, so this adds no new requirement.

Release tagging: the script computes the stable tag itself with the
``year.month.commit-count`` scheme (same count as the version-standard
``get_version.sh``) and creates it (annotated) at HEAD, coordinated with
the ``chore(release): align ...`` commits that pin both distributions to
that version. It then verifies the checkout is exactly at the tag just
created (``git describe --exact-match`` plus tag/HEAD comparison) and
fails closed otherwise, so an AppImage never ships from an arbitrary HEAD.

Build-tag embedding: the tag is baked into the generated ``AppRun`` (as
the ``BOX_RPG_MAKER_APPIMAGE_TAG`` environment export) and written to a
generated ``appimage_tag.txt`` next to the staged ``box_gui`` package, so
the Fase 3 screen can read it without git at runtime. Reader contract:

- Module: ``box_gui.appimage_tag`` (owned by the GUI agent, Fase 3).
- Function: ``def get_appimage_tag() -> str | None``.
- Lookup order: ``BOX_RPG_MAKER_APPIMAGE_TAG`` first, then the sibling
  ``box_gui/appimage_tag.txt`` file. See ``tools/appimage_tag.py`` for the
  tools-side fallback implementing the same lookup without GUI imports.

Payload layout mirrors the checkout (``payload/src/box_gui`` beside
``payload/res``) so ``box_gui`` finds its icons and locale with no GUI
changes: ``app.py`` resolves ``res/icons`` two directories above
``src/box_gui``, and ``i18n.py`` loads ``locale/`` inside the package.

Usage (run from the monorepo root; CI invokes the same entry point, so
no shell scripts are involved)::

    python3 -m tools.build_appimage --check
    python3 -m tools.build_appimage --print-tag
    python3 -m tools.build_appimage --yes
    python3 -m tools.build_appimage --yes --appdir-only --output dist/AppDir
    python3 -m tools.build_appimage --test-build --yes --appdir-only
    python3 -m tools.build_appimage --test-build --tag 26.9.71 --force

Test builds snapshot the dirty tree to a temporary directory, tmp-commit
there, and re-run this module with ``--yes --no-create-tag`` inside the
snapshot, so the real repo only sees read-only git commands and keeps its
HEAD, status, and tags unchanged. The default test artifact is
``tools/target/box-rpg-maker-test.appimage`` (git-ignored); any other
in-repo ``--output`` is refused.

CI triggers are manual only (``workflow_dispatch`` on GitHub Actions,
``when: manual`` on GitLab CI), never on push.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from tools.appimage_tag import (
        APPIMAGE_TAG_ENV_VAR,
        APPIMAGE_TAG_FILENAME,
        is_valid_tag,
    )
except ImportError:  # Running as tools/build_appimage.py puts tools/ on sys.path.
    from appimage_tag import (  # type: ignore[no-redef]
        APPIMAGE_TAG_ENV_VAR,
        APPIMAGE_TAG_FILENAME,
        is_valid_tag,
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
GUI_ROOT = REPO_ROOT / "box-gui"
GUI_SRC = GUI_ROOT / "src" / "box_gui"
GUI_RES = GUI_ROOT / "res"
GUI_INIT_PY = GUI_SRC / "__init__.py"
CLI_INIT_PY = REPO_ROOT / "box-rpg" / "src" / "box" / "__init__.py"

APP_ID = "io.gitlab.christvh.BoxRpgApp"
DESKTOP_FILE_NAME = f"{APP_ID}.desktop"
APP_ICON_NAME = f"{APP_ID}.svg"
ARTIFACT_NAME = "box-rpg-maker.appimage"
PAYLOAD_DIR = Path("usr/share/box-rpg-maker")
SYSTEM_PYTHON = Path("/usr/bin/python3")
APPIMAGETOOL_URL = (
    "https://github.com/AppImage/appimagetool/releases/download/continuous/"
    "appimagetool-x86_64.AppImage"
)
APPIMAGETOOL_MIN_BYTES = 1024 * 1024
TEST_ARTIFACT_NAME = "box-rpg-maker-test.appimage"
TEST_DEFAULT_OUTPUT = REPO_ROOT / "tools" / "target" / TEST_ARTIFACT_NAME
_SNAPSHOT_EXCLUDE_DIRS = frozenset({".venv", "__pycache__", ".pytest_cache", ".ruff_cache"})


def is_linux() -> bool:
    """Return whether the active Python interpreter runs on Linux."""
    return sys.platform.startswith("linux")


def _check_python_version() -> bool:
    return sys.version_info >= (3, 14)


def _read_version(init_py: Path) -> str:
    """Return the __version__ assignment from a package __init__.py file."""
    try:
        content = init_py.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    return match.group(1) if match else "unknown"


def repo_version() -> str:
    """Return the backend version from src/box/__init__.py (cf. install.py)."""
    return _read_version(CLI_INIT_PY)


def gui_repo_version() -> str:
    """Return the frontend version from src/box_gui/__init__.py (cf. install.py)."""
    return _read_version(GUI_INIT_PY)


def _command_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Sanitize the environment for builder subprocesses (cf. install.py).

    Loader injection variables must not reach git/appimagetool children,
    and PATH is pinned because every command uses an absolute path or a
    bare tool name resolved from the system directories.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "PYTHON", "LD_", "DYLD_")) and key != "VIRTUAL_ENV"
    }
    environment["PATH"] = "/usr/bin:/bin"
    environment["PIP_CONFIG_FILE"] = os.devnull
    if extra:
        environment.update(extra)
    return environment


def run(args: list[str], cwd: Path | None = None) -> int:
    """Run a command, streaming output, returning its exit code."""
    proc = subprocess.run(args, check=False, env=_command_environment(), cwd=cwd)
    return proc.returncode


def _git_output(directory: Path, args: list[str]) -> str:
    """Run one git command in directory, returning stripped stdout."""
    try:
        proc = subprocess.run(
            ["git", *args],
            check=False,
            capture_output=True,
            text=True,
            env=_command_environment(),
            cwd=directory,
        )
    except OSError as exc:
        raise RuntimeError(f"git unavailable: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _run_git(args: list[str]) -> str:
    """Run one git command in the real repo, returning stripped stdout."""
    return _git_output(REPO_ROOT, args)


def _run_git_in(directory: Path, args: list[str]) -> str:
    """Run one git command inside the snapshot, returning stripped stdout."""
    return _git_output(directory, args)


def _repo_fingerprint() -> tuple[str, str, tuple[str, ...]]:
    """Capture read-only real-repo state to prove test builds leave it alone."""
    head = _run_git(["rev-parse", "HEAD"])
    status = _run_git(["status", "--porcelain"])
    tags_raw = _run_git(["tag", "-l"])
    tags = tuple(sorted(line.strip() for line in tags_raw.splitlines() if line.strip()))
    return (head, status, tags)


def _resolve_test_output(output: Path | None) -> Path:
    """Resolve the test-build output, refusing in-repo paths except the default."""
    if output is None:
        return TEST_DEFAULT_OUTPUT
    candidate = output if output.is_absolute() else Path.cwd() / output
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate.absolute()
    try:
        default_resolved = TEST_DEFAULT_OUTPUT.resolve()
    except OSError:
        default_resolved = TEST_DEFAULT_OUTPUT.absolute()
    if resolved == default_resolved:
        return default_resolved
    try:
        repo_resolved = REPO_ROOT.resolve()
    except OSError:
        repo_resolved = REPO_ROOT.absolute()
    try:
        resolved.relative_to(repo_resolved)
    except ValueError:
        return resolved
    raise RuntimeError(
        f"refusing in-repo --output for --test-build: {output} (only {TEST_DEFAULT_OUTPUT} allowed)"
    )


def _is_within(path: Path, directory: Path) -> bool:
    """Return True when path sits inside directory, following symlinks."""
    try:
        resolved = directory.resolve()
    except OSError:
        return False
    except ValueError:
        return False
    try:
        path.relative_to(resolved)
    except ValueError:
        return False
    return True


def _snapshot_ignore(directory: str, entries: list[str]) -> set[str]:
    """Exclude caches and build artifacts from the test snapshot."""
    try:
        relative = Path(directory).relative_to(REPO_ROOT)
    except ValueError:
        relative = Path("_outside")
    ignored: set[str] = set()
    for entry in entries:
        if (
            entry in _SNAPSHOT_EXCLUDE_DIRS
            or entry.endswith(".appimage")
            or (relative == Path(".") and entry == "dist")
            or (relative == Path("tools") and entry == "target")
        ):
            ignored.add(entry)
    return ignored


def snapshot_tree(dest: Path) -> Path:
    """Copy the working tree to dest minus caches and build artifacts."""
    if dest.exists():
        raise FileExistsError(f"snapshot destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT, dest, ignore=_snapshot_ignore, symlinks=True)
    return dest


def test_build(
    tag: str | None,
    output: Path | None,
    *,
    appdir_only: bool,
    force: bool,
    appimagetool: Path | None,
    appimagetool_url: str,
    appimagetool_mode: str,
) -> Path:
    """Snapshot the dirty tree and build a throwaway artifact in /tmp.

    The real repo only sees read-only git commands; the snapshot gets the
    ``git add -A`` plus throwaway commit, then re-runs this module with
    ``--yes --no-create-tag`` (resolving ``--tag`` beforehand and forwarding
    it verbatim, so the snapshot tmp-commit never shifts the month count)
    inside it. The real-repo fingerprint must be unchanged afterwards,
    proving no tags or commits leaked out. The output parent is created
    upfront (git-ignored for the default), so an empty directory may remain
    when a later step fails.
    """
    if (REPO_ROOT / ".git").is_file():
        raise RuntimeError("refusing --test-build from a linked worktree (.git is a file)")
    before = _repo_fingerprint()
    resolved_tag = tag or expected_version()
    if not is_valid_tag(resolved_tag):
        raise RuntimeError(f"refusing malformed tag: {resolved_tag!r}")
    resolved_output = _resolve_test_output(output)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="box-rpg-maker-test-") as tmp:
        if _is_within(resolved_output, Path(tmp)):
            raise RuntimeError(
                f"refusing --output inside the snapshot workspace: {resolved_output}"
            )
        snapshot = Path(tmp) / "tree"
        snapshot_tree(snapshot)
        _run_git_in(snapshot, ["add", "-A"])
        _run_git_in(
            snapshot,
            [
                "-c",
                "user.name=box-rpg-maker-test",
                "-c",
                "user.email=box-rpg-maker-test@local",
                "commit",
                "-m",
                "throwaway test build (do not push)",
                "--no-verify",
            ],
        )
        command = [
            sys.executable,
            "-m",
            "tools.build_appimage",
            "--yes",
            "--no-create-tag",
            "--tag",
            resolved_tag,
            "--output",
            str(resolved_output),
        ]
        if appdir_only:
            command.append("--appdir-only")
        if force:
            command.append("--force")
        if appimagetool is not None:
            tool_path = appimagetool if appimagetool.is_absolute() else Path.cwd() / appimagetool
            command.extend(["--appimagetool", str(tool_path)])
        command.extend(["--appimagetool-url", appimagetool_url])
        command.extend(["--appimagetool-mode", appimagetool_mode])
        if run(command, cwd=snapshot) != 0:
            raise RuntimeError(
                f"test build for tag {resolved_tag} failed in snapshot "
                f"(see output above); no artifact at {resolved_output}"
            )
        after = _repo_fingerprint()
        if after != before:
            parts = [
                name
                for name, old, new in zip(("HEAD", "status", "tags"), before, after, strict=True)
                if old != new
            ]
            raise RuntimeError(
                "real repo changed during --test-build "
                f"({', '.join(parts)} differ); refusing to continue"
            )
        return resolved_output


def expected_version(now: datetime | None = None) -> str:
    """Compute the year.month.commit-count tag for the current month.

    The count covers commits reachable from HEAD since the first of the
    month, exactly like the version-standard get_version.sh script.
    """
    moment = now or datetime.now()
    first_day = f"{moment.year:04d}-{moment.month:02d}-01 00:00:00"
    log = _run_git(["log", f"--since={first_day}", "--oneline"])
    count = len([line for line in log.splitlines() if line.strip()])
    return f"{moment.year % 100}.{moment.month}.{count}"


def working_tree_clean() -> bool:
    """Return whether the checkout has no staged or unstaged changes."""
    return _run_git(["status", "--porcelain"]) == ""


def head_commit() -> str:
    """Return the full SHA of HEAD."""
    return _run_git(["rev-parse", "HEAD"])


def tag_exists(tag: str) -> bool:
    """Return whether a git tag already exists."""
    try:
        _run_git(["rev-parse", "--verify", f"refs/tags/{tag}"])
    except RuntimeError:
        return False
    return True


def tag_commit(tag: str) -> str:
    """Return the commit a tag points at (dereferencing annotated tags)."""
    return _run_git(["rev-list", "-n", "1", tag])


def describe_head() -> str:
    """Return the exact tag at HEAD, raising when HEAD is not tagged."""
    return _run_git(["describe", "--exact-match", "--tags", "HEAD"])


def create_tag(tag: str) -> None:
    """Create the annotated release tag at HEAD, failing when git refuses."""
    _run_git(["tag", "-a", tag, "-m", f"box-rpg-maker {tag}"])


def ensure_on_tag(tag: str, *, create: bool) -> None:
    """Create the tag when missing, then verify HEAD is exactly that tag.

    Fails closed: an existing tag pointing elsewhere, a missing tag, or a
    HEAD that does not describe to the tag all raise instead of building
    from an arbitrary checkout. With create=False (local test builds)
    nothing is created or verified; the resolved tag is only embedded.
    """
    if not is_valid_tag(tag):
        raise RuntimeError(f"refusing malformed tag: {tag!r}")
    if not create:
        return
    if tag_exists(tag):
        if tag_commit(tag) != head_commit():
            raise RuntimeError(f"tag {tag} already exists on another commit; not moving it")
    elif not create:
        raise RuntimeError(f"tag {tag} does not exist and creation is disabled")
    else:
        create_tag(tag)
    if describe_head() != tag:
        raise RuntimeError(f"HEAD is not exactly at tag {tag}; refusing to build")
    if tag_commit(tag) != head_commit():
        raise RuntimeError(f"tag {tag} does not resolve to HEAD; refusing to build")


def apprun_source(tag: str) -> str:
    """Return the generated AppRun script with the build tag baked in."""
    return f"""#!{SYSTEM_PYTHON}
\"\"\"AppRun for the box-rpg-maker AppImage (generated by tools/build_appimage.py).

Runs the staged box-gui payload on the host system Python. The box-rpg
backend is never bundled: a missing or stale backend is owned by the app's
own gate (BackendSetupPage), never by this launcher. Do not edit; regenerate
with python3 -m tools.build_appimage.
\"\"\"
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BUILD_TAG = "{tag}"
TAG_ENV_VAR = "{APPIMAGE_TAG_ENV_VAR}"
PYTHON_ENV_VAR = "BOX_RPG_MAKER_PYTHON"
REEXEC_MARKER = "BOX_RPG_MAKER_REEXECED"
FALLBACK_PYTHONS = ("/usr/local/bin/python3", "/usr/local/bin/python3.14", "/usr/bin/python3.14")


def _fail(message: str) -> int:
    print(f"box-rpg-maker AppImage: {{message}}", file=sys.stderr)
    return 1


def _resolve_python_candidate(candidate: str) -> str | None:
    text = candidate.strip()
    if not text or not os.path.isabs(text):
        return None
    try:
        resolved = str(Path(text).resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    if not os.path.isabs(resolved):
        return None
    return resolved


def _probe_python(path: str) -> str:
    # Classify one candidate as ok, old, nogui, or unusable. Ok means
    # Python 3.14+ with importable GTK bindings; old means the bindings
    # import but the version falls short; nogui means it runs but the
    # bindings do not import; anything else is unusable.
    probe = (
        "import sys;"
        "import gi;"
        "gi.require_version('Gtk', '4.0');"
        "gi.require_version('Adw', '1');"
        "sys.exit(0 if sys.version_info >= (3, 14) else 2)"
    )
    try:
        proc = subprocess.run(
            [path, "-c", probe],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unusable"
    if proc.returncode == 0:
        return "ok"
    if proc.returncode == 2:
        return "old"
    return "nogui"


def _candidate_pythons() -> list[str]:
    candidates: list[str] = []
    override = os.environ.get(PYTHON_ENV_VAR, "").strip()
    if override:
        candidates.append(override)
    found = shutil.which("python3")
    if found:
        candidates.append(found)
    candidates.extend(FALLBACK_PYTHONS)
    return candidates


def _reexec_with_suitable_python() -> str:
    # Exec the first GUI-capable Python 3.14+, else report why nothing fit.
    # Returns ok only when an exec was attempted (which never returns);
    # otherwise nogui when some candidate ran but lacked the GTK bindings,
    # old when the bindings imported only on older interpreters, and
    # unusable when nothing runnable turned up at all.
    # A lying wrapper could report success yet stay old; the marker sterilizes
    # the child so a second pass fails closed instead of exec-looping.
    if os.environ.get(REEXEC_MARKER):
        return "unusable"
    try:
        current = str(Path(sys.executable).resolve())
    except (OSError, RuntimeError, ValueError):
        current = ""
    seen: set[str] = set()
    saw_nogui = False
    saw_old_gui = False
    for candidate in _candidate_pythons():
        resolved = _resolve_python_candidate(candidate)
        if resolved is None or resolved in seen or resolved == current:
            continue
        seen.add(resolved)
        result = _probe_python(resolved)
        if result == "ok":
            try:
                os.execve(resolved, [resolved, *sys.argv], {{**os.environ, REEXEC_MARKER: "1"}})
            except OSError:
                continue
        elif result == "old":
            saw_old_gui = True
        elif result == "nogui":
            saw_nogui = True
    if saw_nogui:
        return "nogui"
    if saw_old_gui:
        return "old"
    return "unusable"


def main() -> int:
    if sys.version_info < (3, 14):
        reason = _reexec_with_suitable_python()
        if reason == "nogui":
            return _fail(
                "No Python 3.14 with GTK bindings found. Install PyGObject plus the "
                "GTK 4 and libadwaita typelibs for a Python 3.14 "
                "(for example: pip install PyGObject pycairo into it), or set "
                "BOX_RPG_MAKER_PYTHON to a Python 3.14 that already has them."
            )
        return _fail(f"Python 3.14+ required, found {{sys.version.split()[0]}}")
    # The re-exec marker sterilized a lying wrapper at most; it must not leak
    # into the real app process and its children.
    os.environ.pop(REEXEC_MARKER, None)
    appdir = Path(__file__).resolve().parent
    payload = appdir / "{PAYLOAD_DIR.as_posix()}"
    source_dir = payload / "src"
    if not (source_dir / "box_gui" / "app.py").is_file():
        return _fail(f"staged payload missing: {{source_dir}}/box_gui")
    os.environ.setdefault(TAG_ENV_VAR, BUILD_TAG)
    sys.path.insert(0, str(source_dir))
    # No hard backend gate here: the app's own gate (BackendSetupPage) owns
    # the missing/stale-backend cases, so always start the staged frontend.
    try:
        from box_gui.app import main as gui_main
    except ImportError as exc:
        return _fail(f"staged frontend failed to import: {{exc}}")
    gui_main(sys.argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _stage_desktop(tag: str) -> str:
    """Return the staged desktop entry: host Exec plus the build tag stamp."""
    try:
        content = (GUI_RES / DESKTOP_FILE_NAME).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"desktop entry missing: {exc}") from exc
    lines = [
        line
        for line in content.splitlines()
        if line.strip() and not line.startswith("X-AppImage-Version=")
    ]
    if "Exec=box-rpg-maker" not in lines:
        raise RuntimeError("desktop entry must keep Exec=box-rpg-maker for AppRun dispatch")
    if f"Icon={APP_ID}" not in lines:
        raise RuntimeError(f"desktop entry must keep Icon={APP_ID}")
    lines.append(f"X-AppImage-Version={tag}")
    return "\n".join(lines) + "\n"


def _reject_backend_payload(payload: Path) -> None:
    """Fail closed when the staged payload would ship the backend."""
    if (payload / "src" / "box").exists():
        raise RuntimeError("refusing to bundle the box-rpg backend in the AppImage")
    wheels = list(payload.rglob("*.whl"))
    if wheels:
        raise RuntimeError(f"refusing to bundle wheels in the AppImage: {wheels[0].name}")


def stage_appdir(appdir: Path, tag: str) -> None:
    """Stage the AppDir tree for the tag, packing only box-gui resources."""
    if not is_valid_tag(tag):
        raise RuntimeError(f"refusing malformed tag: {tag!r}")
    if appdir.exists():
        raise FileExistsError(f"AppDir destination already exists: {appdir}")
    if not GUI_SRC.is_dir():
        raise RuntimeError(f"frontend sources missing: {GUI_SRC}")
    icons_dir = GUI_RES / "icons"
    app_icon = icons_dir / APP_ICON_NAME
    if not app_icon.is_file():
        raise RuntimeError(f"application icon missing: {app_icon}")

    payload = appdir / PAYLOAD_DIR
    staged_gui = payload / "src" / "box_gui"
    shutil.copytree(
        GUI_SRC,
        staged_gui,
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
    )
    if not (staged_gui / "app.py").is_file():
        raise RuntimeError("staged frontend is missing box_gui/app.py")
    # The build tag is generated output: it lives only in the staged
    # payload (never in the checkout) for the Fase 3 reader contract.
    (staged_gui / APPIMAGE_TAG_FILENAME).write_text(tag + "\n", encoding="utf-8")

    staged_res = payload / "res"
    (staged_res / "icons").mkdir(parents=True)
    for icon in sorted(icons_dir.glob("*.svg")):
        shutil.copyfile(icon, staged_res / "icons" / icon.name)
    shutil.copyfile(GUI_RES / DESKTOP_FILE_NAME, staged_res / DESKTOP_FILE_NAME)

    desktop_text = _stage_desktop(tag)
    (appdir / DESKTOP_FILE_NAME).write_text(desktop_text, encoding="utf-8")
    applications_dir = appdir / "usr/share/applications"
    applications_dir.mkdir(parents=True)
    (applications_dir / DESKTOP_FILE_NAME).write_text(desktop_text, encoding="utf-8")

    hicolor_dir = appdir / "usr/share/icons/hicolor/scalable/apps"
    hicolor_dir.mkdir(parents=True)
    for icon in sorted(icons_dir.glob("*.svg")):
        shutil.copyfile(icon, hicolor_dir / icon.name)
    shutil.copyfile(app_icon, appdir / APP_ICON_NAME)
    shutil.copyfile(app_icon, appdir / ".DirIcon")

    apprun = appdir / "AppRun"
    apprun.write_text(apprun_source(tag), encoding="utf-8")
    apprun.chmod(0o755)

    _reject_backend_payload(payload)


def download_appimagetool(url: str, destination: Path) -> Path:
    """Download appimagetool with the standard library, failing closed."""
    request = urllib.request.Request(url, headers={"User-Agent": "box-rpg-maker-appimage/1"})
    try:
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            open(destination, "wb") as stream,
        ):
            shutil.copyfileobj(response, stream)
    except OSError as exc:
        raise RuntimeError(f"appimagetool download failed: {exc}") from exc
    destination.chmod(0o755)
    if destination.stat().st_size < APPIMAGETOOL_MIN_BYTES:
        raise RuntimeError("appimagetool download looks truncated; refusing to run it")
    return destination


def ensure_appimagetool(explicit: Path | None, url: str, directory: Path) -> Path:
    """Return a usable appimagetool binary, downloading it when not provided."""
    if explicit is not None:
        if not explicit.is_file() or not os.access(explicit, os.X_OK):
            raise RuntimeError(f"appimagetool is not executable: {explicit}")
        return explicit
    return download_appimagetool(url, directory / "appimagetool-x86_64.AppImage")


def extract_appimagetool(tool: Path, directory: Path) -> Path:
    """Extract the appimagetool AppImage so FUSE-less CI hosts can run it.

    Building never needs FUSE; only running the produced artifact does.
    Extraction is the default because plain containers often lack
    /dev/fuse, and it exercises the same binary as direct execution.
    """
    if run([str(tool), "--appimage-extract"], cwd=directory) != 0:
        raise RuntimeError("failed to extract appimagetool")
    runner = directory / "squashfs-root" / "AppRun"
    if not runner.is_file() or not os.access(runner, os.X_OK):
        raise RuntimeError("extracted appimagetool is missing its AppRun")
    return runner


def build_artifact(runner: Path, appdir: Path, output: Path) -> Path:
    """Convert the AppDir into the distributable AppImage file."""
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = _command_environment({"ARCH": "x86_64"})
    try:
        proc = subprocess.run(
            [str(runner), str(appdir), str(output)],
            check=False,
            env=environment,
        )
    except OSError as exc:
        raise RuntimeError(f"appimagetool failed to start: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError("appimagetool failed to build the artifact")
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("appimagetool produced no artifact")
    output.chmod(0o755)
    return output


def _prompt(prompt: str) -> str | None:
    """Read one interactive response, treating end-of-input as cancellation."""
    try:
        return input(prompt).strip().lower()
    except EOFError:
        print("Aborted.")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the box-rpg-maker AppImage (frontend only, tagged release)."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="artifact file (or AppDir directory with --appdir-only; "
        "default dist/box-rpg-maker.appimage, "
        "or tools/target/box-rpg-maker-test.appimage with --test-build)",
    )
    parser.add_argument("--tag", help="stable tag to build (default: computed from git)")
    parser.add_argument(
        "--test-build",
        action="store_true",
        help="snapshot the dirty tree to /tmp and build there without touching the repo",
    )
    parser.add_argument(
        "--no-create-tag",
        action="store_true",
        help="embed without creating or verifying tags (local test builds)",
    )
    parser.add_argument(
        "--print-tag",
        action="store_true",
        help="print the resolved tag and exit without changing anything",
    )
    parser.add_argument("--appimagetool", type=Path, help="existing appimagetool binary")
    parser.add_argument(
        "--appimagetool-url",
        default=APPIMAGETOOL_URL,
        help="download URL when --appimagetool is not given",
    )
    parser.add_argument(
        "--appimagetool-mode",
        choices=("extract", "run"),
        default="extract",
        help="extract the tool first for FUSE-less hosts (default) or run it directly",
    )
    parser.add_argument(
        "--appdir-only",
        action="store_true",
        help="stage the AppDir tree at --output and skip appimagetool",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify versions, tag state, and tree cleanliness without changing anything",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--force", action="store_true", help="overwrite an existing --output path")
    args = parser.parse_args()

    if not is_linux():
        print("error: this script requires Linux", file=sys.stderr)
        return 1
    if not _check_python_version():
        print(
            "error: Python 3.14+ required, found "
            f"{sys.version_info.major}.{sys.version_info.minor}",
            file=sys.stderr,
        )
        return 1

    if args.print_tag:
        try:
            printed = args.tag or expected_version()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not is_valid_tag(printed):
            print(f"error: refusing malformed tag: {printed!r}", file=sys.stderr)
            return 1
        print(printed)
        return 0

    if args.test_build and args.check:
        print("error: --test-build and --check are mutually exclusive", file=sys.stderr)
        return 1

    if args.test_build:
        try:
            artifact = test_build(
                args.tag,
                args.output,
                appdir_only=args.appdir_only,
                force=args.force,
                appimagetool=args.appimagetool,
                appimagetool_url=args.appimagetool_url,
                appimagetool_mode=args.appimagetool_mode,
            )
        except (OSError, RuntimeError, FileExistsError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"AppImage ready at {artifact}")
        return 0

    try:
        tag = args.tag or expected_version()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not is_valid_tag(tag):
        print(f"error: refusing malformed tag: {tag!r}", file=sys.stderr)
        return 1
    output = args.output if args.output is not None else Path("dist") / ARTIFACT_NAME

    backend_version = repo_version()
    frontend_version = gui_repo_version()
    if backend_version != tag or frontend_version != tag:
        print(
            "error: distributions are not aligned with the tag "
            f"(box-rpg={backend_version}, box-rpg-maker={frontend_version}, tag={tag}); "
            "land the chore(release): align commit first",
            file=sys.stderr,
        )
        return 1
    print(f"OK: distributions aligned at {tag}.")

    try:
        clean = working_tree_clean()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not clean:
        print("error: working tree has uncommitted changes; commit them first", file=sys.stderr)
        return 1
    print("OK: working tree is clean.")

    if args.check:
        if not args.no_create_tag:
            try:
                described = describe_head()
                print(f"HEAD tag: {described}")
                if described != tag:
                    print(f"error: HEAD tag is {described}, expected {tag}", file=sys.stderr)
                    return 1
            except RuntimeError:
                print(f"Tag {tag} would be created at HEAD.")
        print("Check passed: nothing changed.")
        return 0

    if output.exists() and not args.force:
        print(f"error: output already exists: {output} (pass --force)", file=sys.stderr)
        return 1
    if not args.yes:
        if args.no_create_tag:
            question = f"\nBuild the AppImage for tag {tag} without creating it? [y/N] "
        else:
            question = f"\nCreate tag {tag} at HEAD and build the AppImage? [y/N] "
        answer = _prompt(question)
        if answer not in ("y", "yes"):
            if answer is not None:
                print("Aborted.")
            return 0

    try:
        ensure_on_tag(tag, create=not args.no_create_tag)
        if not args.no_create_tag:
            print(f"OK: HEAD is exactly at tag {tag}.")
        with tempfile.TemporaryDirectory(prefix="box-rpg-maker-appimage-") as directory:
            workspace = Path(directory)
            appdir = workspace / "AppDir"
            stage_appdir(appdir, tag)
            print(f"OK: AppDir staged (frontend only, tag {tag}).")
            if args.appdir_only:
                if output.exists():
                    if not args.force:
                        raise RuntimeError(f"output already exists: {output}")
                    shutil.rmtree(output)
                shutil.copytree(appdir, output)
                print(f"AppDir ready at {output}")
                return 0
            tool = ensure_appimagetool(args.appimagetool, args.appimagetool_url, workspace)
            runner = (
                tool if args.appimagetool_mode == "run" else extract_appimagetool(tool, workspace)
            )
            artifact = build_artifact(runner, appdir, output)
    except (OSError, RuntimeError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"AppImage ready at {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
