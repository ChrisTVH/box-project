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
from box.runtime.catalog import RuntimeCatalog
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
        self.writable_targets: list[str] = []
        self.saves_targets: list[str] = []
        self.nw_game_targets: list[str] = []
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

    def persistence(self, paths: AppPaths, game: GameInfo, game_root: Path | None = None) -> None:
        self.calls.append("persistence")

    def game_saves(self, game: GameInfo, descriptor: int) -> int:
        self.calls.append("game_saves")
        with suppress(OSError):
            self.saves_targets.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        return descriptor

    def game_source_saves(self, source: GameInfo, descriptor: int) -> int:
        self.calls.append("game_source_saves")
        return descriptor

    def game_writable(self, descriptor: int) -> None:
        self.calls.append("game_writable")
        with suppress(OSError):
            self.writable_targets.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        self._kept.append(descriptor)

    def nw_game(self, game: GameInfo, descriptor: int, saves: int) -> None:
        self.calls.append("nw_game")
        with suppress(OSError):
            self.nw_game_targets.append(os.readlink(f"/proc/self/fd/{descriptor}"))

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


def _patch_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record: list[str] | None = None
) -> list[FakeSession]:
    sessions: list[FakeSession] = []

    def factory(
        paths: AppPaths,
        game: GameInfo,
        copy_root_files: tuple[str, ...] = (),
        *,
        game_descriptor: int | None = None,
        game_root: Path | None = None,
        devtools: bool = False,
    ) -> FakeSession:
        if record is not None and game_descriptor is not None:
            with suppress(OSError):
                record.append(os.readlink(f"/proc/self/fd/{game_descriptor}"))
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
        ci_mountpoint: Path | None = None,
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
        ci_mountpoint: Path | None = None,
    ) -> LaunchedSession:
        raise AssertionError(message)

    monkeypatch.setattr("box.api.launch.spawn_detached", forbidden_spawn)


class FakeCiMountSession:
    """Track launcher-side mount ownership calls without touching FUSE."""

    def __init__(self, mountpoint: Path) -> None:
        self._mountpoint = mountpoint
        self.raw_targets: list[str] = []
        self.disown_calls = 0
        self.unmount_calls = 0

    @property
    def mountpoint(self) -> Path:
        return self._mountpoint

    def disown(self) -> None:
        self.disown_calls += 1

    def unmount(self) -> None:
        self.unmount_calls += 1


def _patch_fake_cimount(monkeypatch: pytest.MonkeyPatch) -> list[FakeCiMountSession]:
    """Fake the FUSE mount; the real mountpoint directory still backs the view fd."""
    sessions: list[FakeCiMountSession] = []

    def fake_require() -> str:
        return "libfuse3.so.3"

    def fake_mount(game_root_fd: int, mountpoint: Path, **kwargs: object) -> FakeCiMountSession:
        session = FakeCiMountSession(mountpoint)
        with suppress(OSError):
            session.raw_targets.append(os.readlink(f"/proc/self/fd/{game_root_fd}"))
        sessions.append(session)
        return session

    from box.launch import cimount as cimount_module

    monkeypatch.setattr(cimount_module, "require_libfuse3", fake_require)
    monkeypatch.setattr(cimount_module, "mount_ci_mount", fake_mount)
    return sessions


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
    # Persistent game writes run from the writable /game tree itself.
    assert sandboxes[0].last_cwd == "/game"
    assert sandboxes[0].last_arguments[-1] == "/game"
    assert ("/game/package.json", False) in sandboxes[0].binds


def test_nwjs_ci_mount_with_game_writes_uses_view_for_writable_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_nwjs(tmp_path, "v0.90.0")
    sandboxes = _patch_sandbox(monkeypatch, probe="wayland")
    created: list[str] = []
    _patch_session(monkeypatch, tmp_path, created)
    ci_sessions = _patch_fake_cimount(monkeypatch)
    spawned: list[Path | None] = []

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
    ) -> LaunchedSession:
        spawned.append(ci_mountpoint)
        return LaunchedSession(
            identifier=identifier, name=name, root=paths.sessions_root / identifier / name
        )

    monkeypatch.setattr("box.api.launch.spawn_detached", fake_spawn)
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(
        paths, repository, game.root, "v0.90.0", False, allow_game_writes=True, ci_mount=True
    )
    assert handle.identifier
    assert len(ci_sessions) == 1
    assert ci_sessions[0].disown_calls == 1
    assert ci_sessions[0].unmount_calls == 0
    assert spawned == [ci_sessions[0].mountpoint]
    view = os.path.realpath(ci_sessions[0].mountpoint)
    raw = os.path.realpath(game.root)
    assert view != raw
    # The mount pinned the raw game root.
    assert [os.path.realpath(target) for target in ci_sessions[0].raw_targets] == [raw]
    assert len(sandboxes) == 1
    sandbox = sandboxes[0]
    # The writable /game bind serves the mounted view, never the raw tree.
    assert [os.path.realpath(target) for target in sandbox.writable_targets] == [view]
    assert "game_writable" in sandbox.calls
    assert "nw_game" not in sandbox.calls
    # Saves bypass the view and stay on the raw backing tree.
    assert [os.path.realpath(target) for target in sandbox.saves_targets] == [raw]
    # The session manifest and copies also consumed the mounted view.
    assert [os.path.realpath(target) for target in created] == [view]
    # Persistent game writes run from the writable /game tree itself.
    assert sandbox.last_cwd == "/game"
    assert sandbox.last_arguments[-1] == "/game"
    assert ("/game/package.json", False) in sandbox.binds


def test_nwjs_ci_mount_without_game_writes_keeps_readonly_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_nwjs(tmp_path, "v0.90.0")
    sandboxes = _patch_sandbox(monkeypatch, probe="wayland")
    _patch_session(monkeypatch, tmp_path)
    ci_sessions = _patch_fake_cimount(monkeypatch)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn(monkeypatch)
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    assert launch(paths, repository, game.root, "v0.90.0", False, ci_mount=True).identifier
    assert len(ci_sessions) == 1
    assert len(sandboxes) == 1
    sandbox = sandboxes[0]
    assert "nw_game" in sandbox.calls
    assert "game_writable" not in sandbox.calls
    assert sandbox.writable_targets == []
    view = os.path.realpath(ci_sessions[0].mountpoint)
    assert [os.path.realpath(target) for target in sandbox.nw_game_targets] == [view]
    assert [os.path.realpath(target) for target in sandbox.saves_targets] == [
        os.path.realpath(game.root)
    ]
    assert sandbox.last_cwd == "/session/game"


def test_nwjs_game_writes_without_ci_mount_uses_raw_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_nwjs(tmp_path, "v0.90.0")
    sandboxes = _patch_sandbox(monkeypatch, probe="wayland")
    _patch_session(monkeypatch, tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn(monkeypatch)
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    assert launch(paths, repository, game.root, "v0.90.0", False, allow_game_writes=True).identifier
    assert len(sandboxes) == 1
    sandbox = sandboxes[0]
    assert "game_writable" in sandbox.calls
    raw = os.path.realpath(game.root)
    assert [os.path.realpath(target) for target in sandbox.writable_targets] == [raw]
    assert [os.path.realpath(target) for target in sandbox.saves_targets] == [raw]
    assert sandbox.last_cwd == "/game"


def test_live_ci_mount_writable_view_persists_to_backing_tree(tmp_path: Path) -> None:
    """Real FUSE: a write through the writable view persists to the backing tree.

    Needs the writable mount layer; without it the probe write fails.
    """
    import subprocess

    from box.launch import cimount as cimount_module
    from box.launch.sandbox import Sandbox, clean_environment

    if not Path("/dev/fuse").exists():
        pytest.skip("no /dev/fuse device node")
    if not cimount_module.is_available():
        pytest.skip("no libfuse3 backend")
    if os.uname().machine != "x86_64":
        pytest.skip("the case-insensitive mount requires an x86_64 host")
    if not Path("/usr/bin/bwrap").exists():
        pytest.skip("no Bubblewrap sandbox for the live probe")
    game_root = tmp_path / "game"
    game_root.mkdir()
    (game_root / "Image.PNG").write_bytes(b"image-bytes")
    (game_root / "package.json").write_text('{"name": "fixture"}', encoding="utf-8")
    mountpoint = tmp_path / "ci-mount"
    mountpoint.mkdir()
    cimount_module.drop_stale_ci_mount(mountpoint)
    game_fd = os.open(game_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        session = cimount_module.mount_ci_mount(game_fd, mountpoint)
        try:
            with Sandbox(allow_game_writes=True) as sandbox:
                view_fd = sandbox.keep(
                    os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                )
                sandbox.game_writable(os.dup(view_fd))
                probe = tmp_path / "probe.py"
                probe.write_text(
                    "#!/usr/bin/python3\n"
                    "from pathlib import Path\n"
                    "assert Path('/game/image.png').read_bytes() == b'image-bytes'\n"
                    "Path('/game/shot.txt').write_text('screenshot')\n"
                    "print('ci-write-ok')\n",
                    encoding="utf-8",
                )
                probe.chmod(0o700)
                executable = sandbox.runtime(probe)
                result = subprocess.run(
                    sandbox.command([executable], cwd="/game"),
                    pass_fds=sandbox.pass_fds,
                    env=clean_environment(),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            assert result.returncode == 0, result.stderr
            assert result.stdout == "ci-write-ok\n"
            assert (game_root / "shot.txt").read_text(encoding="utf-8") == "screenshot"
        finally:
            session.disown()
            session.close()
            cimount_module.force_unmount(mountpoint)
    finally:
        with suppress(OSError):
            os.close(game_fd)


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


def test_easyrpg_rejects_devtools_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = _easyrpg_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn_forbidden(monkeypatch, "rejected launch must not run")
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)

    with pytest.raises(GameValidationError, match=r"--devtools"):
        launch(paths, repository, game.root, None, False, devtools=True)


def test_devtools_uses_standard_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    standard_binary = _install_nwjs(tmp_path, "v0.90.0")
    seen: dict[str, bool] = {}

    import box.api.launch as api_launch_module

    real_select = api_launch_module._select_launch_runtime  # pyright: ignore[reportPrivateUsage]

    def spy_select(
        catalog: RuntimeCatalog,
        architecture: str,
        version: str | None,
        sdk: bool,
        preferred: str | None,
        interaction: Interaction | None,
    ) -> RuntimeInfo:
        seen["sdk"] = sdk
        return real_select(catalog, architecture, version, sdk, preferred, interaction)

    monkeypatch.setattr("box.api.launch._select_launch_runtime", spy_select)
    sandboxes = _patch_sandbox(monkeypatch, probe="wayland")
    _patch_session(monkeypatch, tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn(monkeypatch)
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(paths, repository, game.root, "v0.90.0", False, devtools=True)
    assert handle.identifier
    assert seen == {"sdk": False}
    assert sandboxes[0].runtime_paths == [standard_binary]


def test_devtools_forwarded_to_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_nwjs(tmp_path, "v0.90.0")
    seen: dict[str, bool] = {}

    def factory(
        paths_arg: AppPaths,
        game_arg: GameInfo,
        copy_root_files: tuple[str, ...] = (),
        *,
        game_descriptor: int | None = None,
        game_root: Path | None = None,
        devtools: bool = False,
    ) -> FakeSession:
        seen["devtools"] = devtools
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        session = FakeSession(descriptor, tmp_path / "session")
        return session

    monkeypatch.setattr("box.api.launch.create_session", factory)
    _patch_sandbox(monkeypatch, probe="wayland")

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    _patch_spawn(monkeypatch)
    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    assert launch(paths, repository, game.root, "v0.90.0", False, devtools=True).identifier
    assert seen == {"devtools": True}


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


def test_list_executables_forwards_to_links_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.api import launch as api_launch

    captured: dict[str, object] = {}
    sentinel = ("Game.exe",)

    def fake_list_executables(root: Path) -> tuple[str, ...]:
        captured["root"] = root
        return sentinel

    monkeypatch.setattr(api_launch, "_list_root_executables", fake_list_executables)
    game = GameInfo(EngineName.RPG_MAKER_MZ, tmp_path / "game")
    result = api_launch.list_executables(game)
    assert result is sentinel
    assert captured["root"] == game.root
