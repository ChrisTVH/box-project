#!/usr/bin/env python3
"""Install box-rpg on the system.

Checks that the system is Linux with Python 3.14+ and pip, then installs the
selected distributions at user level: the box-rpg CLI with its shell
completions, and the box-rpg-maker GTK GUI with its desktop entry and icons.
No sudo is needed. By default it only verifies the prerequisites,
reports the installed / repo versions and shows the exact commands; pass
--install to actually run them (with a confirmation prompt) and --yes to skip
the prompt. Pass --uninstall to remove the selected packages and their files.
Pass --target {cli,gui,all} to select which distributions to manage.
"""

from __future__ import annotations

import argparse
import contextlib
import gettext
import json
import os
import re
import shlex
import shutil
import site
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Generator, Sequence
from ctypes.util import find_library
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parent
CLI_ROOT = REPO_ROOT / "box-rpg"
GUI_ROOT = REPO_ROOT / "box-gui"
PACKAGE = "box-rpg"
GUI_PACKAGE = "box-rpg-maker"
PACKAGES = (PACKAGE, GUI_PACKAGE)
DESKTOP_FILE_NAME = "io.gitlab.christvh.BoxRpgApp.desktop"
BWRAP = Path("/usr/bin/bwrap")
GPG = Path("/usr/bin/gpg")

# Managed shell completions are always published from the current checkout:
# install overwrites any existing file and uninstall removes it. Symlinks are
# never followed and raced replacements are preserved, never overwritten.
# update_managed_file serves every managed file (completions, desktop entry,
# icons) through this same protected path.

# Run only under -I: load trusted pip before exposing user metadata. The
# path-entry finder blocks ALL imports from user-site, including lazy imports.
# Unlike addsitedir(), this never processes .pth files or sitecustomize.
USER_PIP_BOOTSTRAP = """
import json, os, site, sys
from pathlib import Path
from importlib import metadata
from pip._internal.cli.main import main

home = Path.home().resolve()
user_site = Path(site.getusersitepackages()).resolve()
if (not home.is_absolute() or not user_site.is_absolute()
        or user_site == home or not user_site.is_relative_to(home)
        or '..' in user_site.parts):
    raise SystemExit('error: user-site must be inside the user home')
descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    current = Path('/')
    for component in user_site.parts[1:]:
        current /= component
        try:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
        except FileNotFoundError:
            break
        os.close(descriptor)
        descriptor = child
        entry = os.fstat(descriptor)
        if current.is_relative_to(home) and (
            entry.st_uid != os.getuid() or entry.st_mode & 0o022
        ):
            raise SystemExit('error: user-site ancestors must be user-owned without group/other write')
finally:
    os.close(descriptor)

class MetadataOnlyFinder:
    def find_spec(self, fullname, target=None):
        return None

sys.path_importer_cache[str(user_site)] = MetadataOnlyFinder()
sys.path.insert(0, str(user_site))
# Permit pip's explicit --user scheme, not Python's site initialization.
site.ENABLE_USER_SITE = True
if sys.argv[1] in ('--verify-user-distribution', 'uninstall'):
    distribution = metadata.distribution('__PACKAGE__')
    location = Path(distribution.locate_file('')).resolve()
    if location != user_site:
        raise SystemExit('error: refusing a non-user-site distribution')
    if sys.argv[1] == '--verify-user-distribution':
        print(json.dumps([str(location), str(user_site)]))
        raise SystemExit(0)
raise SystemExit(main(['--isolated', *sys.argv[1:]]))
"""


def _languages() -> tuple[str, ...] | None:
    """Return locale candidates for the standalone installer."""
    language = os.environ.get("LANGUAGE")
    if language:
        return tuple(value for value in language.split(":") if value)
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            return (value,)
    return None


_ = gettext.NullTranslations().gettext


def _configure_translation() -> None:
    """Load the standalone installer's catalog for the current environment."""
    global _
    _ = gettext.translation(
        "box",
        localedir=CLI_ROOT / "src/box/locale",
        languages=_languages(),
        fallback=True,
    ).gettext
    argparse._ = _  # pyright: ignore[reportAttributeAccessIssue]


# User-level completion dirs (no root required); the parent dirs are created
# on demand during install / uninstall.
COMPLETION_TARGETS = [
    (
        CLI_ROOT / "res/completions/box-rpg.bash",
        Path.home() / ".local/share/bash-completion/completions/box-rpg",
    ),
    (
        CLI_ROOT / "res/completions/box-rpg.fish",
        Path.home() / ".config/fish/completions/box-rpg.fish",
    ),
    (
        CLI_ROOT / "res/completions/_box-rpg",
        Path.home() / ".local/share/zsh/site-functions/_box-rpg",
    ),
]


def _default_gui_targets() -> list[tuple[Path, Path]]:
    """Resolve the GUI desktop entry plus every currently bundled icon."""
    targets = [
        (
            GUI_ROOT / "res" / DESKTOP_FILE_NAME,
            Path.home() / ".local/share/applications" / DESKTOP_FILE_NAME,
        )
    ]
    icons_dir = GUI_ROOT / "res" / "icons"
    if icons_dir.is_dir():
        for icon in sorted(icons_dir.glob("*.svg")):
            targets.append(
                (
                    icon,
                    Path.home() / ".local/share/icons/hicolor/scalable/apps" / icon.name,
                )
            )
    return targets


# Managed GUI files are always published from the current checkout, like
# completions: install overwrites any existing file and uninstall removes it.
# Icon names are never hardcoded; they follow whatever res/icons/ ships.
GUI_TARGETS = _default_gui_targets()


def selected_packages(target: str) -> tuple[str, ...]:
    """Return the distributions covered by an install/uninstall target."""
    if target == "cli":
        return (PACKAGE,)
    if target == "gui":
        return (GUI_PACKAGE,)
    return PACKAGES


def is_linux() -> bool:
    """Return whether the active Python interpreter runs on Linux."""
    return sys.platform.startswith("linux")


def _check_python_version() -> bool:
    return sys.version_info >= (3, 14)


def _versioning_helpers() -> tuple[Callable[[Path], str], Callable[[Path, str], None]] | None:
    """Load (compute_version, write_version_file) from the monorepo tools.

    Tries the ``tools`` package first (normal ``./install.py`` runs from the
    repo root) then the top-level ``versioning`` module (contexts like the
    test suite that put ``tools/`` itself on ``sys.path``). Returns None
    when neither is importable. The declared return type keeps strict
    type-checking exact at the call sites.
    """
    try:
        from tools.versioning import compute_version, write_version_file

        return compute_version, write_version_file
    except ImportError:
        pass
    try:
        from versioning import compute_version as _compute  # type: ignore[no-redef]
        from versioning import write_version_file as _write  # type: ignore[no-redef]

        return cast(
            "tuple[Callable[[Path], str], Callable[[Path, str], None]]",
            (_compute, _write),
        )
    except ImportError:
        return None


def _dynamic_repo_version() -> str:
    """Return the checkout version computed from git history.

    Never reads installed metadata: callers compare Repo against Installed
    to detect updates, so both sides must stay independent. Returns
    ``"unknown"`` outside a git checkout.
    """
    helpers = _versioning_helpers()
    if helpers is None:
        return "unknown"
    compute_version, _ = helpers
    try:
        return compute_version(REPO_ROOT)
    except Exception:
        return "unknown"


def repo_version() -> str:
    """Return the checkout version for the box-rpg distribution."""
    return _dynamic_repo_version()


def gui_repo_version() -> str:
    """Return the checkout version for the box-rpg-maker distribution."""
    return _dynamic_repo_version()


def installed_version(package: str = PACKAGE) -> str | None:
    """Return the installed distribution version, or None if not installed.

    The user site-packages are checked explicitly so the detection works
    even when this script runs from inside a virtualenv (whose pip cannot
    do --user installs and whose site-packages hide the user ones).
    """
    user_site = Path(site.getusersitepackages())
    try:
        from importlib import metadata

        return next(
            distribution.version
            for distribution in metadata.distributions(path=[str(user_site)])
            if distribution.metadata["Name"] == package
        )
    except Exception:  # PackageNotFoundError / import errors
        return None


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts)


def compare_versions(a: str, b: str) -> int:
    """Compare two dotted versions numerically; -1 if a < b, 0 equal, 1 if a > b."""
    ta, tb = _version_tuple(a), _version_tuple(b)
    width = max(len(ta), len(tb))
    ta = ta + (0,) * (width - len(ta))
    tb = tb + (0,) * (width - len(tb))
    return (ta > tb) - (ta < tb)


def _command_environment() -> dict[str, str]:
    """Sanitize the environment for installer subprocesses.

    Loader injection variables must not reach venv/pip children, and PATH is
    pinned because every installer command uses absolute interpreter paths.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "PYTHON", "LD_", "DYLD_")) and key != "VIRTUAL_ENV"
    }
    environment["PATH"] = "/usr/bin:/bin"
    environment["PIP_CONFIG_FILE"] = os.devnull
    return environment


def run(args: list[str]) -> int:
    """Run a command, streaming output, returning its exit code."""
    proc = subprocess.run(args, check=False, env=_command_environment())
    return proc.returncode


def system_python() -> str:
    """Return the system (non-venv) python, since --user installs need it."""
    base = Path(sys.base_exec_prefix) / "bin" / "python3"
    if base.is_file():
        return str(base)
    return sys.executable


def has_pip() -> bool:
    """Return whether the system Python can invoke pip."""
    try:
        result = subprocess.run(
            [system_python(), "-I", "-m", "pip", "--version"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_command_environment(),
        )
    except OSError:
        return False
    return result.returncode == 0


def _tool_runs(command: list[str]) -> bool:
    """Return whether a helper tool executes successfully."""
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_command_environment(),
        )
    except OSError:
        return False
    return result.returncode == 0


def bwrap_problem() -> str | None:
    """Return None when Bubblewrap can launch games, else a short reason."""
    if not (BWRAP.is_file() and os.access(BWRAP, os.X_OK)):
        return "missing"
    if not _tool_runs([str(BWRAP), "--version"]):
        return "broken"
    # Reproduce the namespaces every launch needs; library binds are required
    # or the probe binary cannot even start (misleading exec failure).
    if not _tool_runs(
        [
            str(BWRAP),
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


def gpg_problem() -> str | None:
    """Return None when GnuPG can verify runtimes, else a short reason."""
    if not (GPG.is_file() and os.access(GPG, os.X_OK)):
        return "missing"
    if not _tool_runs([str(GPG), "--version"]):
        return "broken"
    return None


def gtk_problem() -> str | None:
    """Return None when the GTK4/libadwaita typelibs resolve, else a reason."""
    probe = (
        "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1'); "
        "from gi.repository import Gtk, Adw"
    )
    if not _tool_runs([system_python(), "-I", "-c", probe]):
        return "missing"
    return None


def fuse3_problem() -> str | None:
    """Return None when the case-insensitive mount backend is usable, else a reason."""
    try:
        found = find_library("fuse3")
    except Exception:
        return "missing"
    if found is None:
        return "missing"
    if not Path("/dev/fuse").exists():
        return "no-device"
    return None


def icoextract_problem() -> str | None:
    """Return None when game icon extraction is available, else a reason."""
    if not _tool_runs([system_python(), "-I", "-c", "import icoextract"]):
        return "missing"
    return None


allow_system_packages = False
force_reinstall = False


def _break_system_packages_args() -> list[str]:
    """Override PEP 668 only with separate, explicit consent."""
    return ["--break-system-packages"] if allow_system_packages else []


@contextlib.contextmanager
def open_directory(path: Path, *, create: bool = False) -> Generator[int]:
    """Open every absolute path component without following symbolic links."""
    if not path.is_absolute() or ".." in path.parts:
        raise PermissionError(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def read_regular(parent: int, name: str) -> bytes:
    """Read a regular file, never a symlink, FIFO or device."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise PermissionError(name)
        return stream.read()


def remove_matching(parent: int, name: str, expected: bytes) -> None:
    """Capture before checking ownership; never unlink a raced replacement.

    A conflicting replacement is retained in the private recovery directory if
    its original name is occupied. No existing name is overwritten on recovery.
    """
    recovery = f".box-rpg-recovery-{uuid.uuid4().hex}"
    os.mkdir(recovery, mode=0o700, dir_fd=parent)
    with contextlib.ExitStack() as stack:
        descriptor = os.open(recovery, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        stack.callback(os.close, descriptor)
        captured = False
        try:
            os.rename(name, "entry", src_dir_fd=parent, dst_dir_fd=descriptor)
            captured = True
            if read_regular(descriptor, "entry") != expected:
                raise PermissionError(f"Modified or unowned file: {name}")
            os.unlink("entry", dir_fd=descriptor)
            captured = False
        finally:
            if captured:
                try:
                    os.link(
                        "entry",
                        name,
                        src_dir_fd=descriptor,
                        dst_dir_fd=parent,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise PermissionError(f"File preserved in {recovery}/entry") from exc
                os.unlink("entry", dir_fd=descriptor)
            os.rmdir(recovery, dir_fd=parent)


def update_managed_file(source: Path, target: Path, *, uninstall: bool = False) -> None:
    """Publish the current managed file, replacing any existing file on install."""
    home = Path.home()
    if target == home or not target.is_relative_to(home) or ".." in target.parts:
        raise PermissionError(f"Managed file outside home: {target}")
    with open_directory(source.parent) as source_parent:
        expected = read_regular(source_parent, source.name)
    try:
        with open_directory(target.parent, create=not uninstall) as parent:
            try:
                existing = read_regular(parent, target.name)
            except FileNotFoundError:
                if uninstall:
                    return
            else:
                if uninstall or existing != expected:
                    remove_matching(parent, target.name, existing)
                if uninstall or existing == expected:
                    return
            temporary = f".box-rpg-{uuid.uuid4().hex}"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(expected)
                os.link(temporary, target.name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                os.unlink(temporary, dir_fd=parent)
    except FileNotFoundError:
        if not uninstall:
            raise


def _user_distribution_verified(package: str = PACKAGE) -> bool:
    """Verify what the base interpreter (and therefore pip) will select."""
    try:
        result = subprocess.run(
            [*_user_pip_args(package), "--verify-user-distribution"],
            check=False,
            capture_output=True,
            text=True,
            env=_command_environment(),
        )
        payload: object = json.loads(result.stdout)
        if not isinstance(payload, list):
            return False
        locations = cast(list[object], payload)
        return (
            result.returncode == 0
            and len(locations) == 2
            and isinstance(locations[0], str)
            and locations[0] == locations[1]
            and Path(locations[0]).is_relative_to(Path.home().resolve())
        )
    except OSError, ValueError:
        return False


def _user_pip_args(package: str = PACKAGE) -> list[str]:
    return [system_python(), "-I", "-c", USER_PIP_BOOTSTRAP.replace("__PACKAGE__", package)]


def _pip_install_args(
    wheel: Path, *, with_dependencies: bool = False, extra_wheels: Sequence[Path] = ()
) -> list[str]:
    args = [
        *_user_pip_args(),
        "install",
        "--user",
    ]
    if not with_dependencies:
        # The CLI ships zero runtime dependencies; the GUI wheel installs its
        # real dependencies (PyGObject, pycairo, icoextract) instead, with the
        # backend pinned to the just-built local wheel rather than PyPI.
        args += ["--no-deps", "--no-index"]
    args += [
        str(wheel),
        *(str(extra) for extra in extra_wheels),
        "--no-input",
        "--no-warn-script-location",
        "--disable-pip-version-check",
    ]
    if force_reinstall:
        args.append("--force-reinstall")
    return [*args, *_break_system_packages_args()]


def user_bin_without_path() -> Path | None:
    """Return the user script directory when it is missing from PATH."""
    user_bin = Path(site.getusersitepackages()).parents[2] / "bin"
    if str(user_bin) not in os.environ.get("PATH", "").split(os.pathsep):
        return user_bin
    return None


def _staged_version_file(destination: Path, project_dir: Path | None) -> Path:
    """Return where the generated _version.py lives in staged build sources."""
    if (CLI_ROOT if project_dir is None else project_dir) == GUI_ROOT:
        return destination / "src/box_gui/_version.py"
    return destination / "src/box/_version.py"


def copy_build_source(destination: Path, project_dir: Path | None = None) -> None:
    """Stage the trusted checkout without venvs or previous build artifacts."""
    source_root = (
        CLI_ROOT if project_dir is None else project_dir
    )  # Nested workspaces (e.g. the GUI build stages cli/ and gui/ side by
    # side) may not exist yet; still refuse to reuse an existing destination.
    destination.mkdir(parents=True)
    for name in ("pyproject.toml", "README.md"):
        shutil.copyfile(source_root / name, destination / name)
    # The license lives once at the monorepo root but ships in both wheels.
    shutil.copyfile(REPO_ROOT / "LICENSE", destination / "LICENSE")
    for name in ("src", "res"):
        shutil.copytree(
            source_root / name,
            destination / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
        )
    if source_root == GUI_ROOT:
        # The GUI project ships no separate requirements lock by design; its
        # wheel build reuses the shared locked build environment, so stage the
        # backend build lock where _build_commands expects it.
        requirements = destination / "res" / "requirements" / "build.txt"
        requirements.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CLI_ROOT / "res/requirements/build.txt", requirements)
    else:
        # Backend docs live outside the backend project directory in the
        # monorepo; stage them back under docs/ for the share/doc data files.
        monorepo_docs = REPO_ROOT / "docs" / "box-rpg"
        if monorepo_docs.is_dir():
            shutil.copytree(
                monorepo_docs,
                destination / "docs",
                ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
            )
    # Dynamic versions resolve from a generated _version.py that never lives
    # in git: stamp the staged copy so isolated wheel builds (which ship no
    # tools/) work from a fresh clone without a manual stamping step first.
    helpers = _versioning_helpers()
    if helpers is None:
        raise RuntimeError("cannot stamp _version.py: tools/versioning.py is unavailable")
    _compute_version, _write_version_file = helpers
    _write_version_file(
        _staged_version_file(destination, project_dir),
        _compute_version(REPO_ROOT),
    )


def _wheel_args(python: Path, workspace: Path) -> list[str]:
    return [
        str(python),
        "-I",
        "-m",
        "pip",
        "--isolated",
        "wheel",
        "--no-build-isolation",
        "--no-deps",
        "--no-index",
        "--no-input",
        "--disable-pip-version-check",
        "--wheel-dir",
        str(workspace / "wheels"),
        str(workspace / "source"),
    ]


def _build_commands(workspace: Path) -> list[list[str]]:
    python = workspace / "venv/bin/python"
    return [
        [sys.executable, "-I", "-m", "venv", str(workspace / "venv")],
        [
            str(python),
            "-I",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--require-hashes",
            "--only-binary=:all:",
            "--force-reinstall",
            "--index-url",
            "https://pypi.org/simple",
            "--no-input",
            "--disable-pip-version-check",
            "-r",
            str(workspace / "source/res/requirements/build.txt"),
        ],
        _wheel_args(python, workspace),
    ]


def build_wheel(
    workspace: Path,
    project_dir: Path | None = None,
    *,
    wheel_glob: str = "box_rpg-*.whl",
    label: str = PACKAGE,
) -> Path:
    """Build with hash-checked tools in a private, disposable environment."""
    copy_build_source(workspace / "source", project_dir)
    for command in _build_commands(workspace):
        if run(command) != 0:
            raise RuntimeError("locked wheel build failed")
    wheels = list((workspace / "wheels").glob(wheel_glob))
    if len(wheels) != 1 or not wheels[0].is_file() or wheels[0].is_symlink():
        raise RuntimeError(f"expected exactly one {label} wheel")
    return wheels[0]


def _pip_uninstall_args(package: str = PACKAGE) -> list[str]:
    """Build pip's user-level uninstall command."""
    args = [*_user_pip_args(package), "uninstall", "-y", package]
    return [*args, *_break_system_packages_args()]


def install_commands(target: str = "all") -> list[str]:
    workspace = Path("<private-temporary-directory>")
    cmds: list[str] = []
    if target in ("cli", "all"):
        cmds.append(
            "stage trusted checkout in private temporary directory; "
            + " && ".join(shlex.join(command) for command in _build_commands(workspace))
            + " && "
            + shlex.join(_pip_install_args(workspace / "wheels/box_rpg-<version>-py3-none-any.whl"))
            + "; clean up temporary directory"
        )
        for source, destination in COMPLETION_TARGETS:
            cmds.append(
                f"safe completion install: {shlex.quote(str(source))}"
                f" -> {shlex.quote(str(destination))}"
            )
    if target in ("gui", "all"):
        cmds.append(
            "stage trusted GUI checkout in private temporary directory; "
            + " && ".join(shlex.join(command) for command in _build_commands(workspace))
            + " && "
            + shlex.join(
                _pip_install_args(
                    workspace / "wheels/box_rpg_maker-<version>-py3-none-any.whl",
                    extra_wheels=(workspace / "wheels/box_rpg-<version>-py3-none-any.whl",),
                    with_dependencies=True,
                )
            )
            + "; clean up temporary directory"
        )
        for source, destination in GUI_TARGETS:
            cmds.append(
                f"safe GUI file install: {shlex.quote(str(source))}"
                f" -> {shlex.quote(str(destination))}"
            )
    return cmds


def run_install(target: str = "all") -> bool:
    if os.geteuid() == 0:
        print(_("error: refusing to run as root"), file=sys.stderr)
        return False
    ok = True
    if target in ("cli", "all"):
        try:
            with tempfile.TemporaryDirectory(prefix="box-rpg-build-") as directory:
                wheel = build_wheel(Path(directory))
                if run(_pip_install_args(wheel)) != 0:
                    raise RuntimeError("pip install failed")
        except (OSError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return False
        done = 0
        for source, destination in COMPLETION_TARGETS:
            if not source.exists():
                print(
                    _("warning: completion source missing: {source}").format(source=source),
                    file=sys.stderr,
                )
                continue
            try:
                update_managed_file(source, destination)
            except OSError as exc:
                print(
                    _("error: cannot install completion {target}: {error}").format(
                        target=destination, error=exc
                    ),
                    file=sys.stderr,
                )
                ok = False
                continue
            done += 1
        print(_("installed {count} shell completions").format(count=done))
    if target in ("gui", "all"):
        try:
            with tempfile.TemporaryDirectory(prefix="box-rpg-maker-build-") as directory:
                base = Path(directory)
                # The GUI depends on the backend, so pin it to the just-built
                # local wheel in the same pip invocation instead of PyPI.
                cli_wheel = build_wheel(base / "cli")
                gui_wheel = build_wheel(
                    base / "gui",
                    GUI_ROOT,
                    wheel_glob="box_rpg_maker-*.whl",
                    label=GUI_PACKAGE,
                )
                if (
                    run(
                        _pip_install_args(
                            gui_wheel, extra_wheels=(cli_wheel,), with_dependencies=True
                        )
                    )
                    != 0
                ):
                    raise RuntimeError("pip install failed")
        except (OSError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return False
        done = 0
        for source, destination in GUI_TARGETS:
            if not source.exists():
                print(
                    _("warning: GUI source missing: {source}").format(source=source),
                    file=sys.stderr,
                )
                continue
            try:
                update_managed_file(source, destination)
            except OSError as exc:
                print(
                    _("error: cannot install GUI file {target}: {error}").format(
                        target=destination, error=exc
                    ),
                    file=sys.stderr,
                )
                ok = False
                continue
            done += 1
        print(_("installed {count} GUI files").format(count=done))
    return ok


def uninstall_commands(target: str = "all") -> list[str]:
    cmds: list[str] = []
    if target in ("cli", "all"):
        cmds.append(shlex.join(_pip_uninstall_args()))
        for _source, destination in COMPLETION_TARGETS:
            cmds.append(f"safe completion removal: {shlex.quote(str(destination))}")
    if target in ("gui", "all"):
        cmds.append(shlex.join(_pip_uninstall_args(GUI_PACKAGE)))
        for _source, destination in GUI_TARGETS:
            cmds.append(f"safe GUI file removal: {shlex.quote(str(destination))}")
    return cmds


def run_uninstall(target: str = "all") -> bool:
    if os.geteuid() == 0:
        print(
            _("error: uninstall requires a verified user-site package and a non-root user"),
            file=sys.stderr,
        )
        return False
    ok = True
    for package in selected_packages(target):
        if installed_version(package) is None:
            continue
        if not _user_distribution_verified(package):
            print(
                _("error: uninstall requires a verified user-site package and a non-root user"),
                file=sys.stderr,
            )
            return False
        if run(_pip_uninstall_args(package)) != 0:
            print(_("error: pip uninstall failed"), file=sys.stderr)
            ok = False
    managed: list[tuple[Path, Path]] = []
    if target in ("cli", "all"):
        managed += COMPLETION_TARGETS
    if target in ("gui", "all"):
        managed += GUI_TARGETS
    done = 0
    for source, destination in managed:
        try:
            update_managed_file(source, destination, uninstall=True)
        except OSError as exc:
            print(
                _("error: failed to remove managed file {target}: {error}").format(
                    target=destination, error=exc
                ),
                file=sys.stderr,
            )
            ok = False
        else:
            done += 1
    print(_("removed {count} managed files").format(count=done))
    return ok


def print_commands(title: str, cmds: list[str]) -> None:
    print(f"\n{title}")
    for cmd in cmds:
        print(f"  {cmd}")


def _prompt(prompt: str) -> str | None:
    """Read one interactive response, treating end-of-input as cancellation."""
    try:
        return input(prompt).strip().lower()
    except EOFError:
        print(_("Aborted."))
        return None


def main() -> int:
    global allow_system_packages, force_reinstall
    _configure_translation()
    parser = argparse.ArgumentParser(
        description=_(
            """Install box-rpg on the system.

Checks that the system is Linux with Python 3.14+ and pip, then installs the
selected distributions at user level: the box-rpg CLI with its shell
completions, and the box-rpg-maker GTK GUI with its desktop entry and icons.
No sudo is needed. By default it only verifies prerequisites,
reports installed / repo versions and shows the exact commands; pass --install
to actually run them (with a confirmation prompt) and --yes to skip the
prompt. Pass --uninstall to remove the selected packages and their files.
Pass --target {cli,gui,all} to select which distributions to manage."""
        )
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help=_("actually install (default is a dry run)"),
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help=_("uninstall the selected packages and their files"),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=_("skip the confirmation prompts"),
    )
    parser.add_argument(
        "--break-system-packages",
        action="store_true",
        help="explicitly allow pip to override PEP 668 (not implied by --yes)",
    )
    parser.add_argument(
        "--force-reinstall",
        action="store_true",
        help=_("reinstall the package even when the same version is installed"),
    )
    parser.add_argument(
        "--target",
        choices=("cli", "gui", "all"),
        default="all",
        help=_("select which distributions to manage: cli, gui, or all"),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=_("show the exact commands that would run"),
    )
    args = parser.parse_args()
    allow_system_packages = args.break_system_packages
    force_reinstall = args.force_reinstall

    if args.install and args.uninstall:
        print(_("error: --install and --uninstall are mutually exclusive"), file=sys.stderr)
        return 1

    if not is_linux():
        print(_("error: this script requires Linux"), file=sys.stderr)
        return 1
    print(_("OK: Linux system detected."))

    if not _check_python_version():
        print(
            _("error: Python 3.14+ required, found {major}.{minor}").format(
                major=sys.version_info.major, minor=sys.version_info.minor
            ),
            file=sys.stderr,
        )
        return 1
    print(
        _("OK: Python {major}.{minor} found.").format(
            major=sys.version_info.major, minor=sys.version_info.minor
        )
    )

    if not has_pip():
        print(_("error: pip is required for the system Python"), file=sys.stderr)
        return 1
    print(_("OK: pip found for the system Python."))

    installed = {package: installed_version(package) for package in selected_packages(args.target)}
    if args.target in ("cli", "all"):
        print(_("\nInstalled: {installed}").format(installed=installed[PACKAGE] or "none"))
        print(_("Repo:      {repo}").format(repo=repo_version()))
    if args.target in ("gui", "all"):
        print(_("\nInstalled GUI: {installed}").format(installed=installed[GUI_PACKAGE] or "none"))
        print(_("Repo GUI:      {repo}").format(repo=gui_repo_version()))

    if args.uninstall:
        if all(version is None for version in installed.values()):
            names = ", ".join(selected_packages(args.target))
            print(_("error: {package} is not installed").format(package=names), file=sys.stderr)
            return 1
        cmds = uninstall_commands(args.target)
        print_commands(_("Uninstall commands that would be run:"), cmds)
        if not args.yes:
            answer = _prompt(_("\nProceed with uninstall? [y/N] "))
            if answer not in ("y", "yes"):
                if answer is not None:
                    print(_("Aborted."))
                return 0
        ok = run_uninstall(args.target)
        return 0 if ok else 1

    problem = bwrap_problem()
    if problem == "missing":
        print(
            _("error: Bubblewrap ({bwrap}) is required to launch games").format(bwrap=BWRAP),
            file=sys.stderr,
        )
        return 1
    if problem == "broken":
        print(
            _("error: Bubblewrap ({bwrap}) is installed but does not run").format(bwrap=BWRAP),
            file=sys.stderr,
        )
        return 1
    if problem == "userns":
        print(
            _(
                "error: user namespaces are blocked, so the game sandbox cannot start "
                "(check: sysctl kernel.unprivileged_userns_clone)"
            ),
            file=sys.stderr,
        )
        return 1
    print(_("OK: Bubblewrap is ready to launch games."))
    gpg_issue = gpg_problem()
    if gpg_issue == "missing":
        print(
            _("error: GnuPG ({gpg}) is required to install verified runtimes").format(gpg=GPG),
            file=sys.stderr,
        )
        return 1
    if gpg_issue == "broken":
        print(
            _("error: GnuPG ({gpg}) is installed but does not run").format(gpg=GPG),
            file=sys.stderr,
        )
        return 1
    print(_("OK: GnuPG found."))

    fuse_issue = fuse3_problem()
    if fuse_issue == "missing":
        print(
            _(
                "warning: libfuse3 not found; the case-insensitive mount (--ci-mount) will be unavailable"
            ),
            file=sys.stderr,
        )
    elif fuse_issue == "no-device":
        print(
            _(
                "warning: /dev/fuse not found; the case-insensitive mount (--ci-mount) will be unavailable"
            ),
            file=sys.stderr,
        )
    else:
        print(_("OK: libfuse3 found for the case-insensitive mount."))

    icoextract_issue = icoextract_problem()
    if icoextract_issue == "missing":
        print(
            _("warning: icoextract not found; game icon extraction will be unavailable"),
            file=sys.stderr,
        )
    else:
        print(_("OK: icoextract found for game icon extraction."))

    if args.target in ("gui", "all"):
        if gtk_problem() == "missing":
            print(
                _(
                    "error: GTK 4 and libadwaita typelibs are required to install "
                    "the GUI (needs PyGObject with Gtk 4.0 and Adw 1)"
                ),
                file=sys.stderr,
            )
            return 1
        print(_("OK: GTK 4 and libadwaita found."))

    cmds = install_commands(args.target)
    if args.verbose:
        print_commands(_("Install commands that would be run:"), cmds)
    else:
        print(_("Install plan:"))
        if args.target in ("cli", "all"):
            print(
                _("  - Build {package} {repo} in a private directory.").format(
                    package=PACKAGE, repo=repo_version()
                )
            )
            print(_("  - Install it for your user."))
            print(_("  - Install {count} shell completions.").format(count=len(COMPLETION_TARGETS)))
        if args.target in ("gui", "all"):
            print(
                _("  - Build {package} {repo} in a private directory.").format(
                    package=GUI_PACKAGE, repo=gui_repo_version()
                )
            )
            print(_("  - Install it for your user with its runtime dependencies."))
            print(_("  - Install {count} GUI files.").format(count=len(GUI_TARGETS)))

    selected_repos: list[tuple[str, str]] = []
    if args.target in ("cli", "all"):
        selected_repos.append((PACKAGE, repo_version()))
    if args.target in ("gui", "all"):
        selected_repos.append((GUI_PACKAGE, gui_repo_version()))
    for package, repo in selected_repos:
        current = installed[package]
        if current is None:
            continue
        prefix = f"{package}: " if len(selected_repos) > 1 else ""
        cmp = compare_versions(repo, current)
        if cmp > 0:
            print(
                prefix
                + _("\nAn update is available (installed: {installed}, repo: {repo}).").format(
                    installed=current, repo=repo
                )
            )
        elif cmp < 0:
            print(
                prefix
                + _(
                    "\nThe installed version ({installed}) is newer than the repo ({repo})."
                ).format(installed=current, repo=repo)
            )
        elif force_reinstall:
            print(
                prefix
                + _("\nReinstalling the same version ({installed}) as requested.").format(
                    installed=current
                )
            )
        else:
            print(
                prefix
                + _("\nThe same version ({installed}) is already installed.").format(
                    installed=current
                )
            )

    if not args.install:
        print(_("\nRun with --install to actually install."))
        if any(version is not None for version in installed.values()):
            print(_("To remove it instead, run with --uninstall."))
        return 0

    if any(version is not None for version in installed.values()) and not args.yes:
        names = ", ".join(
            package for package in selected_packages(args.target) if installed[package] is not None
        )
        answer = _prompt(
            _(
                "\n{package} is already installed. "
                "[r] Reinstall/update, [u] Uninstall, [c] Cancel [r/u/c] "
            ).format(package=names)
        )
        if answer in ("u", "uninstall"):
            confirmation = _prompt(_("\nConfirm uninstall? [y/N] "))
            if confirmation not in ("y", "yes"):
                if confirmation is not None:
                    print(_("Aborted."))
                return 0
            ok = run_uninstall(args.target)
            return 0 if ok else 1
        if answer not in ("r", "reinstall", ""):
            if answer is not None:
                print(_("Aborted."))
            return 0
    elif not args.yes:
        answer = _prompt(_("\nProceed with installation? [y/N] "))
        if answer not in ("y", "yes"):
            if answer is not None:
                print(_("Aborted."))
            return 0

    ok = run_install(args.target)
    if ok:
        missing = user_bin_without_path()
        if missing is not None:
            print(
                _("warning: {bindir} is not on PATH; add it to run {prog} from anywhere").format(
                    bindir=missing, prog=PACKAGE
                )
            )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
