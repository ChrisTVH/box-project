import os
from pathlib import Path

import pytest

from box.engines.registry import EngineRegistry, default_registry
from box.errors import GameValidationError
from box.games.detector import detect_game, ensure_allowed_root
from box.games.files import MAX_GAME_FILE_BYTES, open_game_directory, read_game_file
from box.games.inspector import inspect_game
from box.models import EngineName, GameInfo


def make_game(root: Path) -> None:
    root.mkdir()
    for filename in ("RPG_RT.ini", "RPG_RT.ldb", "RPG_RT.lmt"):
        (root / filename).write_text("GameTitle=Original\n", encoding="ascii")


def test_resolved_game_alias_remains_supported(tmp_path: Path) -> None:
    root = tmp_path / "game"
    make_game(root)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    game = detect_game(alias, default_registry())
    ensure_allowed_root(game, (tmp_path,))
    assert inspect_game(alias).title == "Original"
    assert game.root == root


def test_detected_identity_rejects_directory_replacement(tmp_path: Path) -> None:
    root = tmp_path / "game"
    make_game(root)
    game = detect_game(root, default_registry())
    root.rename(tmp_path / "original")
    make_game(root)
    with pytest.raises(GameValidationError, match="changed since detection"):
        ensure_allowed_root(game, (tmp_path,))


def test_detection_rejects_root_replacement_during_adapter_call(tmp_path: Path) -> None:
    root = tmp_path / "game"
    make_game(root)

    class RacingAdapter:
        name = EngineName.RPG_MAKER_2000_2003

        def detect(self, root: Path) -> GameInfo:
            root.rename(tmp_path / "original")
            make_game(root)
            return GameInfo(self.name, root)

    with pytest.raises(GameValidationError, match="changed since detection"):
        detect_game(root, EngineRegistry((RacingAdapter(),)))


def test_safe_open_rejects_ancestor_replaced_during_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ancestor = tmp_path / "parent"
    ancestor.mkdir()
    root = ancestor / "game"
    make_game(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    make_game(outside / "game")
    real_open = os.open

    def racing_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == "parent":
            ancestor.rename(tmp_path / "old-parent")
            ancestor.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)
    with (
        pytest.raises(GameValidationError, match="symlink"),
        open_game_directory(GameInfo(EngineName.RPG_MAKER_2000_2003, root)),
    ):
        pytest.fail("replacement ancestor was followed")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_inspection_rejects_artifact_replaced_after_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path / "game"
    make_game(root)
    game = detect_game(root, default_registry())
    artifact = root / "RPG_RT.ini"
    artifact.unlink()
    if kind == "symlink":
        outside = tmp_path / "outside.ini"
        outside.write_text("GameTitle=Outside", encoding="ascii")
        artifact.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(artifact)
    else:
        artifact.mkdir()

    def detected(*_: object) -> GameInfo:
        return game

    monkeypatch.setattr("box.games.inspector.detect_game", detected)
    with pytest.raises(GameValidationError):
        inspect_game(root)


def test_relative_reader_rejects_nested_symlink_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "game"
    make_game(root)
    child = root / "js"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "plugins.js").write_text("outside", encoding="ascii")
    with open_game_directory(detect_game(root, default_registry())) as descriptor:
        real_open = os.open

        def racing_open(
            path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> int:
            if path == "js":
                child.rename(root / "old-js")
                child.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", racing_open)
        with pytest.raises(GameValidationError):
            read_game_file(descriptor, Path("js/plugins.js"))


def test_reader_enforces_byte_limit_and_accepts_exact_limit(tmp_path: Path) -> None:
    make_game(tmp_path / "game")
    game = detect_game(tmp_path / "game", default_registry())
    artifact = game.root / "sample"
    artifact.write_bytes(b"abcd")
    with open_game_directory(game) as descriptor:
        assert read_game_file(descriptor, Path("sample"), max_bytes=4) == b"abcd"
        with pytest.raises(GameValidationError, match="byte limit"):
            read_game_file(descriptor, Path("sample"), max_bytes=3)


def test_inspection_limits_ini_size(tmp_path: Path) -> None:
    root = tmp_path / "game"
    make_game(root)
    with (root / "RPG_RT.ini").open("wb") as stream:
        stream.truncate(MAX_GAME_FILE_BYTES + 1)
    with pytest.raises(GameValidationError, match="byte limit"):
        inspect_game(root)


def test_inspection_converts_invalid_utf8_manifest_to_validation_error(tmp_path: Path) -> None:
    root = tmp_path / "game"
    (root / "www" / "js").mkdir(parents=True)
    (root / "www" / "index.html").write_text("", encoding="utf-8")
    (root / "www" / "js" / "plugins.js").write_text("", encoding="utf-8")
    (root / "package.json").write_bytes(b"\xff")
    with pytest.raises(GameValidationError, match="invalid game manifest"):
        inspect_game(root)


def test_inspection_converts_deep_json_recursion_to_validation_error(tmp_path: Path) -> None:
    root = tmp_path / "game"
    (root / "www" / "js").mkdir(parents=True)
    (root / "www" / "index.html").write_text("", encoding="utf-8")
    (root / "www" / "js" / "plugins.js").write_text("", encoding="utf-8")
    depth = 100_000
    (root / "package.json").write_text(
        '{"nested":' + "[" * depth + "0" + "]" * depth + "}", encoding="utf-8"
    )
    with pytest.raises(GameValidationError, match="invalid game manifest") as caught:
        inspect_game(root)
    assert isinstance(caught.value.__cause__, RecursionError)


def test_reader_limits_growth_after_fstat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_game(tmp_path / "game")
    game = detect_game(tmp_path / "game", default_registry())
    artifact = game.root / "sample"
    artifact.write_bytes(b"a")
    with open_game_directory(game) as descriptor:
        real_fstat = os.fstat
        calls = 0

        def growing_fstat(fd: int) -> os.stat_result:
            nonlocal calls
            result = real_fstat(fd)
            calls += 1
            if calls == 2:
                artifact.write_bytes(b"12345")
            return result

        monkeypatch.setattr(os, "fstat", growing_fstat)
        with pytest.raises(GameValidationError, match="byte limit"):
            read_game_file(descriptor, Path("sample"), max_bytes=4)
