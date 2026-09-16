"""Filtered GameMode D-Bus proxy tests (no network, no real D-Bus)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from box.api.launch import launch
from box.config.repository import ConfigRepository
from box.errors import LaunchError
from box.launch import supervisor as supervisor_module
from box.launch.gamemode import (
    GAMEMODE_BUS_NAME,
    GAMEMODE_PROXY_SOCKET_NAME,
    GAMEMODERUN,
    XDG_DBUS_PROXY,
    proxy_argv,
    resolve_session_bus_address,
)
from box.launch.sandbox import Sandbox, clean_environment
from box.models import EngineName, GameInfo
from box.paths import AppPaths
from box.runtime.platform import current_architecture


def test_resolve_prefers_explicit_bus_address() -> None:
    assert (
        resolve_session_bus_address({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/bus"})
        == "unix:path=/run/bus"
    )


def test_resolve_falls_back_to_runtime_bus() -> None:
    assert (
        resolve_session_bus_address({"XDG_RUNTIME_DIR": "/run/user/1000"})
        == "unix:path=/run/user/1000/bus"
    )


def test_resolve_prefers_bus_over_runtime_fallback() -> None:
    assert (
        resolve_session_bus_address(
            {"DBUS_SESSION_BUS_ADDRESS": "unix:abstract=/tmp/bus", "XDG_RUNTIME_DIR": "/run/user/0"}
        )
        == "unix:abstract=/tmp/bus"
    )


def test_resolve_fails_closed_without_any_address() -> None:
    with pytest.raises(LaunchError):
        resolve_session_bus_address({})
    with pytest.raises(LaunchError):
        resolve_session_bus_address({"DBUS_SESSION_BUS_ADDRESS": "", "XDG_RUNTIME_DIR": ""})


def test_proxy_argv_exact_order(tmp_path: Path) -> None:
    socket_path = tmp_path / GAMEMODE_PROXY_SOCKET_NAME
    assert proxy_argv("unix:path=/run/user/1000/bus", socket_path) == [
        str(XDG_DBUS_PROXY),
        "unix:path=/run/user/1000/bus",
        str(socket_path),
        "--filter",
        f"--talk={GAMEMODE_BUS_NAME}",
    ]
    assert GAMEMODE_BUS_NAME == "com.feralinteractive.GameMode"


def test_sandbox_gamemode_binds_proxy_and_sets_filtered_bus(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    proxy_path = session_dir / GAMEMODE_PROXY_SOCKET_NAME
    with Sandbox() as sandbox:
        before = len(sandbox.options)
        sandbox.gamemode(proxy_path)
        extra = sandbox.options[before:]
        assert extra[0] == "--ro-bind"
        assert extra[1] == str(proxy_path)
        assert extra[2] == f"/run/user/{GAMEMODE_PROXY_SOCKET_NAME}"
        assert "--setenv" in extra
        index = extra.index("--setenv", extra.index("DBUS_SESSION_BUS_ADDRESS") - 1)
        assert extra[index + 1] == "DBUS_SESSION_BUS_ADDRESS"
        assert extra[index + 2] == f"unix:path=/run/user/{GAMEMODE_PROXY_SOCKET_NAME}"
        assert "--see=" not in " ".join(extra)
        assert "--own=" not in " ".join(extra)
        assert "--broadcast" not in " ".join(extra)
        # Only one filtered talk entry for exactly the GameMode name.
        assert extra.count(f"--talk={GAMEMODE_BUS_NAME}") == 0  # talk lives in proxy argv
        assert "WAYLAND_DISPLAY" not in extra
        assert "DISPLAY" not in extra
        assert "DBUS_SESSION_BUS_ADDRESS" not in clean_environment()


def test_sandbox_gamemode_rejects_wrong_filename(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    with Sandbox() as sandbox, pytest.raises(LaunchError):
        sandbox.gamemode(session_dir / "other-proxy")


def test_sandbox_gamemode_rejects_relative_path() -> None:
    with Sandbox() as sandbox, pytest.raises(LaunchError):
        sandbox.gamemode(Path("gamemode-proxy"))


def test_sandbox_gamemode_fails_closed_on_insecure_parent(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o755)
    with Sandbox() as sandbox, pytest.raises(LaunchError, match="proxy"):
        sandbox.gamemode(session_dir / GAMEMODE_PROXY_SOCKET_NAME)


def test_sandbox_gamemode_rejects_symlinked_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with Sandbox() as sandbox, pytest.raises(LaunchError):
        sandbox.gamemode(link / GAMEMODE_PROXY_SOCKET_NAME)


class _ExitSupervisor(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class FakeProc:
    """Minimal Popen double recording termination requests."""

    _next_pid = 50000

    def __init__(self, *, already_exited: bool = False) -> None:
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self._exited = already_exited
        self._exit_code = 0

    def poll(self) -> int | None:
        return self._exit_code if self._exited else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self._exited = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.terminated or self.killed:
            self._exited = True
        if self._exited:
            return self._exit_code
        return self._exit_code


def _supervisor_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AppPaths, str, str, Path, int, int, int, dict[str, Any]]:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    identifier = "testgame"
    name = "testsession"
    root = tmp_path / "session"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / supervisor_module.SESSION_LOCK_NAME).touch(exist_ok=True)
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    session_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    state: dict[str, Any] = {"pipe_errors": [], "removed": [], "signals": {}, "kills": []}
    monkeypatch.setattr(supervisor_module, "_close_extra_fds", lambda keep: None)
    monkeypatch.setattr(os, "dup2", lambda old, new: None)
    monkeypatch.setattr(supervisor_module, "_list_session_names", lambda p, i: [])
    monkeypatch.setattr(supervisor_module, "is_session_running", lambda p, i, n: False)
    monkeypatch.setattr(supervisor_module, "poll_launch_status", lambda p, i, n: None)

    def fake_pipe_error(pipe: int, message: str) -> None:
        state["pipe_errors"].append(message)

    def fake_remove(p: AppPaths, r: Path, parent_descriptor: int, session_descriptor: int) -> None:
        state["removed"].append(r)

    def fake_exit(code: int) -> None:
        raise _ExitSupervisor(code)

    monkeypatch.setattr(supervisor_module, "_pipe_error", fake_pipe_error)
    monkeypatch.setattr(supervisor_module, "_remove_supervised", fake_remove)
    monkeypatch.setattr(os, "_exit", fake_exit)
    # Keep the pipe read end for the caller; the supervisor owns the write end.
    state["read_fd"] = read_fd
    return paths, identifier, name, root, parent_fd, session_fd, write_fd, state


def test_supervisor_starts_proxy_with_exact_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    expected_proxy = root / GAMEMODE_PROXY_SOCKET_NAME
    address = "unix:path=/run/user/1000/bus"
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    # Host registration is mocked: it would use subprocess.run, which shares
    # the mocked Popen object, so the real D-Bus call stays out of this test.
    monkeypatch.setattr(supervisor_module._gamemode, "register_host_game", lambda pid, **kw: None)
    monkeypatch.setattr(supervisor_module._gamemode, "unregister_host_game", lambda pid, **kw: True)
    calls: list[list[str]] = []
    proxy = FakeProc()
    bwrap = FakeProc(already_exited=True)

    def fake_popen(args: list[str], **kwargs: Any) -> FakeProc:
        calls.append(list(args))
        assert kwargs.get("close_fds") is True
        assert kwargs.get("stdin") is subprocess.DEVNULL
        assert kwargs.get("stdout") is subprocess.DEVNULL
        assert kwargs.get("stderr") is subprocess.DEVNULL
        assert kwargs.get("shell", False) is False
        if len(calls) == 1:
            assert "pass_fds" not in kwargs
            return proxy
        assert kwargs.get("pass_fds") == ()
        return bwrap

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        supervisor_module, "_wait_for_proxy_socket", lambda path, proc, timeout=2.0: True
    )
    command = [str(supervisor_module.BWRAP), "--ro-bind", "/a", "/b"]
    with pytest.raises(_ExitSupervisor) as excinfo:
        supervisor_module._supervisor_main(
            paths,
            identifier,
            name,
            root,
            command,
            (),
            parent_fd,
            session_fd,
            pipe_write,
            use_gamemode=True,
            gamemode_proxy=expected_proxy,
        )
    assert excinfo.value.code == 0
    assert len(calls) == 2
    assert calls[0] == [
        str(XDG_DBUS_PROXY),
        address,
        str(expected_proxy),
        "--filter",
        f"--talk={GAMEMODE_BUS_NAME}",
    ]
    assert calls[1][0] == str(supervisor_module.BWRAP)
    assert state["removed"]
    os.close(state["read_fd"])
    with suppress(OSError):
        os.close(pipe_write)


def test_supervisor_gates_ready_on_socket_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    proxy = FakeProc()
    calls: list[list[str]] = []

    def fake_popen(args: list[str], **kwargs: Any) -> FakeProc:
        calls.append(list(args))
        return proxy

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        supervisor_module, "_wait_for_proxy_socket", lambda path, proc, timeout=2.0: False
    )
    terminated: list[FakeProc] = []
    monkeypatch.setattr(supervisor_module, "_terminate_proxy", lambda proc: terminated.append(proc))
    with pytest.raises(_ExitSupervisor) as excinfo:
        supervisor_module._supervisor_main(
            paths,
            identifier,
            name,
            root,
            [str(supervisor_module.BWRAP)],
            (),
            parent_fd,
            session_fd,
            pipe_write,
            use_gamemode=True,
            gamemode_proxy=root / GAMEMODE_PROXY_SOCKET_NAME,
        )
    assert excinfo.value.code == 1
    assert len(calls) == 1  # bwrap never starts without a ready proxy socket
    assert terminated == [proxy]
    assert state["removed"]
    assert state["pipe_errors"]
    os.close(state["read_fd"])
    with suppress(OSError):
        os.close(pipe_write)


def test_supervisor_kills_proxy_on_bwrap_error_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    proxy = FakeProc()
    terminated: list[FakeProc | None] = []
    monkeypatch.setattr(supervisor_module, "_terminate_proxy", lambda proc: terminated.append(proc))
    monkeypatch.setattr(
        supervisor_module, "_wait_for_proxy_socket", lambda path, proc, timeout=2.0: True
    )

    def fail_bwrap(args: list[str], **kwargs: Any) -> FakeProc:
        if str(args[0]).endswith("xdg-dbus-proxy"):
            return proxy
        raise OSError("bwrap missing")

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fail_bwrap)
    with pytest.raises(_ExitSupervisor) as excinfo:
        supervisor_module._supervisor_main(
            paths,
            identifier,
            name,
            root,
            [str(supervisor_module.BWRAP)],
            (),
            parent_fd,
            session_fd,
            pipe_write,
            use_gamemode=True,
            gamemode_proxy=root / GAMEMODE_PROXY_SOCKET_NAME,
        )
    assert excinfo.value.code == 1
    assert terminated == [proxy]
    assert state["removed"]


def test_supervisor_kills_proxy_after_successful_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, _state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setattr(supervisor_module._gamemode, "register_host_game", lambda pid, **kw: None)
    monkeypatch.setattr(supervisor_module._gamemode, "unregister_host_game", lambda pid, **kw: True)
    proxy = FakeProc()
    bwrap = FakeProc(already_exited=True)
    terminated: list[FakeProc | None] = []
    monkeypatch.setattr(supervisor_module, "_terminate_proxy", lambda proc: terminated.append(proc))
    monkeypatch.setattr(
        supervisor_module, "_wait_for_proxy_socket", lambda path, proc, timeout=2.0: True
    )

    def fake_popen(args: list[str], **kwargs: Any) -> FakeProc:
        return proxy if str(args[0]).endswith("xdg-dbus-proxy") else bwrap

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    with pytest.raises(_ExitSupervisor) as excinfo:
        supervisor_module._supervisor_main(
            paths,
            identifier,
            name,
            root,
            [str(supervisor_module.BWRAP)],
            (),
            parent_fd,
            session_fd,
            pipe_write,
            use_gamemode=True,
            gamemode_proxy=root / GAMEMODE_PROXY_SOCKET_NAME,
        )
    assert excinfo.value.code == 0
    assert terminated == [proxy]


def test_supervisor_sigterm_reaches_proxy_and_bwrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setattr(supervisor_module._gamemode, "register_host_game", lambda pid, **kw: None)
    monkeypatch.setattr(supervisor_module._gamemode, "unregister_host_game", lambda pid, **kw: True)
    proxy = FakeProc()
    bwrap = FakeProc()
    handlers: dict[int, Any] = {}
    monkeypatch.setattr(
        signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler)
    )
    kills: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append(pid))
    monkeypatch.setattr(supervisor_module, "_terminate_proxy", lambda proc: None)
    monkeypatch.setattr(
        supervisor_module, "_wait_for_proxy_socket", lambda path, proc, timeout=2.0: True
    )

    def fake_popen(args: list[str], **kwargs: Any) -> FakeProc:
        return proxy if str(args[0]).endswith("xdg-dbus-proxy") else bwrap

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    with pytest.raises(_ExitSupervisor):
        supervisor_module._supervisor_main(
            paths,
            identifier,
            name,
            root,
            [str(supervisor_module.BWRAP)],
            (),
            parent_fd,
            session_fd,
            pipe_write,
            use_gamemode=True,
            gamemode_proxy=root / GAMEMODE_PROXY_SOCKET_NAME,
        )
    assert signal.SIGTERM in handlers
    kills.clear()
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert proxy.pid in kills
    assert bwrap.pid in kills
    os.close(state["read_fd"])
    with suppress(OSError):
        os.close(pipe_write)


def test_wait_for_proxy_socket_validates_socket_and_owner(tmp_path: Path) -> None:
    proxy = FakeProc()
    missing = tmp_path / "missing-proxy"
    assert supervisor_module._wait_for_proxy_socket(missing, proxy, timeout=0.1) is False
    regular = tmp_path / "regular"
    regular.write_text("not a socket")
    assert supervisor_module._wait_for_proxy_socket(regular, proxy, timeout=0.1) is False
    sock_path = tmp_path / "proxy.sock"
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(sock_path))
        assert supervisor_module._wait_for_proxy_socket(sock_path, proxy, timeout=2.0) is True
    exited = FakeProc(already_exited=True)
    assert supervisor_module._wait_for_proxy_socket(missing, exited, timeout=0.1) is False


def test_terminate_proxy_sigterm_then_kill() -> None:
    live = FakeProc()
    supervisor_module._terminate_proxy(live)
    assert live.terminated is True
    assert live.wait_calls >= 1
    exited = FakeProc(already_exited=True)
    supervisor_module._terminate_proxy(exited)
    assert exited.terminated is False
    supervisor_module._terminate_proxy(None)


class FakeSandboxNoGamemode:
    """Sandbox double without a gamemode method for the disabled path."""

    def __init__(self, *, allow_network: bool = False, allow_game_writes: bool = False) -> None:
        self._kept: list[int] = []
        self.last_arguments: list[str] = []

    def __enter__(self) -> FakeSandboxNoGamemode:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return ()

    def keep(self, descriptor: int) -> int:
        return descriptor

    def runtime(self, executable: Path) -> str:
        return "/runtime/fake"

    def display_probe(self) -> str:
        return "wayland"

    def desktop(self) -> None:
        pass

    def devices(self) -> None:
        pass

    def audio(self) -> None:
        pass

    def persistence(self, paths: AppPaths, game: GameInfo) -> None:
        pass

    def game_saves(self, game: GameInfo, descriptor: int) -> int:
        return descriptor

    def nw_game(self, game: GameInfo, descriptor: int, saves: int) -> None:
        pass

    def bind(self, descriptor: int, destination: str, *, writable: bool = False) -> None:
        pass

    def command(self, arguments: list[str], *, cwd: str = "/") -> list[str]:
        self.last_arguments = list(arguments)
        return ["fake-bwrap", *arguments]


def _nwjs_game(tmp_path: Path) -> GameInfo:
    root = tmp_path / "game"
    root.mkdir()
    entry = root / "index.html"
    entry.write_text("fixture", encoding="utf-8")
    manifest = root / "package.json"
    manifest.write_text('{"name": "fixture"}', encoding="utf-8")
    return GameInfo(EngineName.RPG_MAKER_MZ, root, entry, manifest)


def test_no_proxy_bind_or_env_when_gamemode_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    arch = current_architecture()
    binary = tmp_path / "cache" / "runtimes" / "nwjs" / f"linux-{arch}" / "standard-v0.90.0" / "nw"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    sandboxes: list[FakeSandboxNoGamemode] = []

    def factory(*, allow_network: bool = False, allow_game_writes: bool = False) -> Any:
        sandbox = FakeSandboxNoGamemode(
            allow_network=allow_network, allow_game_writes=allow_game_writes
        )
        sandboxes.append(sandbox)
        return sandbox

    def fake_session(
        paths: AppPaths,
        game: GameInfo,
        copy_root_files: tuple[str, ...] = (),
        *,
        game_descriptor: int | None = None,
    ) -> Any:
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

        class _Session:
            session_descriptor = descriptor
            parent_descriptor = os.dup(descriptor)
            root = tmp_path / "session"
            name = "testsession"
            identifier = "testidentifier"

            def detach(self) -> None:
                pass

            def __enter__(self) -> _Session:
                return self

            def __exit__(self, *args: Any) -> None:
                with suppress(OSError):
                    os.close(self.session_descriptor)
                with suppress(OSError):
                    os.close(self.parent_descriptor)

        return _Session()

    seen: dict[str, Any] = {}

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
    ) -> Any:
        seen["use_gamemode"] = use_gamemode
        seen["gamemode_proxy"] = gamemode_proxy
        from box.launch.supervisor import LaunchedSession

        return LaunchedSession(identifier, name, tmp_path)

    monkeypatch.setattr("box.api.launch.Sandbox", factory)
    monkeypatch.setattr("box.api.launch.create_session", fake_session)
    monkeypatch.setattr("box.api.launch.spawn_detached", fake_spawn)
    monkeypatch.setattr("box.api.launch.detect_game", lambda path, registry: game)
    monkeypatch.delenv("DISPLAY", raising=False)
    handle = launch(paths, repository, game.root, "v0.90.0", False, use_gamemode=False)
    assert handle.identifier
    assert seen == {"use_gamemode": False, "gamemode_proxy": None}
    assert str(GAMEMODERUN) not in sandboxes[0].last_arguments
    assert GAMEMODE_PROXY_SOCKET_NAME not in " ".join(sandboxes[0].last_arguments)


def test_spawn_detached_keeps_backward_compatible_defaults(tmp_path: Path) -> None:
    import inspect

    parameters = inspect.signature(supervisor_module.spawn_detached).parameters
    assert parameters["use_gamemode"].default is False
    assert parameters["gamemode_proxy"].default is None
    main_parameters = inspect.signature(supervisor_module._supervisor_main).parameters
    assert main_parameters["use_gamemode"].default is False
    assert main_parameters["gamemode_proxy"].default is None
