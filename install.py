#!/usr/bin/env python3
"""Install box-rpg on the system.

Verifies that the system is Arch Linux (or Arch-based) and installs the
package at user level with pip (--user --break-system-packages for PEP 668),
then places the shell completions in each shell's user directory. No sudo is
needed. By default it only verifies the OS, reports the installed / repo
versions and shows the exact commands; pass --install to actually run them
(with a confirmation prompt) and --yes to skip the prompt. Pass --uninstall
to remove the package and its completions.
"""

from __future__ import annotations

import argparse
import re
import shlex
import site
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
OS_RELEASE = Path("/etc/os-release")
PACMAN = Path("/usr/bin/pacman")
PACKAGE = "box-rpg"
INIT_PY = REPO_ROOT / "src/box/__init__.py"

# User-level completion dirs (no root required); the parent dirs are created
# on demand during install / uninstall.
COMPLETION_TARGETS = [
    (
        REPO_ROOT / "res/completions/box-rpg.bash",
        Path.home() / ".local/share/bash-completion/completions/box-rpg",
    ),
    (
        REPO_ROOT / "res/completions/box-rpg.fish",
        Path.home() / ".config/fish/completions/box-rpg.fish",
    ),
    (
        REPO_ROOT / "res/completions/_box-rpg",
        Path.home() / ".local/share/zsh/site-functions/_box-rpg",
    ),
]


def _read_os_release() -> str:
    try:
        return OS_RELEASE.read_text(encoding="utf-8")
    except OSError:
        return ""


def _field(content: str, name: str) -> str:
    prefix = f"{name}="
    for line in content.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip('"')
    return ""


def is_arch() -> bool:
    """True if the system is Arch Linux or Arch-based (ID / ID_LIKE / pacman)."""
    content = _read_os_release()
    ids = " ".join(value for field in ("ID", "ID_LIKE") if (value := _field(content, field)))
    if "arch" in ids.lower():
        return True
    return PACMAN.exists()


def _check_python_version() -> bool:
    return sys.version_info >= (3, 14)


def repo_version() -> str:
    """Return the package version from src/box/__init__.py."""
    try:
        content = INIT_PY.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    return match.group(1) if match else "unknown"


def installed_version() -> str | None:
    """Return the installed distribution version, or None if not installed.

    The user site-packages are checked explicitly so the detection works
    even when this script runs from inside a virtualenv (whose pip cannot
    do --user installs and whose site-packages hide the user ones).
    """
    user_site = Path(site.getusersitepackages())
    if str(user_site) not in sys.path:
        sys.path.insert(0, str(user_site))
    try:
        from importlib import metadata

        return metadata.version(PACKAGE)
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


def run(args: list[str]) -> int:
    """Run a command, streaming output, returning its exit code."""
    proc = subprocess.run(args, check=False)
    return proc.returncode


def system_python() -> str:
    """Return the system (non-venv) python, since --user installs need it."""
    base = Path(sys.base_exec_prefix) / "bin" / "python3"
    if base.is_file():
        return str(base)
    return sys.executable


def _pip_install_args() -> list[str]:
    return [
        system_python(),
        "-m",
        "pip",
        "install",
        "--user",
        str(REPO_ROOT),
        "--break-system-packages",
        "--no-input",
        "--disable-pip-version-check",
    ]


def install_commands() -> list[str]:
    cmds = [shlex.join(_pip_install_args())]
    for source, target in COMPLETION_TARGETS:
        cmds.append(f"mkdir -p {target.parent}")
        cmds.append(f"cp {source} {target}")
    return cmds


def run_install() -> bool:
    ok = True
    if run(_pip_install_args()) != 0:
        print("error: pip install failed", file=sys.stderr)
        ok = False
    for source, target in COMPLETION_TARGETS:
        if not source.exists():
            print(f"warning: completion source missing: {source}", file=sys.stderr)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"error: cannot create directory {target.parent}: {exc}", file=sys.stderr)
            ok = False
            continue
        if run(["cp", str(source), str(target)]) != 0:
            print(f"error: failed to install completion to {target}", file=sys.stderr)
            ok = False
        else:
            print(f"installed completion {target}")
    return ok


def uninstall_commands() -> list[str]:
    cmds = [
        f"{system_python()} -m pip uninstall -y {PACKAGE} --break-system-packages",
    ]
    for _, target in COMPLETION_TARGETS:
        cmds.append(f"rm -f {target}")
    return cmds


def run_uninstall() -> bool:
    ok = True
    if (
        run(
            [
                system_python(),
                "-m",
                "pip",
                "uninstall",
                "-y",
                PACKAGE,
                "--break-system-packages",
            ]
        )
        != 0
    ):
        print("error: pip uninstall failed", file=sys.stderr)
        ok = False
    for _, target in COMPLETION_TARGETS:
        if run(["rm", "-f", str(target)]) != 0:
            print(f"error: failed to remove completion {target}", file=sys.stderr)
            ok = False
        else:
            print(f"removed completion {target}")
    return ok


def print_commands(title: str, cmds: list[str]) -> None:
    print(f"\n{title}")
    for cmd in cmds:
        print(f"  {cmd}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install",
        action="store_true",
        help="actually install (default is a dry run)",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="uninstall the package and its completions",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompts",
    )
    args = parser.parse_args()

    if args.install and args.uninstall:
        print("error: --install and --uninstall are mutually exclusive", file=sys.stderr)
        return 1

    if not is_arch():
        print(
            "error: this script is meant for Arch Linux or Arch-based systems "
            "(no /etc/os-release 'arch' ID/ID_LIKE and no /usr/bin/pacman)",
            file=sys.stderr,
        )
        return 1
    print("OK: Arch Linux (or Arch-based) system detected.")

    if not _check_python_version():
        print(
            f"error: Python 3.14+ required, found {sys.version_info.major}.{sys.version_info.minor}",
            file=sys.stderr,
        )
        return 1
    print(f"OK: Python {sys.version_info.major}.{sys.version_info.minor} found.")

    installed = installed_version()
    repo = repo_version()
    print(f"\nInstalled: {installed or 'none'}")
    print(f"Repo:      {repo}")

    if args.uninstall:
        if installed is None:
            print(f"error: {PACKAGE} is not installed", file=sys.stderr)
            return 1
        cmds = uninstall_commands()
        print_commands("Uninstall commands that would be run:", cmds)
        if not args.yes:
            answer = input("\nProceed with uninstall? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 0
        ok = run_uninstall()
        return 0 if ok else 1

    cmds = install_commands()
    print_commands("Install commands that would be run:", cmds)

    if installed is not None:
        cmp = compare_versions(repo, installed)
        if cmp > 0:
            print(f"\nAn update is available (installed: {installed}, repo: {repo}).")
        elif cmp < 0:
            print(f"\nThe installed version ({installed}) is newer than the repo ({repo}).")
        else:
            print(f"\nThe same version ({installed}) is already installed.")

    if not args.install:
        print("\nRun with --install to actually install.")
        if installed is not None:
            print("To remove it instead, run with --uninstall.")
        return 0

    if installed is not None and not args.yes:
        answer = (
            input(
                f"\n{PACKAGE} is already installed. "
                "[r] Reinstall/update, [u] Uninstall, [c] Cancel [r/u/c] "
            )
            .strip()
            .lower()
        )
        if answer in ("u", "uninstall"):
            if input("\nConfirm uninstall? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 0
            ok = run_uninstall()
            return 0 if ok else 1
        if answer not in ("r", "reinstall", ""):
            print("Aborted.")
            return 0
    elif not args.yes:
        answer = input("\nProceed with installation? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 0

    ok = run_install()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
