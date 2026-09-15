import os
from pathlib import Path

import pytest

from box.errors import LaunchError
from box.launch.links import copy_game_root_file, list_root_files, open_game_root


def test_open_game_root_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "game").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(LaunchError, match="unsafe"):
        open_game_root(alias / "game")


def test_copy_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    game = tmp_path / "game"
    session = tmp_path / "session"
    game.mkdir()
    session.mkdir()
    os.mkfifo(game / "messages.csv")
    with pytest.raises(LaunchError, match="unsafe"):
        copy_game_root_file(session, game, "messages.csv")
    assert not (session / "messages.csv").exists()


def test_copy_enforces_limit_and_removes_partial_after_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = tmp_path / "game"
    session = tmp_path / "session"
    game.mkdir()
    session.mkdir()
    artifact = game / "messages.csv"
    artifact.write_bytes(b"a")
    monkeypatch.setattr("box.launch.links.MAX_GAME_FILE_BYTES", 64 * 1024)
    real_fstat = os.fstat

    def growing_fstat(descriptor: int) -> os.stat_result:
        result = real_fstat(descriptor)
        artifact.write_bytes(b"a" * (64 * 1024 + 1))
        return result

    monkeypatch.setattr(os, "fstat", growing_fstat)
    with pytest.raises(LaunchError, match="byte limit"):
        copy_game_root_file(session, game, "messages.csv")
    assert list(session.iterdir()) == []


def test_copy_accepts_exact_limit_and_rejects_larger_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = tmp_path / "game"
    session = tmp_path / "session"
    game.mkdir()
    session.mkdir()
    monkeypatch.setattr("box.launch.links.MAX_GAME_FILE_BYTES", 4)
    (game / "exact").write_bytes(b"1234")
    (game / "large").write_bytes(b"12345")
    assert copy_game_root_file(session, game, "exact").read_bytes() == b"1234"
    with pytest.raises(LaunchError, match="byte limit"):
        copy_game_root_file(session, game, "large")
    assert not (session / "large").exists()


def test_list_root_files_filters_mixed_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "b.py").write_bytes(b"bb")
    (game / "a.txt").write_bytes(b"a")
    (game / "subdir").mkdir()
    (game / "link.txt").symlink_to(game / "a.txt")
    (game / "package.json").write_bytes(b"{}")
    (game / "game").write_bytes(b"x")
    (game / "large.bin").write_bytes(b"12345")
    (game / "exact.bin").write_bytes(b"1234")
    os.mkfifo(game / "pipe")
    monkeypatch.setattr("box.launch.links.MAX_GAME_FILE_BYTES", 4)
    assert list_root_files(game) == ("a.txt", "b.py", "exact.bin")


def test_list_root_files_empty_root_returns_empty(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    assert list_root_files(game) == ()


def test_list_root_files_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(LaunchError, match="unsafe"):
        list_root_files(tmp_path / "missing")


def test_list_root_files_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "game").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(LaunchError, match="unsafe"):
        list_root_files(alias / "game")
