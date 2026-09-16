"""Non-interactive launch orchestration tests without subprocess or bwrap."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from types import TracebackType

import pytest

from box.api.launch import Interaction, authorize_game, launch, list_root_files
from box.config.repository import ConfigRepository
from box.engines.registry import EngineRegistry
from box.errors import GameValidationError
from box.launch.supervisor import LaunchedSession
from box.models import EngineName, GameInfo, RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime.easyrpg import EasyRPGRuntime
from box.runtime.platform import current_architecture


class FakeInteraction:
    """Record GUI callbacks with scripted answers."""

    def __init__(
        self,
        *,
        confirm_x11: bool = True,
        confirm_add_root: bool = True,
        choose_runtime: int | None = 0,
    ) -> None:
        self._confirm_x11 = confirm_x11
        self._confirm_add_root = confirm_add_root
        self._choose_runtime = choose_runtime
        self.x11_calls: list[str] = []
        self.add_root_calls: list[Path] = []
        self.runtime_calls: list[tuple[str, tuple[str, ...], str]] = []

    def confirm_x11(self, display: str) -> bool:
        self.x11_calls.append(display)
        return self._confirm_x11

    def confirm_add_root(self, path: Path) -> bool:
        self.add_root_calls.append(path)
        return self._confirm_add_root

    def choose_runtime(self, kind: str, candidates: tuple[str, ...], title: str) -> int | None:
        self.runtime_calls.append((kind, candidates, title))
        return self._choose_runtime


class FakeSandbox:
    """Record sandbox policy calls without touching Bubblewrap."""

    def __init__(self, *, allow_network: bool = False, allow_game_writes: bool = False) -> None:
        self.allow_network = allow_network
        self.allow_game_writes = allow_game_writes
        self.calls: list[str] = []
        self.runtime_paths: list[Path] = []
        self.binds: list[tuple[str, bool]] = []
        self.probe = "wayland"
        self.last_cwd = "/"
        self.last_arguments: list[str] = []
        self._kept: list[int] = []

    def __enter__(self) -> FakeSandbox:
        self.calls.append("enter")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.calls.append("exit")
        for descriptor in self._kept:
            with suppress(OSError):
                os.close(descriptor)
        self._kept.clear()

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return tuple(self._kept)

    def keep(self, descriptor: int) -> int:
        self._kept.append(descriptor)
        return descriptor

    def runtime(self, executable: Path) -> str:
        self.calls.append("runtime")
        self.runtime_paths.append(executable)
        return "/runtime/fake"

    def display_probe(self) -> str:
        return self.probe

    def desktop(self) -> None:
        self.calls.append("desktop")

    def x11(self) -> None:
        self.calls.append("x11")

    def devices(self) -> None:
        self.calls.append("devices")

    def audio(self) -> None:
        self.calls.append("audio")

    def persistence(self, paths: AppPaths, game: GameInfo) -> None:
        self.calls.append("persistence")

    def game_saves(self, game: GameInfo, descriptor: int) -> int:
        self.calls.append("game_saves")
        return descriptor

    def game_writable(self, descriptor: int) -> None:
        self.calls.append("game_writable")
        self._kept.append(descriptor)

    def nw_game(self, game: GameInfo, descriptor: int, saves: int) -> None:
        self.calls.append("nw_game")

    def bind(self, descriptor: int, destination: str, *, writable: bool = False) -> None:
        self.calls.append(f"bind:{destination}")
        self.binds.append((destination, writable))

    def command(self, arguments: list[str], *, cwd: str = "/") -> list[str]:
        self.calls.append(f"command:{cwd}")
        self.last_cwd = cwd
        self.last_arguments = list(arguments)
        return ["fake-bwrap", *arguments]


class FakeSession:
    """Minimal isolated session double holding a real directory descriptor."""

    def __init__(self, session_descriptor: int, root: Path) -> None:
        self.session_descriptor = session_descriptor
        self.parent_descriptor = os.dup(session_descriptor)
        self.root = root
        self.name = "testsession"
        self.detached = False

    @property
    def identifier(self) -> str:
        return "testidentifier"

    def detach(self) -> None:
        self.detached = True

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        with suppress(OSError):
            os.close(self.session_descriptor)
        with suppress(OSError):
            os.close(self.parent_descriptor)


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


def _nwjs_game(tmp_path: Path) -> GameInfo:
    root = tmp_path / "game"
    root.mkdir()
    entry = root / "index.html"
    entry.write_text("fixture", encoding="utf-8")
    manifest = root / "package.json"
    manifest.write_text('{"name": "fixture"}', encoding="utf-8")
    return GameInfo(EngineName.RPG_MAKER_MZ, root, entry, manifest)


def _easyrpg_game(tmp_path: Path) -> GameInfo:
    root = tmp_path / "game"
    root.mkdir()
    return GameInfo(EngineName.RPG_MAKER_2000_2003, root)


def _install_nwjs(tmp_path: Path, version: str) -> Path:
    arch = current_architecture()
    binary = (
        tmp_path
        / "cache"
        / "runtimes"
        / "nwjs"
        / f"linux-{arch}"
        / f"standard-{version.lower()}"
        / "nw"
    )
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)
    return binary


def _install_easyrpg(tmp_path: Path, version: str) -> Path:
    binary = tmp_path / "cache" / "runtimes" / "easyrpg" / version / "easyrpg-player"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)
    return binary


def _patch_sandbox(monkeypatch: pytest.MonkeyPatch, *, probe: str = "wayland") -> list[FakeSandbox]:
    sandboxes: list[FakeSandbox] = []

    def factory(*, allow_network: bool = False, allow_game_writes: bool = False) -> FakeSandbox:
        sandbox = FakeSandbox(allow_network=allow_network, allow_game_writes=allow_game_writes)
        sandbox.probe = probe
        sandboxes.append(sandbox)
        return sandbox

    monkeypatch.setattr("box.api.launch.Sandbox", factory)
    return sandboxes


def _patch_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[FakeSession]:
    sessions: list[FakeSession] = []

    def factory(
        paths: AppPaths,
        game: GameInfo,
        copy_root_files: tuple[str, ...] = (),
        *,
        game_descriptor: int | None = None,
    ) -> FakeSession:
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        session = FakeSession(descriptor, tmp_path / "session")
        sessions.append(session)
        return session

    monkeypatch.setattr("box.api.launch.create_session", factory)
    return sessions


def _patch_spawn(
    monkeypatch: pytest.MonkeyPatch,
    capture: list[list[str]] | None = None,
) -> list[tuple[list[str], tuple[int, ...]]]:
    """Mock detached spawn; capture commands and return a fake handle."""
    seen: list[tuple[list[str], tuple[int, ...]]] = []

    def fake_spawn(
        paths: AppPaths,
        identifier: str,
        name: str,
        command: list[str],
        pass_fds: tuple[int, ...] = (),
        *,
        parent_descriptor: int,
        session_descriptor: int,
        use_gamemode: bool = False,
        gamemode_proxy: Path | None = None,
    ) -> LaunchedSession:
        seen.append((list(command), tuple(pass_fds)))
        if capture is not None:
            capture.append(list(command))
        return LaunchedSession(
            identifier=identifier, name=name, root=paths.sessions_root / identifier / name
        )

    monkeypatch.setattr("box.api.launch.spawn_detached", fake_spawn)
    # EasyRPG branch creates its session via create_supervisor_session; keep fds valid.
    return seen


def _patch_spawn_forbidden(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    """Fail any detached spawn attempt with a test assertion."""

    def forbidden_spawn(
        paths: AppPaths,
        identifier: str,
        name: str,
        command: list[str],
        pass_fds: tuple[int, ...] = (),
        *,
        parent_descriptor: int,
        session_descriptor: int,
        use_gamemode: bool = False,
        gamemode_proxy: Path | None = None,
    ) -> LaunchedSession:
        raise AssertionError(message)

    monkeypatch.setattr("box.api.launch.spawn_detached", forbidden_spawn)


def test_nwjs_launch_returns_handle_and_forwards_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    binary = _install_nwjs(tmp_path, "v0.90.0")
    interaction = FakeInteraction()
    sandboxes = _patch_sandbox(monkeypatch, probe="wayland")
    _patch_session(monkeypatch, tmp_path)
    commands: list[list[str]] = []

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn(monkeypatch, capture=commands)
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(
        paths,
        repository,
        game.root,
        "v0.90.0",
        False,
        allow_network=True,
        allow_game_writes=True,
        interaction=interaction,
    )
    assert handle.identifier
    assert handle.name
    assert len(sandboxes) == 1
    assert sandboxes[0].allow_network is True
    assert sandboxes[0].allow_game_writes is True
    assert "devices" in sandboxes[0].calls
    assert "audio" in sandboxes[0].calls
    assert sandboxes[0].runtime_paths == [binary]
    assert interaction.runtime_calls == []
    assert len(commands) == 1
    assert commands[0][0] == "fake-bwrap"
    assert "--ozone-platform=wayland" in sandboxes[0].last_arguments
    assert sandboxes[0].last_cwd == "/session/game"


def test_easyrpg_launch_uses_fullscreen_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _easyrpg_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_easyrpg(tmp_path, "0.8.1.1")
    interaction = FakeInteraction()
    sandboxes = _patch_sandbox(monkeypatch, probe="wayland")

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn(monkeypatch)
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    assert launch(
        paths, repository, game.root, "0.8.1.1", False, interaction=interaction
    ).identifier
    assert len(sandboxes) == 1
    assert sandboxes[0].last_cwd == "/game"
    assert sandboxes[0].last_arguments[-6:] == [
        "/runtime/fake",
        "--project-path",
        "/game",
        "--fullscreen",
        "--save-path",
        "/game/save",
    ]


def test_easyrpg_rejects_sdk_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = _easyrpg_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn_forbidden(monkeypatch, "rejected launch must not run")
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)

    with pytest.raises(GameValidationError, match=r"only available for NW\.js"):
        launch(paths, repository, game.root, None, True, interaction=FakeInteraction())


def test_x11_denial_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_nwjs(tmp_path, "v0.90.0")
    interaction = FakeInteraction(confirm_x11=False)
    _patch_sandbox(monkeypatch, probe="x11")
    _patch_session(monkeypatch, tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn_forbidden(monkeypatch, "denied X11 must not run")
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.setenv("DISPLAY", ":0")

    with pytest.raises(GameValidationError, match="not confirmed"):
        launch(paths, repository, game.root, "v0.90.0", False, interaction=interaction)
    assert len(interaction.x11_calls) == 1
    assert interaction.x11_calls[0] == ":0"


def test_x11_non_interactive_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_nwjs(tmp_path, "v0.90.0")
    _patch_sandbox(monkeypatch, probe="x11")
    _patch_session(monkeypatch, tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.setenv("DISPLAY", ":0")

    with pytest.raises(GameValidationError, match="consent"):
        launch(paths, repository, game.root, "v0.90.0", False, interaction=None)


def test_add_root_confirm_stores_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_nwjs(tmp_path, "v0.90.0")
    interaction = FakeInteraction(confirm_add_root=True)
    _patch_sandbox(monkeypatch, probe="wayland")
    _patch_session(monkeypatch, tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn(monkeypatch)
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    assert launch(
        paths, repository, game.root, "v0.90.0", False, interaction=interaction
    ).identifier
    assert interaction.add_root_calls == [game.root]
    assert repository.load().allowed_game_roots == (game.root,)


def test_add_root_denied_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_nwjs(tmp_path, "v0.90.0")
    interaction = FakeInteraction(confirm_add_root=False)
    _patch_sandbox(monkeypatch, probe="wayland")
    _patch_session(monkeypatch, tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn_forbidden(monkeypatch, "denied root must not run")
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    with pytest.raises(GameValidationError, match="not authorized"):
        launch(paths, repository, game.root, "v0.90.0", False, interaction=interaction)
    assert repository.load().allowed_game_roots == ()


def test_authorize_game_confirm_flow(tmp_path: Path) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    allowed = authorize_game(game, repository.load(), repository, FakeInteraction())
    assert allowed.allowed_game_roots == (game.root,)

    class _DenyPrompt(FakeInteraction):
        def confirm_add_root(self, path: Path) -> bool:
            raise AssertionError("already allowed game must not prompt")

    again = authorize_game(game, repository.load(), repository, _DenyPrompt(confirm_add_root=False))
    assert again.allowed_game_roots == (game.root,)


def test_authorize_game_denied_keeps_existing_roots(tmp_path: Path) -> None:
    game = _nwjs_game(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(other)
    with pytest.raises(GameValidationError, match="not authorized"):
        authorize_game(game, repository.load(), repository, FakeInteraction(confirm_add_root=False))
    assert repository.load().allowed_game_roots == (other,)


def test_nwjs_runtime_choice_selects_requested_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    first = _install_nwjs(tmp_path, "v0.115.0")
    second = _install_nwjs(tmp_path, "v0.112.0")
    interaction = FakeInteraction(choose_runtime=1)
    sandboxes = _patch_sandbox(monkeypatch, probe="wayland")
    _patch_session(monkeypatch, tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn(monkeypatch)
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    assert launch(paths, repository, game.root, None, False, interaction=interaction).identifier
    assert len(interaction.runtime_calls) == 1
    kind, candidates, _title = interaction.runtime_calls[0]
    assert kind == "nwjs"
    assert candidates == ("v0.115.0", "v0.112.0")
    assert sandboxes[0].runtime_paths == [second]
    assert first != second


def test_easyrpg_runtime_choice_selects_requested_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _easyrpg_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_easyrpg(tmp_path, "0.8.1.1")
    second_binary = _install_easyrpg(tmp_path, "0.8")
    interaction = FakeInteraction(choose_runtime=1)
    sandboxes = _patch_sandbox(monkeypatch, probe="wayland")

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn(monkeypatch)
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    assert launch(paths, repository, game.root, None, False, interaction=interaction).identifier
    assert len(interaction.runtime_calls) == 1
    kind, candidates, _title = interaction.runtime_calls[0]
    assert kind == "easyrpg"
    assert candidates == ("0.8.1.1", "0.8")
    assert sandboxes[0].runtime_paths == [second_binary]


def test_runtime_cancel_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_nwjs(tmp_path, "v0.115.0")
    _install_nwjs(tmp_path, "v0.112.0")
    interaction = FakeInteraction(choose_runtime=None)
    _patch_sandbox(monkeypatch, probe="wayland")
    _patch_session(monkeypatch, tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn_forbidden(monkeypatch, "cancelled choice must not run")
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    with pytest.raises(GameValidationError, match="cancelled"):
        launch(paths, repository, game.root, None, False, interaction=interaction)


def test_non_interactive_unregistered_root_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(other)
    _install_nwjs(tmp_path, "v0.90.0")
    _patch_sandbox(monkeypatch, probe="wayland")
    _patch_session(monkeypatch, tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    with pytest.raises(GameValidationError, match="outside configured roots"):
        launch(paths, repository, game.root, "v0.90.0", False, interaction=None)


def test_non_interactive_ambiguous_runtime_picks_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    latest = _install_nwjs(tmp_path, "v0.115.0")
    _install_nwjs(tmp_path, "v0.112.0")
    sandboxes = _patch_sandbox(monkeypatch, probe="wayland")
    _patch_session(monkeypatch, tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn(monkeypatch)
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    assert launch(paths, repository, game.root, None, False, interaction=None).identifier
    assert sandboxes[0].runtime_paths == [latest]


def test_interaction_type_matches_protocol(tmp_path: Path) -> None:
    interaction: Interaction = FakeInteraction()
    assert interaction.confirm_x11(":0") is True
    assert interaction.confirm_add_root(tmp_path) is True
    assert interaction.choose_runtime("nwjs", ("v0.90.0",), "title") == 0


def test_fake_runtime_models_match_orchestration(tmp_path: Path) -> None:
    runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), tmp_path, tmp_path / "nw")
    player = EasyRPGRuntime("0.8.1.1", tmp_path)
    assert runtime.spec.version == "v0.90.0"
    assert player.version == "0.8.1.1"


def test_list_root_files_forwards_to_links_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    sentinel = ("a.txt", "b.py")

    def fake_list_root_files(root: Path) -> tuple[str, ...]:
        captured["root"] = root
        return sentinel

    monkeypatch.setattr("box.api.launch._list_root_files", fake_list_root_files)
    game = GameInfo(EngineName.RPG_MAKER_MZ, tmp_path / "game")
    result = list_root_files(game)
    assert result is sentinel
    assert captured["root"] == game.root
