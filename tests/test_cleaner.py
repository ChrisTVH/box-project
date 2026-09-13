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
    other_dir = repository / "unrelated" / "__pycache__"
    bytecode(other_dir / "other.pyc")
    targets = cleaner.collect_targets(repository)
    assert targets["caches"] == [owned, other_dir]
    assert cleaner.remove(targets)
    assert not owned.exists() and not other_dir.exists()
    assert (repository / "src" / "__pycache__").exists()
    assert all(path.exists() for path in [foreign, invalid, nested])


def test_pycache_dirs_with_only_bytecode_are_removed(repository: Path) -> None:
    pure = repository / "pkg" / "__pycache__"
    bytecode(pure / "first.pyc")
    bytecode(pure / "second.pyc")
    empty = repository / "empty" / "__pycache__"
    empty.mkdir(parents=True)
    targets = cleaner.collect_targets(repository)
    assert pure in targets["caches"]
    assert empty in targets["caches"]
    assert cleaner.remove(targets)
    assert not pure.exists()
    assert not empty.exists()


def test_pycache_dir_with_subdirectory_keeps_directory(repository: Path) -> None:
    cache = repository / "pkg" / "__pycache__"
    owned = bytecode(cache / "owned.pyc")
    (cache / "nested").mkdir(parents=True)
    (cache / "nested" / "keep.txt").write_text("keep")
    targets = cleaner.collect_targets(repository)
    assert targets["caches"] == [owned]
    assert cleaner.remove(targets)
    assert not owned.exists()
    assert cache.exists()


def test_nonempty_owned_trees_are_removed_recursively(repository: Path) -> None:
    build_file = repository / "build" / "output" / "result.bin"
    build_file.parent.mkdir(parents=True)
    build_file.write_bytes(b"generated")
    venv_file = repository / ".venv" / "lib" / "package.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_bytes(b"generated")
    cache_file = repository / "sub" / ".pytest_cache" / "v" / "cache.bin"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"generated")
    egg_file = repository / "box_rpg.egg-info" / "PKG-INFO"
    egg_file.parent.mkdir(parents=True)
    egg_file.write_text("generated")
    nested = repository / "vendor" / "build" / "output.bin"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"keep")
    (repository / "vendor" / "pyproject.toml").touch()
    targets = cleaner.collect_targets(repository)
    assert repository / "build" in targets["build"]
    assert repository / ".venv" in targets["venvs"]
    assert repository / "sub" / ".pytest_cache" in targets["caches"]
    assert repository / "box_rpg.egg-info" in targets["build"]
    assert cleaner.remove(targets)
    assert not (repository / "build").exists()
    assert not (repository / ".venv").exists()
    assert nested.exists()


def test_symlinks_inside_owned_trees_never_reach_outside(repository: Path, tmp_path: Path) -> None:
    victim = tmp_path / "outside.txt"
    victim.write_text("keep")
    link_dir = repository / "build"
    link_dir.mkdir()
    (link_dir / "evil").symlink_to(victim)
    (link_dir / "real").write_text("generated")
    targets = cleaner.collect_targets(repository)
    assert cleaner.remove(targets)
    assert not link_dir.exists()
    assert victim.read_text() == "keep"


def test_git_dir_is_never_entered(repository: Path) -> None:
    planted = bytecode(repository / ".git" / "objects" / "__pycache__" / "planted.pyc")
    targets = cleaner.collect_targets(repository)
    assert not any(targets.values())
    assert planted.exists()


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


def test_foreign_file_added_during_tree_removal_aborts(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = repository / "build"
    (directory / "old").mkdir(parents=True)
    (directory / "old" / "stale.txt").write_text("stale")
    targets = cleaner.collect_targets(repository)
    original_unlink = os.unlink

    def raced_unlink(path: str, *, dir_fd: int | None = None) -> None:
        original_unlink(path, dir_fd=dir_fd)
        (directory / "foreign").write_text("keep")

    monkeypatch.setattr(cleaner.os, "unlink", raced_unlink)
    assert not cleaner.remove(targets)
    assert directory.exists()
    assert (directory / "foreign").read_text() == "keep"


def test_remove_tree_rejects_excessive_depth(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cleaner, "_MAX_TREE_DEPTH", -1)
    directory = repository / "build"
    directory.mkdir()
    descriptor = os.open(repository, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError, match="too deep"):
            cleaner._remove_tree(descriptor, "build")
    finally:
        os.close(descriptor)
    assert directory.exists()


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
    # Keep the directory itself ineligible so the file-level removal path is exercised.
    (repository / "src" / "__pycache__" / "notes.txt").write_text("keep")
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
