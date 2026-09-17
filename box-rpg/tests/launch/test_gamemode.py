"""GameMode wrapper detection and sandboxed payload prefix tests."""

from __future__ import annotations

import os
import shutil
from contextlib import suppress
from pathlib import Path
from types import TracebackType

import pytest

from box.api.launch import is_gamemode_available, launch
from box.config.repository import ConfigRepository
from box.engines.registry import EngineRegistry
from box.errors import LaunchError
from box.launch.gamemode import (
    GAMEMODE_PROXY_SOCKET_NAME,
    GAMEMODERUN,
    XDG_DBUS_PROXY,
    is_available,
    require_gamemode,
)
from box.launch.sandbox import BWRAP, Sandbox
from box.models import EngineName, GameInfo
from box.paths import AppPaths
from box.runtime.platform import current_architecture


def _fake_is_file_available(self: Path) -> bool:
    return self in (GAMEMODERUN, XDG_DBUS_PROXY)


def _fake_is_file_missing(self: Path) -> bool:
    return False


def _fake_is_file_gamemoderun_only(self: Path) -> bool:
    return self == GAMEMODERUN


def _fake_is_file_proxy_only(self: Path) -> bool:
    return self == XDG_DBUS_PROXY


def _fake_access_ok(path: object, mode: int) -> bool:
    assert isinstance(mode, int)
    return True


def _fake_access_denied(path: object, mode: int) -> bool:
    assert isinstance(mode, int)
    return False


def _fake_which_found(name: str) -> str | None:
    return f"/usr/bin/{name}"


def _fake_which_missing(name: str) -> str | None:
    assert name in ("gamemoderun", "xdg-dbus-proxy")
    return None


def _fake_which_gamemoderun_only(name: str) -> str | None:
    assert name in ("gamemoderun", "xdg-dbus-proxy")
    return "/usr/bin/gamemoderun" if name == "gamemoderun" else None


def _fake_which_proxy_only(name: str) -> str | None:
    assert name in ("gamemoderun", "xdg-dbus-proxy")
    return "/usr/bin/xdg-dbus-proxy" if name == "xdg-dbus-proxy" else None


def test_is_available_when_fixed_path_is_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_file", _fake_is_file_available)
    monkeypatch.setattr(os, "access", _fake_access_ok)
    monkeypatch.setattr(shutil, "which", _fake_which_missing)
    assert is_available() is True
    assert is_gamemode_available() is True


def test_is_available_falls_back_to_which_when_not_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_file", _fake_is_file_available)
    monkeypatch.setattr(os, "access", _fake_access_denied)
    monkeypatch.setattr(shutil, "which", _fake_which_found)
    assert is_available() is True


def test_is_available_falls_back_to_which_when_fixed_path_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_file", _fake_is_file_missing)
    monkeypatch.setattr(shutil, "which", _fake_which_found)
    assert is_available() is True


def test_is_available_when_missing_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_file", _fake_is_file_missing)
    monkeypatch.setattr(shutil, "which", _fake_which_missing)
    assert is_available() is False
    assert is_gamemode_available() is False


@pytest.mark.parametrize(
    ("is_file", "which", "expected"),
    [
        (_fake_is_file_available, _fake_which_missing, True),
        (_fake_is_file_missing, _fake_which_found, True),
        (_fake_is_file_gamemoderun_only, _fake_which_gamemoderun_only, False),
        (_fake_is_file_proxy_only, _fake_which_proxy_only, False),
        (_fake_is_file_gamemoderun_only, _fake_which_proxy_only, True),
        (_fake_is_file_missing, _fake_which_gamemoderun_only, False),
        (_fake_is_file_missing, _fake_which_proxy_only, False),
    ],
)
def test_is_available_requires_both_binaries(
    monkeypatch: pytest.MonkeyPatch, is_file: object, which: object, expected: bool
) -> None:
    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(os, "access", _fake_access_ok)
    monkeypatch.setattr(shutil, "which", which)
    assert is_available() is expected


def test_require_gamemode_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_file", _fake_is_file_missing)
    monkeypatch.setattr(shutil, "which", _fake_which_missing)
    with pytest.raises(LaunchError, match="gamemoderun"):
        require_gamemode()


def test_require_gamemode_distinct_proxy_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_file", _fake_is_file_gamemoderun_only)
    monkeypatch.setattr(os, "access", _fake_access_ok)
    monkeypatch.setattr(shutil, "which", _fake_which_gamemoderun_only)
    with pytest.raises(LaunchError, match="xdg-dbus-proxy"):
        require_gamemode()


def test_require_gamemode_keeps_gamemoderun_message_when_proxy_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_file", _fake_is_file_proxy_only)
    monkeypatch.setattr(os, "access", _fake_access_ok)
    monkeypatch.setattr(shutil, "which", _fake_which_proxy_only)
    with pytest.raises(LaunchError, match="gamemoderun"):
        require_gamemode()
    with pytest.raises(LaunchError) as excinfo:
        require_gamemode()
    assert "xdg-dbus-proxy" not in str(excinfo.value)


def test_require_gamemode_returns_fixed_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_file", _fake_is_file_available)
    monkeypatch.setattr(os, "access", _fake_access_ok)
    monkeypatch.setattr(shutil, "which", _fake_which_missing)
    assert require_gamemode() == str(GAMEMODERUN)


class FakeSandbox:
    """Record sandbox payloads without touching Bubblewrap."""

    def __init__(self, *, allow_network: bool = False, allow_game_writes: bool = False) -> None:
        self.allow_network = allow_network
        self.allow_game_writes = allow_game_writes
        self.last_arguments: list[str] = []
        self.last_cwd = "/"
        self.gamemode_paths: list[Path] = []
        self._kept: list[int] = []

    def __enter__(self) -> FakeSandbox:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
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
        return "/runtime/fake"

    def display_probe(self) -> str:
        return "wayland"

    def desktop(self) -> None:
        pass

    def x11(self) -> None:
        pass

    def devices(self) -> None:
        pass

    def audio(self) -> None:
        pass

    def gamemode(self, proxy_host_path: Path) -> None:
        self.gamemode_paths.append(proxy_host_path)

    def persistence(self, paths: AppPaths, game: GameInfo, game_root: Path | None = None) -> None:
        pass

    def game_saves(self, game: GameInfo, descriptor: int) -> int:
        return descriptor

    def game_source_saves(self, source: GameInfo, descriptor: int) -> int:
        return descriptor

    def game_writable(self, descriptor: int) -> None:
        self._kept.append(descriptor)

    def nw_game(self, game: GameInfo, descriptor: int, saves: int) -> None:
        pass

    def bind(self, descriptor: int, destination: str, *, writable: bool = False) -> None:
        pass

    def command(self, arguments: list[str], *, cwd: str = "/") -> list[str]:
        self.last_cwd = cwd
        self.last_arguments = list(arguments)
        return ["fake-bwrap", *arguments]


class FakeSession:
    """Minimal isolated session double holding a real directory descriptor."""

    def __init__(self, session_descriptor: int) -> None:
        self.session_descriptor = session_descriptor
        self.parent_descriptor = os.dup(session_descriptor)
        self.name = "testsession"
        self.root = Path("/tmp/testsession")

    @property
    def identifier(self) -> str:
        return "testidentifier"

    def detach(self) -> None:
        pass

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


def _install_nwjs(tmp_path: Path, version: str) -> None:
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


def _install_easyrpg(tmp_path: Path, version: str) -> None:
    binary = tmp_path / "cache" / "runtimes" / "easyrpg" / version / "easyrpg-player"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)


def _patch_sandbox(monkeypatch: pytest.MonkeyPatch) -> list[FakeSandbox]:
    sandboxes: list[FakeSandbox] = []

    def factory(*, allow_network: bool = False, allow_game_writes: bool = False) -> FakeSandbox:
        sandbox = FakeSandbox(allow_network=allow_network, allow_game_writes=allow_game_writes)
        sandboxes.append(sandbox)
        return sandbox

    monkeypatch.setattr("box.api.launch.Sandbox", factory)
    return sandboxes


def _patch_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def factory(
        paths: AppPaths,
        game: GameInfo,
        copy_root_files: tuple[str, ...] = (),
        *,
        game_descriptor: int | None = None,
        game_root: Path | None = None,
    ) -> FakeSession:
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        return FakeSession(descriptor)

    monkeypatch.setattr("box.api.launch.create_session", factory)


def _make_gamemode_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("box.launch.gamemode.is_available", lambda: True)
    monkeypatch.setattr("box.launch.gamemode.require_gamemode", lambda: str(GAMEMODERUN))


def _make_gamemode_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from box.launch import gamemode as gamemode_module

    def _missing() -> str:
        raise LaunchError(
            "GameMode requires gamemoderun (/usr/bin/gamemoderun); "
            "install GameMode or retry without --gamemode"
        )

    monkeypatch.setattr("box.launch.gamemode.is_available", lambda: False)
    monkeypatch.setattr(gamemode_module, "require_gamemode", _missing)
    monkeypatch.setattr("box.api.launch._gamemode.require_gamemode", _missing)


@pytest.mark.parametrize("engine", ["easyrpg", "nwjs"])
def test_launch_with_gamemode_missing_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    if engine == "easyrpg":
        game = _easyrpg_game(tmp_path)
        _install_easyrpg(tmp_path, "0.8.1.1")
        version: str | None = "0.8.1.1"
    else:
        game = _nwjs_game(tmp_path)
        _install_nwjs(tmp_path, "v0.90.0")
        version = "v0.90.0"
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _patch_sandbox(monkeypatch)
    _patch_session(monkeypatch, tmp_path)
    _make_gamemode_missing(monkeypatch)
    monkeypatch.delenv("DISPLAY", raising=False)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

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
        ci_mountpoint: Path | None = None,
    ) -> object:
        raise AssertionError("missing GameMode must not run")

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.setattr("box.api.launch.spawn_detached", forbidden_spawn)

    with pytest.raises(LaunchError, match="gamemoderun"):
        launch(paths, repository, game.root, version, False, use_gamemode=True)


@pytest.mark.parametrize("engine", ["easyrpg", "nwjs"])
def test_launch_with_gamemode_prefixes_inside_sandbox_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    if engine == "easyrpg":
        game = _easyrpg_game(tmp_path)
        _install_easyrpg(tmp_path, "0.8.1.1")
        version: str | None = "0.8.1.1"
    else:
        game = _nwjs_game(tmp_path)
        _install_nwjs(tmp_path, "v0.90.0")
        version = "v0.90.0"
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    sandboxes = _patch_sandbox(monkeypatch)
    _patch_session(monkeypatch, tmp_path)
    _make_gamemode_available(monkeypatch)
    monkeypatch.delenv("DISPLAY", raising=False)
    commands: list[list[str]] = []

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

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
        ci_mountpoint: Path | None = None,
    ) -> object:
        commands.append(command)
        from box.launch.supervisor import LaunchedSession

        return LaunchedSession(identifier, name, paths.sessions_root / identifier / name)

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.setattr("box.api.launch.spawn_detached", fake_spawn)

    handle = launch(paths, repository, game.root, version, False, use_gamemode=True)
    assert handle.identifier
    assert len(sandboxes) == 1
    assert len(commands) == 1
    assert commands[0][0] == "fake-bwrap"
    assert commands[0][0] != str(GAMEMODERUN)
    assert sandboxes[0].last_arguments[0] == str(GAMEMODERUN)
    assert sandboxes[0].last_arguments[1] == "/runtime/fake"
    assert len(sandboxes[0].gamemode_paths) == 1
    assert sandboxes[0].gamemode_paths[0].name == GAMEMODE_PROXY_SOCKET_NAME


def test_gamemode_prefix_survives_real_bootstrap_wrapping() -> None:
    with Sandbox() as sandbox:
        payload = [str(GAMEMODERUN), "/runtime/fake", "--fullscreen"]
        full = sandbox.command(payload, cwd="/game")
    assert full[0] == str(BWRAP)
    separator = full.index("--")
    assert full[separator + 1] == "/usr/bin/python3"
    bootstrap_index = separator + 6
    assert full[bootstrap_index] == str(GAMEMODERUN)
    assert full[bootstrap_index + 1] == "/runtime/fake"


def test_cli_launch_parser_accepts_gamemode() -> None:
    from box.cli.parser import build_parser

    enabled = build_parser().parse_args(["launch", "--gamemode"])
    assert enabled.gamemode is True
    disabled = build_parser().parse_args(["launch"])
    assert disabled.gamemode is False


def test_cli_execute_forwards_gamemode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from box.cli import launch as launch_command

    seen: dict[str, object] = {}

    def fake_api(
        paths: object,
        repository: object,
        game_path: Path,
        version: str | None,
        sdk: bool,
        copy_root_files: tuple[str, ...] = (),
        *,
        allow_network: bool = False,
        allow_game_writes: bool = False,
        x11: bool = False,
        use_gamemode: bool = False,
        ci_mount: bool = False,
        interaction: object = None,
    ) -> object:
        seen["use_gamemode"] = use_gamemode
        from box.launch.supervisor import LaunchedSession

        return LaunchedSession("testid", "testname", tmp_path / "session")

    def fake_poll(paths: object, identifier: str, name: str) -> int | None:
        return 0

    monkeypatch.setattr("box.cli.launch.api_launch", fake_api)
    monkeypatch.setattr("box.cli.launch.api_poll_status", fake_poll)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    assert launch_command.execute(paths, repository, tmp_path, None, False, gamemode=True) == 0
    assert seen == {"use_gamemode": True}
