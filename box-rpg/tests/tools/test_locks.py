"""Check lock structure and closure against the installed locked toolchain."""

import re
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def _find_project_root(start: Path) -> Path:
    """Walk up from the test file until the box-rpg checkout root is found."""
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "res/requirements").is_dir():
            return candidate
    raise AssertionError(f"box-rpg project root not found above {start}")


ROOT = _find_project_root(Path(__file__).resolve().parent)
LOCKS = ROOT / "res/requirements"


def read_lock(name: str) -> dict[str, str]:
    content = (LOCKS / f"{name}.txt").read_text(encoding="utf-8")
    entries = content.replace("\\\n", "").splitlines()
    result: dict[str, str] = {}
    for entry in entries:
        match = re.fullmatch(
            r"([a-z0-9-]+)==([0-9][a-z0-9.]*)"
            r"(?:\s+--hash=sha256:[0-9a-f]{64})+",
            entry,
        )
        assert match is not None, f"Unpinned, unhashed or unsupported requirement: {entry}"
        package, version = match.groups()
        assert package not in result
        result[package] = version
    assert result
    return result


@pytest.mark.parametrize("name", ["bootstrap", "build", "dev"])
def test_locks_have_exact_versions_and_sha256(name: str) -> None:
    read_lock(name)


def test_locks_match_project_inputs_and_keep_runtime_empty() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == []
    build = read_lock("build")
    assert project["build-system"]["requires"] == [f"setuptools=={build['setuptools']}"]
    for requirement in project["build-system"]["requires"]:
        assert requirement in project["dependency-groups"]["build"]
    for group, requirements in (
        (build, project["dependency-groups"]["build"]),
        (read_lock("dev"), project["project"]["optional-dependencies"]["dev"]),
        (read_lock("bootstrap"), (LOCKS / "bootstrap.in").read_text().splitlines()),
    ):
        for text in requirements:
            requirement = Requirement(text)
            assert group[canonicalize_name(requirement.name)] in requirement.specifier
    assert not set(build) & set(read_lock("dev"))


@pytest.mark.parametrize("name", ["build", "dev"])
def test_installed_locks_include_all_active_transitive_dependencies(name: str) -> None:
    locked = read_lock(name)
    for package, version in locked.items():
        distribution = metadata.distribution(package)
        assert distribution.version == version, "Install the hashed locks before testing"
        for text in distribution.requires or []:
            requirement = Requirement(text)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            dependency = canonicalize_name(requirement.name)
            assert dependency in locked, f"Missing {package} dependency: {text}"
            assert locked[dependency] in requirement.specifier
