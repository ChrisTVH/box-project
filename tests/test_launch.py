from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from box.cli.launch import authorize_game, execute
from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.engines.registry import EngineRegistry
from box.errors import GameValidationError, RuntimeError
from box.games.identity import game_id
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


def test_execute_uses_the_game_root_as_nwjs_working_directory_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), runtime_root, runtime_root / "nw")
    session_root = tmp_path / "session"
    session_root.mkdir()
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    calls: list[tuple[Path | None, tuple[str, ...]]] = []

    @contextmanager
    def session(
        _: AppPaths,
        __: GameInfo,
        copy_root_files: tuple[str, ...] = (),
        *,
        game_descriptor: int | None = None,
    ) -> Generator[object]:
        calls.append((None, copy_root_files))
        yield type(
            "Session",
            (),
            {
                "root": session_root,
                "reference": session_root,
                "game_reference": game_root,
                "profile_root": paths.profiles_root / "0123456789abcdef",
                "process_descriptors": (),
            },
        )()

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    def select(*_: object) -> RuntimeInfo:
        return runtime

    def authorize(
        _: GameInfo,
        __: AppConfig,
        ___: ConfigRepository,
        ____: Callable[[str], str] | None = None,
    ) -> AppConfig:
        return repository.load()

    def run(_: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
        calls.append((cwd, ()))
        return 0

    monkeypatch.setattr("box.cli.launch.detect_game", detect_game)
    monkeypatch.setattr("box.cli.launch.select_runtime", select)
    monkeypatch.setattr("box.cli.launch.authorize_game", authorize)
    monkeypatch.setattr("box.cli.launch.create_session", session)
    monkeypatch.setattr("box.cli.launch.run_process", run)

    assert execute(paths, repository, game_root, None, False, game_cwd=True) == 0
    assert calls == [(None, ()), (game_root, ())]


def test_execute_keeps_the_game_pinned_when_authorization_replaces_its_path(
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
        assert cwd is None
        assert (Path(command[-1]) / "game" / "index.html").read_text(encoding="utf-8") == "original"
        assert (
            f"--user-data-dir={paths.profiles_root / game_id(tmp_path / 'original') / 'user-data'}"
            in command
        )
        return 0

    def detect(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    def select(*_: object) -> RuntimeInfo:
        return runtime

    monkeypatch.setattr("box.cli.launch.detect_game", detect)
    monkeypatch.setattr("box.cli.launch.select_runtime", select)
    monkeypatch.setattr("box.cli.launch.authorize_game", authorize)
    monkeypatch.setattr("box.cli.launch.run_process", run)

    assert execute(paths, repository, game_root, None, False) == 0
