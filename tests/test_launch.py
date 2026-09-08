from collections.abc import Callable
from pathlib import Path

import pytest

from box.cli.launch import authorize_game, execute
from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.engines.registry import EngineRegistry
from box.errors import GameValidationError, RuntimeError
from box.models import EngineName, GameInfo, RuntimeInfo, RuntimeSpec
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


def test_authorize_game_rejects_root_moved_outside_during_prompt(tmp_path: Path) -> None:
    game_root = tmp_path / "games" / "sample"
    game_root.mkdir(parents=True)
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)

    def confirm(_: str) -> str:
        game_root.rename(tmp_path / "outside")
        game_root.mkdir()
        return "yes"

    with pytest.raises(GameValidationError, match="changed since detection"):
        authorize_game(game, repository.load(), repository, read=confirm)
    assert repository.load().allowed_game_roots == ()


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_authorization_revalidates_after_persistence_load_before_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    original = AppConfig(preferred_runtime="v0.90.0")
    repository.save(original)
    stored = paths.config_file.read_bytes()
    real_load = repository.load
    confirmed = False

    def confirm(_: str) -> str:
        nonlocal confirmed
        confirmed = True
        return "yes"

    def racing_load() -> AppConfig:
        config = real_load()
        if confirmed:
            game_root.rename(tmp_path / "original")
            if replacement == "symlink":
                game_root.symlink_to(Path("/"), target_is_directory=True)
            else:
                game_root.mkdir()
        return config

    monkeypatch.setattr(repository, "load", racing_load)
    with pytest.raises(GameValidationError):
        authorize_game(game, original, repository, read=confirm)

    assert real_load() == original
    assert paths.config_file.read_bytes() == stored


def test_authorization_escapes_prompt_controls_without_changing_stored_path(tmp_path: Path) -> None:
    game_root = tmp_path / "game\x1b[31m\n\t\x9b\u202e"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    prompts: list[str] = []

    def confirm(prompt: str) -> str:
        prompts.append(prompt)
        return "yes"

    config = authorize_game(game, repository.load(), repository, read=confirm)

    assert len(prompts) == 1
    assert r"game\x1b[31m\x0a\x09\x9b\u202e" in prompts[0]
    assert all(control not in prompts[0] for control in ("\x1b", "\n", "\t", "\x9b", "\u202e"))
    assert config.allowed_game_roots == (game_root,)
    assert repository.load().allowed_game_roots == (game_root,)


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


def test_authorize_game_prunes_a_deleted_root_before_adding_a_new_game(tmp_path: Path) -> None:
    deleted_root = tmp_path / "games" / "deleted"
    game_root = tmp_path / "games" / "new"
    deleted_root.mkdir(parents=True)
    game_root.mkdir()
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(deleted_root)
    deleted_root.rmdir()

    config = authorize_game(game, repository.load(), repository, read=lambda _: "yes")

    assert config.allowed_game_roots == (game_root,)
    assert repository.load() == config


def test_authorize_game_prunes_deleted_roots_before_validating_an_allowed_game(
    tmp_path: Path,
) -> None:
    deleted_root = tmp_path / "games" / "deleted"
    game_root = tmp_path / "games" / "allowed"
    deleted_root.mkdir(parents=True)
    game_root.mkdir()
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.save(AppConfig(allowed_game_roots=(deleted_root, game_root)))
    deleted_root.rmdir()

    config = authorize_game(game, repository.load(), repository)

    assert config.allowed_game_roots == (game_root,)
    assert repository.load() == config


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

    with pytest.raises(RuntimeError, match=r"box-rpg runtime nwjs available --interactive"):
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

    def run(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
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
    assert calls[0][0][:2] == [str(executable), "--project-path"]
    assert calls[0][0][3] == "--fullscreen"
    assert calls[0][1] == Path(calls[0][0][2])


def test_execute_rejects_nwjs_options_for_rpg_rt_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    monkeypatch.setattr("box.cli.launch.detect_game", detect_game)

    with pytest.raises(GameValidationError, match="--copy-root-file"):
        execute(paths, repository, game_root, None, False, copy_root_files=("messages.csv",))


def test_execute_rejects_relocation_during_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    (game_root / "index.html").write_text("original", encoding="utf-8")
    (game_root / "package.json").write_text('{"name": "Original"}', encoding="utf-8")
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), runtime_root, runtime_root / "nw")
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)

    def authorize(
        _: GameInfo,
        __: AppConfig,
        ___: ConfigRepository,
        ____: Callable[[str], str] | None = None,
    ) -> AppConfig:
        game_root.rename(tmp_path / "original")
        game_root.mkdir()
        (game_root / "index.html").write_text("replacement", encoding="utf-8")
        return repository.load()

    def run(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
        pytest.fail("a relocated game must not launch")

    def detect(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    def select(*_: object) -> RuntimeInfo:
        return runtime

    monkeypatch.setattr("box.cli.launch.detect_game", detect)
    monkeypatch.setattr("box.cli.launch.select_runtime", select)
    monkeypatch.setattr("box.cli.launch.authorize_game", authorize)
    monkeypatch.setattr("box.cli.launch.run_process", run)

    with pytest.raises(GameValidationError, match="changed since detection"):
        execute(paths, repository, game_root, None, False)
