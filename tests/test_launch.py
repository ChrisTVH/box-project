from pathlib import Path

import pytest

from box.cli.launch import authorize_game, execute
from box.config.repository import ConfigRepository
from box.engines.registry import EngineRegistry
from box.errors import GameValidationError, RuntimeError
from box.models import EngineName, GameInfo
from box.paths import AppPaths


def test_authorize_game_registers_the_detected_game_root_when_configuration_is_empty(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "games" / "sample"
    game_root.mkdir(parents=True)
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    config = authorize_game(game, repository.load(), repository)

    assert config.allowed_game_roots == (game_root,)
    assert repository.load() == config


def test_authorize_game_requires_existing_configuration_to_allow_the_game(tmp_path: Path) -> None:
    game_root = tmp_path / "games" / "sample"
    other_root = tmp_path / "games" / "other"
    game_root.mkdir(parents=True)
    other_root.mkdir()
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(other_root)

    with pytest.raises(GameValidationError, match="outside configured roots"):
        authorize_game(game, repository.load(), repository)


def test_authorize_game_rejects_a_registered_root_replaced_by_a_symlink(tmp_path: Path) -> None:
    game_root = tmp_path / "games" / "sample"
    game_root.mkdir(parents=True)
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    authorize_game(game, repository.load(), repository)
    outside = tmp_path / "outside"
    outside.mkdir()
    game_root.rmdir()
    game_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(GameValidationError, match="contains a symlink"):
        authorize_game(game, repository.load(), repository)


def test_execute_does_not_register_a_game_when_no_runtime_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "games" / "sample"
    game_root.mkdir(parents=True)
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    monkeypatch.setattr("box.cli.launch.detect_game", detect_game)

    with pytest.raises(RuntimeError, match=r"no matching NW\.js runtime"):
        execute(paths, repository, game_root, None, False)

    assert repository.load().allowed_game_roots == ()
