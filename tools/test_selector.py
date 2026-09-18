"""Change-based test selection for the box-project monorepo.

Both packages already mirror their layout 1:1 (``src/box/runtime`` <->
``tests/runtime``, ``src/box_gui/pages`` <-> ``tests/pages``, ...), so a
changed source file can be mapped to its test group by directory name alone
-- no manifest to keep in sync. A change that cannot be scoped safely (a
top-level module, a src subdir with no matching tests/ dir, a conftest.py,
shared root tooling) falls back to running that package's full suite.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tools.versioning import _find_repo_root

# Package name -> its source root, relative to the package directory.
_SRC_ROOT = {
    "box-rpg": "src/box",
    "box-gui": "src/box_gui",
}

# Root files that only box-rpg/tests/tools exercises (test_install.py,
# test_cleaner.py, test_appimage.py, test_packaging.py import them directly).
_ROOT_TOOLS_TRIGGERS = ("install.py", "cleaner.py", "tools/")
_ROOT_TOOLS_TARGET = ("box-rpg", "tools")

# Source prefixes that are a stable cross-package contract: a change here
# can affect the other package even though it lives outside its tree.
# box-gui/AGENTS.md: "through the stable box.api surface".
_CROSS_PACKAGE_FULL = {
    "box-rpg/src/box/api/": "box-gui",
}


@dataclass
class Plan:
    groups: dict[str, set[str]] = field(default_factory=dict)
    files: dict[str, set[str]] = field(default_factory=dict)
    full: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)

    def add_group(self, pkg: str, group: str, reason: str) -> None:
        self.groups.setdefault(pkg, set()).add(group)
        self.reasons.append(reason)

    def add_file(self, pkg: str, rel_path: str, reason: str) -> None:
        self.files.setdefault(pkg, set()).add(rel_path)
        self.reasons.append(reason)

    def mark_full(self, pkg: str, reason: str) -> None:
        self.full.add(pkg)
        self.reasons.append(reason)


def get_changed_files(repo_root: Path, base: str | None = None) -> list[str]:
    """Return changed file paths, relative to ``repo_root``, POSIX-style.

    With no ``base``, diffs the working tree (staged + unstaged) against
    HEAD and adds untracked files -- "what am I about to test right now".
    With ``base``, diffs that ref against the working tree instead, for
    reviewing a whole branch (e.g. ``--base origin/main``).
    """
    diff_target = [base] if base else ["HEAD"]
    tracked = subprocess.run(
        ["git", "diff", "--name-only", *diff_target],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    paths = tracked.stdout.splitlines() + untracked.stdout.splitlines()
    return sorted({p.strip() for p in paths if p.strip()})


def build_plan(changed_files: list[str], repo_root: Path) -> Plan:
    plan = Plan()
    for path in changed_files:
        _classify(path, repo_root, plan)
    return plan


def _classify(path: str, repo_root: Path, plan: Plan) -> None:
    parts = path.split("/")
    pkg = parts[0] if parts[0] in _SRC_ROOT else None

    if pkg is None:
        if path.startswith(_ROOT_TOOLS_TRIGGERS):
            plan.add_group(*_ROOT_TOOLS_TARGET, f"{path} -> box-rpg/tests/tools")
        else:
            plan.reasons.append(f"{path}: no pytest coverage, skipped")
        return

    rest = "/".join(parts[1:])

    if rest.startswith("tests/"):
        _classify_test_file(pkg, rest, plan)
        return

    src_prefix = _SRC_ROOT[pkg]
    if rest.startswith(src_prefix + "/"):
        _classify_src_file(pkg, path, rest[len(src_prefix) + 1 :], repo_root, plan)
        return

    if rest == "pyproject.toml":
        plan.mark_full(pkg, f"{path}: pytest/tool config changed")
    else:
        plan.reasons.append(f"{path}: no pytest coverage, skipped")


def _classify_test_file(pkg: str, rest: str, plan: Plan) -> None:
    sub = rest[len("tests/") :]
    segments = sub.split("/")
    if len(segments) == 1:
        if segments[0] == "conftest.py":
            plan.mark_full(pkg, f"{pkg}/tests/conftest.py changed")
        else:
            plan.add_file(pkg, rest, f"{rest} changed directly")
    else:
        plan.add_group(pkg, segments[0], f"{rest} changed directly")


def _classify_src_file(pkg: str, full_path: str, sub: str, repo_root: Path, plan: Plan) -> None:
    segments = sub.split("/")
    if len(segments) == 1:
        plan.mark_full(pkg, f"{full_path}: top-level module, no scoped group")
        return

    group = segments[0]
    if (repo_root / pkg / "tests" / group).is_dir():
        plan.add_group(pkg, group, f"{full_path} -> {pkg}/tests/{group}")
    else:
        plan.mark_full(pkg, f"{full_path}: no matching tests/{group}/, running full {pkg} suite")

    for trigger, target_pkg in _CROSS_PACKAGE_FULL.items():
        if full_path.startswith(trigger):
            plan.mark_full(target_pkg, f"{full_path}: touches the stable contract, checking {target_pkg}")


def pytest_commands(plan: Plan) -> dict[str, list[str]]:
    """Build the ``pytest`` argv per package (run each with cwd=<package>)."""
    commands: dict[str, list[str]] = {}
    packages = set(plan.groups) | set(plan.files) | plan.full
    for pkg in sorted(packages):
        if pkg in plan.full:
            commands[pkg] = ["python3", "-m", "pytest"]
            continue
        targets = sorted(f"tests/{g}" for g in plan.groups.get(pkg, ()))
        targets += sorted(plan.files.get(pkg, ()))
        commands[pkg] = ["python3", "-m", "pytest", *targets]
    return commands


def print_plan(plan: Plan, explain: bool = True) -> None:
    if explain:
        for reason in plan.reasons:
            print(f"  - {reason}")
    commands = pytest_commands(plan)
    if not commands:
        print("No test-relevant changes detected.")
        return
    print("\nPlan:")
    for pkg, argv in commands.items():
        print(f"  (cd {pkg} && {' '.join(argv)})")


def run_plan(plan: Plan, repo_root: Path) -> int:
    exit_code = 0
    for pkg, argv in pytest_commands(plan).items():
        result = subprocess.run(argv, cwd=repo_root / pkg, check=False)
        exit_code = exit_code or result.returncode
    return exit_code


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".", help="Path inside the monorepo.")
    parser.add_argument("--base", help="Diff this git ref against the working tree instead of HEAD.")
    parser.add_argument("--run", action="store_true", help="Execute the selected pytest commands.")
    parser.add_argument("--quiet", action="store_true", help="Skip the per-file reasoning, print only the plan.")
    args = parser.parse_args(argv[1:])

    repo_root = _find_repo_root(args.path)
    changed = get_changed_files(repo_root, args.base)
    plan = build_plan(changed, repo_root)
    print_plan(plan, explain=not args.quiet)
    if args.run:
        return run_plan(plan, repo_root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
