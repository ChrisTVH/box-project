from collections.abc import Callable
from pathlib import Path

import pytest

from box.cli.launch import authorize_game, execute
from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.engines.registry import EngineRegistry
from box.errors import GameValidationError, RuntimeError
from box.models import EngineName, GameInfo
from box.paths import AppPaths
from box.runtime.easyrpg import EasyRPGRuntime


def test_authorize_game_registers_the_detected_game_root_after_confirmation(
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
    config = authorize_game(game, repository.load(), repository, read=lambda _: "yes")

    assert config.allowed_game_roots == (game_root,)
    assert repository.load() == config


def test_authorize_game_requires_confirmation_for_an_unregistered_game(tmp_path: Path) -> None:
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

    with pytest.raises(GameValidationError, match="was not authorized"):
        authorize_game(game, repository.load(), repository, read=lambda _: "no")

    assert repository.load().allowed_game_roots == (other_root,)


def test_authorize_game_rejects_unregistered_games_without_an_interactive_reader(
    tmp_path: Path,
) -> None:
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
    authorize_game(game, repository.load(), repository, read=lambda _: "yes")
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


def test_execute_launches_rpg_rt_projects_with_easyrpg_fullscreen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    executable = runtime_root / "easyrpg-player"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    calls: list[tuple[list[str], Path | None]] = []

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    def latest(_: object) -> EasyRPGRuntime:
        return EasyRPGRuntime("0.8.1.1", runtime_root)

    def run(command: list[str], cwd: Path | None = None) -> int:
        calls.append((command, cwd))
        return 0

    monkeypatch.setattr("box.cli.launch.detect_game", detect_game)
    monkeypatch.setattr("box.cli.launch.EasyRPGCatalog.latest", latest)

    def authorize(
        _: GameInfo,
        __: AppConfig,
        ___: ConfigRepository,
        ____: Callable[[str], str] | None = None,
    ) -> AppConfig:
        return repository.load()

    monkeypatch.setattr("box.cli.launch.authorize_game", authorize)
    monkeypatch.setattr("box.cli.launch.run_process", run)

    assert execute(paths, repository, game_root, None, False) == 0
    assert calls == [
        ([str(executable), "--project-path", str(game_root), "--fullscreen"], game_root)
    ]
