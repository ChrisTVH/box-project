import importlib.util
import os
import subprocess
from pathlib import Path

import cleaner
import pytest

# These tests deliberately exercise the standalone script's safety helpers.
# pyright: reportPrivateUsage=false


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(cleaner, "REPO_ROOT", root)

    def no_protected(_: Path) -> set[Path]:
        return set()

    monkeypatch.setattr(cleaner, "_protected", no_protected)
    return root


def bytecode(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(importlib.util.MAGIC_NUMBER + b"\0" * 12 + b"generated")
    return path


def test_cache_policy_preserves_foreign_and_nested_projects(repository: Path) -> None:
    owned = bytecode(repository / "src" / "__pycache__" / "owned.pyc")
    foreign = repository / "src" / "__pycache__" / "notes.txt"
    foreign.write_text("keep")
    invalid = foreign.with_suffix(".pyc")
    invalid.write_text("not bytecode")
    nested = bytecode(repository / "src" / "vendor" / "__pycache__" / "nested.pyc")
    (repository / "src" / "vendor" / "pyproject.toml").touch()
    other = bytecode(repository / "unrelated" / "__pycache__" / "other.pyc")
    targets = cleaner.collect_targets(repository)
    assert targets["caches"] == [owned]
    assert cleaner.remove(targets)
    assert not owned.exists()
    assert all(path.exists() for path in [foreign, invalid, nested, other])


def test_only_empty_root_outputs_are_owned(repository: Path) -> None:
    (repository / "build").mkdir()
    (repository / "venv").mkdir()
    nested = repository / "src" / "build"
    nested.mkdir(parents=True)
    foreign = repository / "dist" / "release.zip"
    foreign.parent.mkdir()
    foreign.write_bytes(b"keep")
    targets = cleaner.collect_targets(repository)
    assert targets["build"] == [repository / "build"]
    assert targets["venvs"] == [repository / "venv"]
    assert cleaner.remove(targets)
    assert nested.exists() and foreign.exists()


def test_tracked_files_and_containers_are_protected(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracked = bytecode(repository / "src" / "__pycache__" / "tracked.pyc")
    (repository / "build").mkdir()

    def protected(_: Path) -> set[Path]:
        return {tracked.relative_to(repository), Path("build/deleted-tracked-file")}

    monkeypatch.setattr(cleaner, "_protected", protected)
    assert not any(cleaner.collect_targets(repository).values())
    assert not cleaner.remove({"caches": [tracked]})
    assert tracked.exists()


def test_git_inventory_includes_tracked_and_nonignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def inventory(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert "--cached" in args and "--others" in args and "--exclude-standard" in args
        return subprocess.CompletedProcess(args, 0, stdout=b"tracked\0untracked\0")

    monkeypatch.setattr(cleaner.subprocess, "run", inventory)
    assert cleaner._protected(tmp_path) == {Path("tracked"), Path("untracked")}


def test_git_failure_fails_closed(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owned = bytecode(repository / "__pycache__" / "owned.pyc")

    def unavailable(_: Path) -> set[Path]:
        raise FileNotFoundError("git")

    monkeypatch.setattr(cleaner, "_protected", unavailable)
    assert not any(cleaner.collect_targets(repository).values())
    assert not cleaner.remove({"caches": [owned]})
    assert owned.exists()


@pytest.mark.parametrize("ancestor", [False, True])
def test_symlink_replacement_after_collection(
    repository: Path,
    tmp_path: Path,
    ancestor: bool,
) -> None:
    owned = bytecode(repository / "src" / "__pycache__" / "owned.pyc")
    targets = cleaner.collect_targets(repository)
    victim = bytecode(tmp_path / "outside" / "__pycache__" / "owned.pyc")
    if ancestor:
        (repository / "src").rename(repository / "saved")
        (repository / "src").symlink_to(tmp_path / "outside", target_is_directory=True)
    else:
        owned.rename(owned.with_suffix(".saved"))
        owned.symlink_to(victim)
    assert not cleaner.remove(targets)
    assert victim.exists()


def test_new_foreign_file_prevents_directory_removal(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = repository / "build"
    directory.mkdir()
    targets = cleaner.collect_targets(repository)
    original = os.rmdir

    def raced_rmdir(path: str, *, dir_fd: int | None = None) -> None:
        (directory / "foreign").write_text("keep")
        original(path, dir_fd=dir_fd)

    monkeypatch.setattr(cleaner.os, "rmdir", raced_rmdir)
    assert not cleaner.remove(targets)
    assert (directory / "foreign").read_text() == "keep"


def test_traversal_and_root_rejected(repository: Path, tmp_path: Path) -> None:
    victim = bytecode(tmp_path / "__pycache__" / "outside.pyc")
    assert not cleaner.remove(
        {"caches": [repository, repository / ".." / victim.relative_to(tmp_path)]}
    )
    assert victim.exists()


def test_real_git_index_and_ignore_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    tracked = bytecode(root / "src" / "__pycache__" / "tracked.pyc")
    subprocess.run(["git", "-C", str(root), "add", str(tracked)], check=True)
    (root / ".gitignore").write_text("*.pyc\n")
    owned = bytecode(tracked.with_name("owned.pyc"))
    foreign = tracked.with_name("notes.txt")
    foreign.write_text("keep")
    monkeypatch.setattr(cleaner, "REPO_ROOT", root)
    targets = cleaner.collect_targets(root)
    assert targets["caches"] == [owned]
    # A file staged after discovery must also be protected at deletion time.
    subprocess.run(["git", "-C", str(root), "add", "-f", str(owned)], check=True)
    assert not cleaner.remove(targets)
    assert tracked.exists() and owned.exists() and foreign.exists()


def test_parent_swap_at_deletion_uses_open_descriptor(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = bytecode(repository / "src" / "__pycache__" / "owned.pyc")
    targets = cleaner.collect_targets(repository)
    victim = bytecode(tmp_path / "outside" / "owned.pyc")
    original = os.rename
    saved = owned.parent.with_name("saved")

    def raced_rename(src: str, dst: str, **kwargs: object) -> None:
        original(owned.parent, saved)
        owned.parent.symlink_to(victim.parent, target_is_directory=True)
        original(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cleaner.os, "rename", raced_rename)
    assert cleaner.remove(targets)
    assert victim.exists()
    assert not (saved / owned.name).exists()
