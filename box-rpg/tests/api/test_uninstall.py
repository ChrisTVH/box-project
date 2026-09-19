"""Tests for descriptor-safe full wipe of launcher-owned roots."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from box.api.uninstall import full_wipe_data
from box.errors import BoxError
from box.paths import AppPaths


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an isolated HOME and point the process at it."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _paths_for(home: Path) -> AppPaths:
    """Build launcher roots inside the isolated HOME."""
    return AppPaths(
        config_root=home / ".config" / "box-rpg",
        cache_root=home / ".cache" / "box-rpg",
    )


def test_missing_dirs_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path, monkeypatch)
    paths = _paths_for(home)

    full_wipe_data(paths)

    assert not paths.cache_root.exists()
    assert not os.path.lexists(paths.cache_root)
    assert not paths.config_root.exists()
    assert home.is_dir()


def test_happy_path_removes_only_launcher_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    paths = _paths_for(home)
    paths.cache_root.mkdir(parents=True)
    paths.config_root.mkdir(parents=True)
    (paths.cache_root / "profiles").mkdir()
    (paths.cache_root / "profiles" / "payload.bin").write_bytes(b"cached")
    (paths.config_root / "config.toml").write_text("key = 1\n", encoding="utf-8")
    keeper = home / "keeper.txt"
    keeper.write_text("keep\n", encoding="utf-8")
    game = tmp_path / "game.sav"
    game.write_bytes(b"save")

    full_wipe_data(paths)

    assert not os.path.lexists(paths.cache_root)
    assert not os.path.lexists(paths.config_root)
    assert keeper.is_file()
    assert game.is_file()
    assert home.is_dir()


def test_symlink_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path, monkeypatch)
    paths = _paths_for(home)
    paths.cache_root.parent.mkdir(parents=True)
    paths.config_root.mkdir(parents=True)
    target = tmp_path / "real"
    target.mkdir()
    (target / "payload.bin").write_bytes(b"payload")
    paths.cache_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(BoxError):
        full_wipe_data(paths)

    assert target.is_dir()
    assert (target / "payload.bin").is_file()
    assert paths.config_root.is_dir()


def test_symlink_ancestor_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path, monkeypatch)
    link_parent = home / ".cache"
    link_parent.mkdir(exist_ok=True)
    real_parent = tmp_path / "real-cache"
    real_parent.mkdir()
    (real_parent / "box-rpg").mkdir()
    # Replace the intermediate .cache directory with a symlink escape.
    link_parent.rmdir()
    link_parent.symlink_to(real_parent, target_is_directory=True)
    paths = _paths_for(home)

    with pytest.raises(BoxError):
        full_wipe_data(paths)

    assert (real_parent / "box-rpg").is_dir()


def test_outside_home_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path, monkeypatch)
    outside = tmp_path / "outside" / "box-rpg"
    outside.mkdir(parents=True)
    (outside / "payload.bin").write_bytes(b"payload")
    paths = AppPaths(
        config_root=home / ".config" / "box-rpg",
        cache_root=outside,
    )

    with pytest.raises(BoxError):
        full_wipe_data(paths)

    assert outside.is_dir()
    assert (outside / "payload.bin").is_file()


def test_unexpected_name_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path, monkeypatch)
    other = home / ".cache" / "other"
    other.mkdir(parents=True)
    paths = AppPaths(config_root=home / ".config" / "box-rpg", cache_root=other)

    with pytest.raises(BoxError):
        full_wipe_data(paths)

    assert other.is_dir()
