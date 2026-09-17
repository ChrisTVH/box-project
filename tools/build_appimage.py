#!/usr/bin/env python3
"""Build the box-rpg-maker AppImage from the box-gui checkout (Python pure).

The artifact is ``box-rpg-maker.appimage``: a type 2 AppImage packing ONLY
the ``box-gui`` frontend (GTK4 + libadwaita) with its resources (``res/``,
in-package ``locale/``, desktop entry, icons). The ``box-rpg`` backend is
never bundled; the AppImage runs on the host system Python (>= 3.14) and
``import box.api`` resolves against whatever ``install.py`` installed, so
no Python interpreter is vendored either.

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
    python3 -m tools.build_appimage --yes
    python3 -m tools.build_appimage --yes --appdir-only --output dist/AppDir

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


def _run_git(args: list[str]) -> str:
    """Run one git command, returning stripped stdout or raising on failure."""
    try:
        proc = subprocess.run(
            ["git", *args],
            check=False,
            capture_output=True,
            text=True,
            env=_command_environment(),
            cwd=REPO_ROOT,
        )
    except OSError as exc:
        raise RuntimeError(f"git unavailable: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


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

    Fails closed: an existing tag pointing elsewhere, a missing tag with
    creation disabled, or a HEAD that does not describe to the tag all
    raise instead of building from an arbitrary checkout.
    """
    if not is_valid_tag(tag):
        raise RuntimeError(f"refusing malformed tag: {tag!r}")
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
backend is never bundled: it must be installed for the system Python
(see install.py). Do not edit; regenerate with python3 -m tools.build_appimage.
\"\"\"
from __future__ import annotations

import os
import sys
from pathlib import Path

BUILD_TAG = "{tag}"
TAG_ENV_VAR = "{APPIMAGE_TAG_ENV_VAR}"


def _fail(message: str) -> int:
    print(f"box-rpg-maker AppImage: {{message}}", file=sys.stderr)
    return 1


def main() -> int:
    if sys.version_info < (3, 14):
        return _fail(f"Python 3.14+ required, found {{sys.version.split()[0]}}")
    appdir = Path(__file__).resolve().parent
    payload = appdir / "{PAYLOAD_DIR.as_posix()}"
    source_dir = payload / "src"
    if not (source_dir / "box_gui" / "app.py").is_file():
        return _fail(f"staged payload missing: {{source_dir}}/box_gui")
    os.environ.setdefault(TAG_ENV_VAR, BUILD_TAG)
    sys.path.insert(0, str(source_dir))
    try:
        import box.api  # noqa: F401 -- backend comes from the host install.
    except ImportError:
        return _fail(
            "backend box-rpg is not installed for the system Python; "
            "run ./install.py --install --target cli first"
        )
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
        default=Path("dist") / ARTIFACT_NAME,
        help="artifact file (or AppDir directory with --appdir-only)",
    )
    parser.add_argument("--tag", help="stable tag to build (default: computed from git)")
    parser.add_argument(
        "--no-create-tag",
        action="store_true",
        help="only verify the tag exists at HEAD instead of creating it",
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

    try:
        tag = args.tag or expected_version()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not is_valid_tag(tag):
        print(f"error: refusing malformed tag: {tag!r}", file=sys.stderr)
        return 1

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
        try:
            described = describe_head()
            print(f"HEAD tag: {described}")
            if described != tag:
                print(f"error: HEAD tag is {described}, expected {tag}", file=sys.stderr)
                return 1
        except RuntimeError:
            if args.no_create_tag:
                print(f"error: tag {tag} is not at HEAD", file=sys.stderr)
                return 1
            print(f"Tag {tag} would be created at HEAD.")
        print("Check passed: nothing changed.")
        return 0

    if args.output.exists() and not args.force:
        print(f"error: output already exists: {args.output} (pass --force)", file=sys.stderr)
        return 1
    if not args.yes:
        answer = _prompt(f"\nCreate tag {tag} at HEAD and build the AppImage? [y/N] ")
        if answer not in ("y", "yes"):
            if answer is not None:
                print("Aborted.")
            return 0

    try:
        ensure_on_tag(tag, create=not args.no_create_tag)
        print(f"OK: HEAD is exactly at tag {tag}.")
        with tempfile.TemporaryDirectory(prefix="box-rpg-maker-appimage-") as directory:
            workspace = Path(directory)
            appdir = workspace / "AppDir"
            stage_appdir(appdir, tag)
            print(f"OK: AppDir staged (frontend only, tag {tag}).")
            if args.appdir_only:
                if args.output.exists():
                    if not args.force:
                        raise RuntimeError(f"output already exists: {args.output}")
                    shutil.rmtree(args.output)
                shutil.copytree(appdir, args.output)
                print(f"AppDir ready at {args.output}")
                return 0
            tool = ensure_appimagetool(args.appimagetool, args.appimagetool_url, workspace)
            runner = (
                tool if args.appimagetool_mode == "run" else extract_appimagetool(tool, workspace)
            )
            artifact = build_artifact(runner, appdir, args.output)
    except (OSError, RuntimeError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"AppImage ready at {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
