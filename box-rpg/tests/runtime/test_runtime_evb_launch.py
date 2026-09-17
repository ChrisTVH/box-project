"""Tests for packed-executable unpacking hooks in non-interactive launch."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from types import TracebackType

import pytest

from box.api.launch import launch
from box.config.repository import ConfigRepository
from box.errors import RuntimeError
from box.games.identity import game_id
from box.launch.supervisor import LaunchedSession
from box.models import GameInfo
from box.paths import AppPaths
from box.runtime import evb as evb_module
from box.runtime.platform import current_architecture


class FakeInteraction:
    """Record GUI callbacks with scripted answers."""

    def __init__(self, *, confirm_add_root: bool = True) -> None:
        self._confirm_add_root = confirm_add_root
        self.add_root_calls: list[Path] = []

    def confirm_x11(self, display: str) -> bool:
        del display
        return True

    def confirm_add_root(self, path: Path) -> bool:
        self.add_root_calls.append(path)
        return self._confirm_add_root

    def choose_runtime(self, kind: str, candidates: tuple[str, ...], title: str) -> int | None:
        del kind, candidates, title
        return 0


class FakeSandbox:
    """Record sandbox policy calls without touching Bubblewrap."""

    def __init__(self, *, allow_network: bool = False, allow_game_writes: bool = False) -> None:
        self.allow_network = allow_network
        self.allow_game_writes = allow_game_writes
        self.calls: list[str] = []
        self.persistence_roots: list[Path | None] = []
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

    def gamemode(self, socket_path: Path) -> None:
        del socket_path
        self.calls.append("gamemode")

    def persistence(self, paths: AppPaths, game: GameInfo, game_root: Path | None = None) -> None:
        del paths, game
        self.calls.append("persistence")
        self.persistence_roots.append(game_root)

    def game_saves(self, game: GameInfo, descriptor: int) -> int:
        del game
        self.calls.append("game_saves")
        duplicated = os.dup(descriptor)
        self._kept.append(duplicated)
        return duplicated

    def game_source_saves(self, source: GameInfo, descriptor: int) -> int:
        del source
        self.calls.append("game_source_saves")
        duplicated = os.dup(descriptor)
        self._kept.append(duplicated)
        return duplicated

    def game_writable(self, descriptor: int) -> None:
        self.calls.append("game_writable")
        self._kept.append(descriptor)

    def nw_game(self, game: GameInfo, descriptor: int, saves: int) -> None:
        del game, descriptor, saves
        self.calls.append("nw_game")

    def bind(self, descriptor: int, destination: str, *, writable: bool = False) -> None:
        del writable
        self.calls.append(f"bind:{destination}")
        self._kept.append(descriptor)

    def command(self, arguments: list[str], *, cwd: str = "/") -> list[str]:
        self.calls.append(f"command:{cwd}")
        self.last_cwd = cwd
        self.last_arguments = list(arguments)
        return ["fake-bwrap", *arguments]


class FakeSession:
    """Minimal isolated session double keyed by the launched game root."""

    def __init__(
        self, game: GameInfo, descriptor: int, root: Path, game_root: Path | None = None
    ) -> None:
        self.identifier = game_id(game.root if game_root is None else game_root)
        self.session_descriptor = descriptor
        self.parent_descriptor = os.dup(descriptor)
        self.root = root
        self.name = "testsession"

    def detach(self) -> None:
        return None

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
    """Return isolated launcher paths below the test directory."""
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


def _packed_source(tmp_path: Path) -> tuple[Path, Path]:
    """Create a packed candidate directory with one custom-named executable."""
    source = tmp_path / "source-game"
    source.mkdir()
    executable = source / "My Custom Game.exe"
    executable.write_bytes(b"fake packed executable")
    return (source, executable)


def _mv_cache_tree(tmp_path: Path) -> Path:
    """Create a detectable MV tree standing in for an unpacked cache entry."""
    cache = tmp_path / "cache-entry"
    (cache / "www" / "js").mkdir(parents=True)
    (cache / "www" / "index.html").write_text("fixture", encoding="utf-8")
    (cache / "www" / "js" / "plugins.js").write_text("fixture", encoding="utf-8")
    (cache / "package.json").write_text('{"name": "fixture"}', encoding="utf-8")
    return cache


def _rpg_rt_cache_tree(tmp_path: Path) -> Path:
    """Create a detectable 2000/2003 tree for the EasyRPG branch."""
    cache = tmp_path / "cache-entry"
    cache.mkdir(parents=True)
    for filename in ("RPG_RT.ini", "RPG_RT.ldb", "RPG_RT.lmt"):
        (cache / filename).write_text("fixture", encoding="utf-8")
    return cache


def _install_nwjs(tmp_path: Path, version: str) -> None:
    """Install one fake NW.js runtime into the isolated cache."""
    arch = current_architecture()
    binary = (
        tmp_path / "cache" / "runtimes" / "nwjs" / f"linux-{arch}" / f"standard-{version.lower()}"
    ) / "nw"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)


def _install_easyrpg(tmp_path: Path, version: str) -> None:
    """Install one fake EasyRPG runtime into the isolated cache."""
    binary = tmp_path / "cache" / "runtimes" / "easyrpg" / version / "easyrpg-player"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)


def _patch_sandbox(monkeypatch: pytest.MonkeyPatch) -> list[FakeSandbox]:
    """Replace Bubblewrap sandboxing with recording doubles."""
    sandboxes: list[FakeSandbox] = []

    def factory(*, allow_network: bool = False, allow_game_writes: bool = False) -> FakeSandbox:
        sandbox = FakeSandbox(allow_network=allow_network, allow_game_writes=allow_game_writes)
        sandboxes.append(sandbox)
        return sandbox

    monkeypatch.setattr("box.api.launch.Sandbox", factory)
    return sandboxes


def _patch_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Path | None]:
    """Replace session creation with doubles keyed by the launched root."""

    seen: list[Path | None] = []

    def factory(
        paths: AppPaths,
        game: GameInfo,
        copy_root_files: tuple[str, ...] = (),
        *,
        game_descriptor: int | None = None,
        game_root: Path | None = None,
    ) -> FakeSession:
        del paths, copy_root_files, game_descriptor
        seen.append(game_root)
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        return FakeSession(game, descriptor, tmp_path / "session", game_root)

    monkeypatch.setattr("box.api.launch.create_session", factory)
    return seen


def _patch_spawn(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Mock detached spawn; capture commands and return a fake handle."""
    commands: list[list[str]] = []

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
        commands.append(list(command))
        return LaunchedSession(
            identifier=identifier, name=name, root=paths.sessions_root / identifier / name
        )

    monkeypatch.setattr("box.api.launch.spawn_detached", fake_spawn)
    return commands


def _stub_ensure_unpacked(monkeypatch: pytest.MonkeyPatch, cache: Path) -> None:
    """Redirect cache unpacking to a prepared detectable tree."""

    def fake_unpack(paths_arg: AppPaths, exe: Path) -> Path:
        del paths_arg, exe
        return cache

    monkeypatch.setattr(evb_module, "ensure_unpacked", fake_unpack)


def test_nwjs_packed_launch_authorizes_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unpacking precedes detection; consent and identity cover the source."""
    source, _executable = _packed_source(tmp_path)
    cache = _mv_cache_tree(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_nwjs(tmp_path, "v0.90.0")
    interaction = FakeInteraction()
    sandboxes = _patch_sandbox(monkeypatch)
    session_roots = _patch_session(monkeypatch, tmp_path)
    commands = _patch_spawn(monkeypatch)
    _stub_ensure_unpacked(monkeypatch, cache)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(paths, repository, source, "v0.90.0", False, interaction=interaction)

    assert interaction.add_root_calls == [source]
    assert repository.load().allowed_game_roots == (source,)
    assert handle.identifier == game_id(source)
    assert handle.identifier != game_id(cache)
    assert session_roots == [source]
    assert sandboxes[0].persistence_roots == [source]
    assert len(commands) == 1
    assert commands[0][0] == "fake-bwrap"


def test_easyrpg_packed_launch_authorizes_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The EasyRPG branch shares the unpack-before-detect hook and consent."""
    source, _executable = _packed_source(tmp_path)
    cache = _rpg_rt_cache_tree(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_easyrpg(tmp_path, "0.8.1.1")
    interaction = FakeInteraction()
    sandboxes = _patch_sandbox(monkeypatch)
    commands = _patch_spawn(monkeypatch)
    _stub_ensure_unpacked(monkeypatch, cache)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(paths, repository, source, "0.8.1.1", False, interaction=interaction)

    assert interaction.add_root_calls == [source]
    assert repository.load().allowed_game_roots == (source,)
    assert handle.identifier == game_id(source)
    assert handle.identifier != game_id(cache)
    assert sandboxes[0].persistence_roots == [source]
    assert len(commands) == 1
    assert sandboxes[0].last_cwd == "/game"
    assert sandboxes[0].last_arguments[-6:] == [
        "/runtime/fake",
        "--project-path",
        "/game",
        "--fullscreen",
        "--save-path",
        "/game/save",
    ]


def test_noncandidate_launch_skips_unpacking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain game directories launch without touching the unpack cache."""
    source = _mv_cache_tree(tmp_path / "plain")
    (source / "game.nw").write_text("fixture", encoding="utf-8")
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_nwjs(tmp_path, "v0.90.0")
    interaction = FakeInteraction()
    _patch_sandbox(monkeypatch)
    _patch_session(monkeypatch, tmp_path)
    _patch_spawn(monkeypatch)

    def forbidden_unpack(paths_arg: AppPaths, exe: Path) -> Path:
        raise AssertionError(f"unpacking must not run for {exe}")

    monkeypatch.setattr(evb_module, "ensure_unpacked", forbidden_unpack)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(paths, repository, source, "v0.90.0", False, interaction=interaction)

    assert interaction.add_root_calls == [source]
    assert handle.identifier == game_id(source)


def test_unpack_failure_aborts_before_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt packed inputs fail the launch with the unpacking error."""
    source, _executable = _packed_source(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_nwjs(tmp_path, "v0.90.0")
    _patch_sandbox(monkeypatch)
    _patch_session(monkeypatch, tmp_path)
    _patch_spawn(monkeypatch)

    def failing_unpack(paths_arg: AppPaths, exe: Path) -> Path:
        raise RuntimeError("cannot decode packed payload")

    monkeypatch.setattr(evb_module, "ensure_unpacked", failing_unpack)
    monkeypatch.delenv("DISPLAY", raising=False)

    with pytest.raises(RuntimeError, match="cannot decode packed payload"):
        launch(
            paths,
            repository,
            source,
            "v0.90.0",
            False,
            interaction=FakeInteraction(),
        )


def test_packed_source_engine_is_cached_game_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launched engine follows the unpacked tree, not the source name."""
    source, _executable = _packed_source(tmp_path)
    cache = _mv_cache_tree(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_nwjs(tmp_path, "v0.90.0")
    sandboxes = _patch_sandbox(monkeypatch)
    _patch_session(monkeypatch, tmp_path)
    _patch_spawn(monkeypatch)
    _stub_ensure_unpacked(monkeypatch, cache)
    monkeypatch.delenv("DISPLAY", raising=False)

    launch(paths, repository, source, "v0.90.0", False, interaction=FakeInteraction())

    assert "nw_game" in sandboxes[0].calls
    assert sandboxes[0].last_cwd == "/session/game"


def test_packed_launch_while_game_lock_held_fails_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launching while the profile game entry is locked fails fast as busy."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from box.runtime.security import cache_lock

    source, _executable = _packed_source(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_nwjs(tmp_path, "v0.90.0")
    _patch_sandbox(monkeypatch)
    _patch_session(monkeypatch, tmp_path)
    _patch_spawn(monkeypatch)
    monkeypatch.delenv("DISPLAY", raising=False)
    identifier = game_id(source)
    profile_descriptor = paths.open_or_create_private_cache_directory("profiles", identifier)
    entered = threading.Event()
    release = threading.Event()
    try:

        def hold_lock() -> None:
            with cache_lock(profile_descriptor, "game"):
                entered.set()
                assert release.wait(timeout=30)

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(hold_lock)
            assert entered.wait(timeout=30)
            try:
                with pytest.raises(RuntimeError, match="busy"):
                    launch(
                        paths,
                        repository,
                        source,
                        "v0.90.0",
                        False,
                        interaction=FakeInteraction(),
                    )
            finally:
                release.set()
                pending.result(timeout=30)
    finally:
        os.close(profile_descriptor)
