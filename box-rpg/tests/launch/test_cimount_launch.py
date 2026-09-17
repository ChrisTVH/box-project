"""Case-insensitive mount launch wiring (CLI, API, supervisor teardown)."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from types import TracebackType

import pytest

from box.api.launch import launch
from box.cli.parser import build_parser
from box.config.repository import ConfigRepository
from box.engines.registry import EngineRegistry
from box.errors import GameValidationError, LaunchError
from box.launch import cimount as cimount_module
from box.launch import supervisor as supervisor_module
from box.launch.supervisor import LaunchedSession
from box.models import EngineName, GameInfo
from box.paths import AppPaths
from box.runtime.platform import current_architecture


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


class FakeSandbox:
    """Record sandbox calls and descriptor targets without Bubblewrap."""

    def __init__(self, *, allow_network: bool = False, allow_game_writes: bool = False) -> None:
        self.allow_network = allow_network
        self.allow_game_writes = allow_game_writes
        self.last_arguments: list[str] = []
        self.last_cwd = "/"
        self.nw_game_targets: list[str] = []
        self.nw_game_fds: list[int] = []
        self.saves_targets: list[str] = []
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
        pass

    def persistence(self, paths: AppPaths, game: GameInfo, game_root: Path | None = None) -> None:
        pass

    def game_saves(self, game: GameInfo, descriptor: int) -> int:
        with suppress(OSError):
            self.saves_targets.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        return descriptor

    def game_source_saves(self, source: GameInfo, descriptor: int) -> int:
        return descriptor

    def game_writable(self, descriptor: int) -> None:
        self._kept.append(descriptor)

    def nw_game(self, game: GameInfo, descriptor: int, saves: int) -> None:
        self.nw_game_fds.append(descriptor)
        with suppress(OSError):
            self.nw_game_targets.append(os.readlink(f"/proc/self/fd/{descriptor}"))

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
        self.create_descriptor: int | None = None

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


class FakeCiMountSession:
    """Track launcher-side ownership calls without touching FUSE."""

    def __init__(self, mountpoint: Path) -> None:
        self._mountpoint = mountpoint
        self.disown_calls = 0
        self.unmount_calls = 0

    @property
    def mountpoint(self) -> Path:
        return self._mountpoint

    def disown(self) -> None:
        self.disown_calls += 1

    def unmount(self) -> None:
        self.unmount_calls += 1

    def close(self) -> None:
        pass


def _patch_sandbox(monkeypatch: pytest.MonkeyPatch) -> list[FakeSandbox]:
    sandboxes: list[FakeSandbox] = []

    def factory(*, allow_network: bool = False, allow_game_writes: bool = False) -> FakeSandbox:
        sandbox = FakeSandbox(allow_network=allow_network, allow_game_writes=allow_game_writes)
        sandboxes.append(sandbox)
        return sandbox

    monkeypatch.setattr("box.api.launch.Sandbox", factory)
    return sandboxes


def _patch_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record: list[object]) -> None:
    def factory(
        paths: AppPaths,
        game: GameInfo,
        copy_root_files: tuple[str, ...] = (),
        *,
        game_descriptor: int | None = None,
        game_root: Path | None = None,
    ) -> FakeSession:
        if game_descriptor is not None:
            record.append(game_descriptor)
            with suppress(OSError):
                record.append(Path(os.readlink(f"/proc/self/fd/{game_descriptor}")))
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        session = FakeSession(descriptor)
        return session

    monkeypatch.setattr("box.api.launch.create_session", factory)


def test_cli_launch_parser_accepts_ci_mount() -> None:
    enabled = build_parser().parse_args(["launch", "--ci-mount"])
    assert enabled.ci_mount is True
    disabled = build_parser().parse_args(["launch"])
    assert disabled.ci_mount is False


def test_cli_execute_forwards_ci_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from box.cli import launch as launch_command

    seen: dict[str, object] = {}
    # Pin the early availability pre-check so the forwarding assertion stays
    # hermetic on hosts without libfuse3 or /dev/fuse.
    monkeypatch.setattr("box.cli.launch.api_ci_mount_available", lambda: True)
    real_exists = Path.exists

    def _fuse_present(self: Path) -> bool:
        if self == Path("/dev/fuse"):
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _fuse_present)

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
        seen["ci_mount"] = ci_mount
        return LaunchedSession("testid", "testname", tmp_path / "session")

    def fake_poll(paths: object, identifier: str, name: str) -> int | None:
        return 0

    monkeypatch.setattr("box.cli.launch.api_launch", fake_api)
    monkeypatch.setattr("box.cli.launch.api_poll_status", fake_poll)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    assert launch_command.execute(paths, repository, tmp_path, None, False, ci_mount=True) == 0
    assert seen == {"ci_mount": True}


def test_cli_main_forwards_ci_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    from box.cli.main import main

    seen: dict[str, object] = {}

    def fake_execute(
        *args: object,
        allow_network: bool = False,
        allow_game_writes: bool = False,
        x11: bool = False,
        gamemode: bool = False,
        ci_mount: bool = False,
    ) -> int:
        seen["ci_mount"] = ci_mount
        return 0

    monkeypatch.setattr("box.cli.main.launch_command.execute", fake_execute)
    assert main(["launch", "--ci-mount"]) == 0
    assert seen == {"ci_mount": True}


def test_easyrpg_rejects_ci_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = _easyrpg_game(tmp_path)
    _install_easyrpg(tmp_path, "0.8.1.1")
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def forbidden_spawn(*args: object, **kwargs: object) -> object:
        raise AssertionError("rejected EasyRPG launch must not run")

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.setattr("box.api.launch.spawn_detached", forbidden_spawn)
    with pytest.raises(GameValidationError, match="--ci-mount"):
        launch(paths, repository, game.root, "0.8.1.1", False, ci_mount=True)


def test_nwjs_ci_mount_missing_libfuse3_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    _install_nwjs(tmp_path, "v0.90.0")
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _patch_sandbox(monkeypatch)
    created: list[object] = []
    _patch_session(monkeypatch, tmp_path, created)
    monkeypatch.delenv("DISPLAY", raising=False)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def missing_lib() -> str:
        raise LaunchError("case-insensitive mount requires libfuse3; retry without --ci-mount")

    def forbidden_mount(game_root_fd: int, mountpoint: Path, **kwargs: object) -> object:
        raise AssertionError("missing lib must fail before mounting")

    def forbidden_spawn(*args: object, **kwargs: object) -> object:
        raise AssertionError("missing lib must not spawn")

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.setattr(cimount_module, "require_libfuse3", missing_lib)
    monkeypatch.setattr(cimount_module, "mount_ci_mount", forbidden_mount)
    monkeypatch.setattr("box.api.launch.spawn_detached", forbidden_spawn)
    with pytest.raises(LaunchError, match="libfuse3"):
        launch(paths, repository, game.root, "v0.90.0", False, ci_mount=True)


def test_nwjs_ci_mount_success_uses_mounted_fd_and_disowns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    _install_nwjs(tmp_path, "v0.90.0")
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    sandboxes = _patch_sandbox(monkeypatch)
    created: list[object] = []
    _patch_session(monkeypatch, tmp_path, created)
    monkeypatch.delenv("DISPLAY", raising=False)
    sessions: list[FakeCiMountSession] = []
    spawned: dict[str, object] = {}
    raw_targets: list[str] = []

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def fake_require() -> str:
        return "libfuse3.so.3"

    def fake_mount(game_root_fd: int, mountpoint: Path, **kwargs: object) -> FakeCiMountSession:
        with suppress(OSError):
            raw_targets.append(os.readlink(f"/proc/self/fd/{game_root_fd}"))
        session = FakeCiMountSession(mountpoint)
        sessions.append(session)
        return session  # type: ignore[return-value]

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
        spawned["ci_mountpoint"] = ci_mountpoint
        return LaunchedSession(identifier, name, paths.sessions_root / identifier / name)

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.setattr(cimount_module, "require_libfuse3", fake_require)
    monkeypatch.setattr(cimount_module, "mount_ci_mount", fake_mount)
    monkeypatch.setattr("box.api.launch.spawn_detached", fake_spawn)

    handle = launch(paths, repository, game.root, "v0.90.0", False, ci_mount=True)

    assert handle.identifier
    assert len(sessions) == 1
    assert sessions[0].disown_calls == 1
    assert sessions[0].unmount_calls == 0
    # The spawn received the launcher-owned mountpoint for supervisor teardown.
    assert isinstance(spawned.get("ci_mountpoint"), Path)
    mountpoint = spawned["ci_mountpoint"]
    assert isinstance(mountpoint, Path)
    assert mountpoint.name == cimount_module.CI_MOUNT_DIRNAME
    # The game view consumed the mounted descriptor, never the raw game root.
    assert len(sandboxes) == 1
    assert len(sandboxes[0].nw_game_targets) == 1
    assert len(raw_targets) == 1
    assert sandboxes[0].nw_game_targets[0] != raw_targets[0]
    assert Path(sandboxes[0].nw_game_targets[0]).name == cimount_module.CI_MOUNT_DIRNAME
    assert Path(raw_targets[0]) == game.root


def test_nwjs_ci_mount_failure_unmounts_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    _install_nwjs(tmp_path, "v0.90.0")
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _patch_sandbox(monkeypatch)
    created: list[object] = []
    _patch_session(monkeypatch, tmp_path, created)
    monkeypatch.delenv("DISPLAY", raising=False)
    sessions: list[FakeCiMountSession] = []

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def fake_require() -> str:
        return "libfuse3.so.3"

    def fake_mount(game_root_fd: int, mountpoint: Path, **kwargs: object) -> FakeCiMountSession:
        session = FakeCiMountSession(mountpoint)
        sessions.append(session)
        return session  # type: ignore[return-value]

    def failing_spawn(
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
        raise LaunchError("boom before supervisor owns the mount")

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.setattr(cimount_module, "require_libfuse3", fake_require)
    monkeypatch.setattr(cimount_module, "mount_ci_mount", fake_mount)
    monkeypatch.setattr("box.api.launch.spawn_detached", failing_spawn)
    with pytest.raises(LaunchError, match="boom"):
        launch(paths, repository, game.root, "v0.90.0", False, ci_mount=True)
    assert len(sessions) == 1
    assert sessions[0].disown_calls == 0
    assert sessions[0].unmount_calls == 1


class _SupervisorExit(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class _FakeGameProcess:
    """Minimal Bubblewrap child double that exits immediately."""

    def __init__(self) -> None:
        self.pid = 424242

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int | None:
        return 0

    def terminate(self) -> None:
        pass


def test_supervisor_teardown_unmounts_ci_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.launch.supervisor import _supervisor_main, create_supervisor_session

    paths = _paths(tmp_path)
    identifier = "testidentifier"
    name, root, parent_fd, session_fd = create_supervisor_session(paths, identifier)
    mountpoint = tmp_path / cimount_module.CI_MOUNT_DIRNAME
    mountpoint.mkdir()
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    detached: list[Path] = []

    def fake_unmount(path: Path) -> None:
        detached.append(path)

    def fake_popen(*args: object, **kwargs: object) -> _FakeGameProcess:
        return _FakeGameProcess()

    def fake_exit(code: int) -> None:
        raise _SupervisorExit(code)

    monkeypatch.setattr(supervisor_module, "_close_extra_fds", lambda keep: None)
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cimount_module, "force_unmount", fake_unmount)
    monkeypatch.setattr(supervisor_module.os, "_exit", fake_exit)
    monkeypatch.setattr(supervisor_module.signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor_module.time, "sleep", lambda seconds: None)
    try:
        with pytest.raises(_SupervisorExit) as excinfo:
            _supervisor_main(
                paths,
                identifier,
                name,
                root,
                [str(supervisor_module.BWRAP), "--ro-bind", "/tmp", "/tmp"],
                (),
                parent_fd,
                session_fd,
                write_fd,
                ci_mountpoint=mountpoint,
            )
        assert excinfo.value.code == 0
    finally:
        with suppress(OSError):
            os.close(read_fd)
        with suppress(OSError):
            os.close(write_fd)
        with suppress(OSError):
            os.close(parent_fd)
        with suppress(OSError):
            os.close(session_fd)
    assert detached == [mountpoint]


def test_supervisor_without_ci_mount_skips_unmount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.launch.supervisor import _supervisor_main, create_supervisor_session

    paths = _paths(tmp_path)
    identifier = "testidentifier"
    name, root, parent_fd, session_fd = create_supervisor_session(paths, identifier)
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    calls = 0

    def fake_unmount(path: Path) -> None:
        nonlocal calls
        calls += 1

    def fake_popen(*args: object, **kwargs: object) -> _FakeGameProcess:
        return _FakeGameProcess()

    def fake_exit(code: int) -> None:
        raise _SupervisorExit(code)

    monkeypatch.setattr(supervisor_module, "_close_extra_fds", lambda keep: None)
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cimount_module, "force_unmount", fake_unmount)
    monkeypatch.setattr(supervisor_module.os, "_exit", fake_exit)
    monkeypatch.setattr(supervisor_module.signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor_module.time, "sleep", lambda seconds: None)
    try:
        with pytest.raises(_SupervisorExit):
            _supervisor_main(
                paths,
                identifier,
                name,
                root,
                [str(supervisor_module.BWRAP), "--ro-bind", "/tmp", "/tmp"],
                (),
                parent_fd,
                session_fd,
                write_fd,
            )
    finally:
        with suppress(OSError):
            os.close(read_fd)
        with suppress(OSError):
            os.close(write_fd)
        with suppress(OSError):
            os.close(parent_fd)
        with suppress(OSError):
            os.close(session_fd)
    assert calls == 0


def _fake_fuse_library(name: str) -> str | None:
    assert name == "fuse3"
    return "libfuse3.so.3"


def _missing_fuse_library(name: str) -> str | None:
    assert name == "fuse3"
    return None


def test_is_ci_mount_available_when_library_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from box.api import launch as api_launch_module
    from box.api.launch import is_ci_mount_available

    monkeypatch.setattr(cimount_module, "find_library", _fake_fuse_library)
    assert is_ci_mount_available() is True
    assert "is_ci_mount_available" in api_launch_module.__all__


def test_is_ci_mount_available_when_library_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from box.api.launch import is_ci_mount_available

    monkeypatch.setattr(cimount_module, "find_library", _missing_fuse_library)
    assert is_ci_mount_available() is False


def test_cli_execute_ci_mount_missing_library_warns_then_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from box.cli import launch as launch_command

    monkeypatch.setattr("box.cli.launch.api_ci_mount_available", lambda: False)
    launched = False

    def forbidden_api(
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
        nonlocal launched
        launched = True
        raise AssertionError("unavailable ci-mount must not launch")

    monkeypatch.setattr("box.cli.launch.api_launch", forbidden_api)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    with pytest.raises(LaunchError, match="libfuse3"):
        launch_command.execute(paths, repository, tmp_path, None, False, ci_mount=True)
    assert launched is False
    captured = capsys.readouterr()
    assert "warning:" in captured.err
    assert "libfuse3" in captured.err
    assert "--ci-mount" in captured.err


def test_cli_execute_ci_mount_missing_device_warns_then_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from box.cli import launch as launch_command

    monkeypatch.setattr("box.cli.launch.api_ci_mount_available", lambda: True)
    real_exists = Path.exists

    def missing_device(self: Path) -> bool:
        if self == Path("/dev/fuse"):
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", missing_device)
    launched = False

    def forbidden_api(
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
        nonlocal launched
        launched = True
        raise AssertionError("unavailable ci-mount must not launch")

    monkeypatch.setattr("box.cli.launch.api_launch", forbidden_api)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    with pytest.raises(LaunchError, match="/dev/fuse"):
        launch_command.execute(paths, repository, tmp_path, None, False, ci_mount=True)
    assert launched is False
    captured = capsys.readouterr()
    assert "warning:" in captured.err
    assert "/dev/fuse" in captured.err
    assert "--ci-mount" in captured.err


def test_cli_execute_ci_mount_available_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from box.cli import launch as launch_command

    monkeypatch.setattr("box.cli.launch.api_ci_mount_available", lambda: True)
    real_exists = Path.exists

    def device_present(self: Path) -> bool:
        if self == Path("/dev/fuse"):
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", device_present)
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
        seen["ci_mount"] = ci_mount
        return LaunchedSession("testid", "testname", tmp_path / "session")

    def fake_poll(paths: object, identifier: str, name: str) -> int | None:
        return 0

    monkeypatch.setattr("box.cli.launch.api_launch", fake_api)
    monkeypatch.setattr("box.cli.launch.api_poll_status", fake_poll)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    assert launch_command.execute(paths, repository, tmp_path, None, False, ci_mount=True) == 0
    assert seen == {"ci_mount": True}
    assert "warning:" not in capsys.readouterr().err


def test_cli_execute_without_ci_mount_skips_availability_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from box.cli import launch as launch_command

    monkeypatch.setattr("box.cli.launch.api_ci_mount_available", lambda: False)
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
        seen["ci_mount"] = ci_mount
        return LaunchedSession("testid", "testname", tmp_path / "session")

    def fake_poll(paths: object, identifier: str, name: str) -> int | None:
        return 0

    monkeypatch.setattr("box.cli.launch.api_launch", fake_api)
    monkeypatch.setattr("box.cli.launch.api_poll_status", fake_poll)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    assert launch_command.execute(paths, repository, tmp_path, None, False, ci_mount=False) == 0
    assert seen == {"ci_mount": False}
    assert "warning:" not in capsys.readouterr().err
