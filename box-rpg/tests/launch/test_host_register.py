"""Host-side GameMode registration tests (no network, no real D-Bus)."""

from __future__ import annotations

import os
import signal
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from box.errors import LaunchError
from box.launch import gamemode as gamemode_module
from box.launch import supervisor as supervisor_module
from box.launch.gamemode import (
    BUSCTL,
    GAMEMODE_BUS_NAME,
    GAMEMODE_INTERFACE,
    GAMEMODE_OBJECT_PATH,
    GAMEMODE_PROXY_SOCKET_NAME,
    is_bus_client_available,
    register_argv,
    register_host_game,
    require_bus_client,
    unregister_argv,
    unregister_host_game,
)
from box.paths import AppPaths


def test_register_argv_exact_order() -> None:
    assert register_argv(4242) == [
        str(BUSCTL),
        "--user",
        "call",
        GAMEMODE_BUS_NAME,
        GAMEMODE_OBJECT_PATH,
        GAMEMODE_INTERFACE,
        "RegisterGame",
        "i",
        "4242",
    ]
    assert Path("/usr/bin/busctl") == BUSCTL
    assert BUSCTL.is_absolute()
    assert GAMEMODE_BUS_NAME == "com.feralinteractive.GameMode"
    assert GAMEMODE_OBJECT_PATH == "/com/feralinteractive/GameMode"
    assert GAMEMODE_INTERFACE == "com.feralinteractive.GameMode"


def test_unregister_argv_exact_order() -> None:
    assert unregister_argv(4242) == [
        str(BUSCTL),
        "--user",
        "call",
        GAMEMODE_BUS_NAME,
        GAMEMODE_OBJECT_PATH,
        GAMEMODE_INTERFACE,
        "UnregisterGame",
        "i",
        "4242",
    ]


@pytest.mark.parametrize("pid", [0, -1, -4242])
def test_register_argv_rejects_non_positive_pid(pid: int) -> None:
    with pytest.raises(ValueError):
        register_argv(pid)
    with pytest.raises(ValueError):
        unregister_argv(pid)


def test_register_argv_rejects_bool_pid() -> None:
    with pytest.raises(ValueError):
        register_argv(True)
    with pytest.raises(ValueError):
        unregister_argv(False)


def test_is_bus_client_available_uses_fixed_path_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_file", lambda self: self == BUSCTL)
    monkeypatch.setattr(os, "access", lambda path, mode: True)
    assert is_bus_client_available() is True
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    assert is_bus_client_available() is False
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.setattr(os, "access", lambda path, mode: True)
    assert is_bus_client_available() is False


def test_require_bus_client_distinct_msgid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    with pytest.raises(LaunchError) as excinfo:
        require_bus_client()
    message = str(excinfo.value)
    assert "busctl" in message
    assert "/usr/bin/busctl" in message
    assert "gamemoderun" not in message
    assert "xdg-dbus-proxy" not in message


def test_require_bus_client_returns_fixed_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_file", lambda self: self == BUSCTL)
    monkeypatch.setattr(os, "access", lambda path, mode: True)
    assert require_bus_client() == str(BUSCTL)


def _completed(args: list[str], code: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, code)


def test_register_host_game_success_uses_busctl_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gamemode_module, "is_bus_client_available", lambda: True)
    monkeypatch.setattr(gamemode_module, "require_bus_client", lambda: str(BUSCTL))
    seen: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["args"] = list(args)
        seen["kwargs"] = dict(kwargs)
        assert kwargs.get("close_fds") is True
        assert kwargs.get("stdin") is subprocess.DEVNULL
        assert kwargs.get("stdout") is subprocess.DEVNULL
        assert kwargs.get("stderr") is subprocess.DEVNULL
        assert kwargs.get("shell", False) is False
        assert "timeout" in kwargs
        return _completed(list(args), 0)

    monkeypatch.setattr(gamemode_module.subprocess, "run", fake_run)
    register_host_game(4242)
    assert seen["args"] == register_argv(4242)


@pytest.mark.parametrize("failure", ["nonzero", "oserror", "timeout"])
def test_register_host_game_fails_closed(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    monkeypatch.setattr(gamemode_module, "is_bus_client_available", lambda: True)
    monkeypatch.setattr(gamemode_module, "require_bus_client", lambda: str(BUSCTL))

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure == "nonzero":
            return _completed(list(args), 1)
        if failure == "oserror":
            raise OSError("bus down")
        raise subprocess.TimeoutExpired(cmd=args, timeout=5.0)

    monkeypatch.setattr(gamemode_module.subprocess, "run", fake_run)
    with pytest.raises(LaunchError, match="cannot register game with GameMode"):
        register_host_game(4242)


def test_register_host_game_fails_closed_when_bus_client_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gamemode_module, "is_bus_client_available", lambda: False)

    def missing() -> str:
        raise LaunchError(
            "GameMode requires busctl (/usr/bin/busctl); "
            "install systemd or retry without --gamemode"
        )

    monkeypatch.setattr(gamemode_module, "require_bus_client", missing)

    def forbidden_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("missing bus client must not run")

    monkeypatch.setattr(gamemode_module.subprocess, "run", forbidden_run)
    with pytest.raises(LaunchError, match="busctl"):
        register_host_game(4242)


def test_unregister_host_game_success_and_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gamemode_module, "is_bus_client_available", lambda: True)
    monkeypatch.setattr(
        gamemode_module.subprocess, "run", lambda args, **kwargs: _completed(list(args), 0)
    )
    assert unregister_host_game(4242) is True
    monkeypatch.setattr(
        gamemode_module.subprocess, "run", lambda args, **kwargs: _completed(list(args), 3)
    )
    assert unregister_host_game(4242) is False

    def failing_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("bus down")

    monkeypatch.setattr(gamemode_module.subprocess, "run", failing_run)
    assert unregister_host_game(4242) is False
    assert unregister_host_game(0) is False
    assert unregister_host_game(-5) is False
    monkeypatch.setattr(gamemode_module, "is_bus_client_available", lambda: False)
    assert unregister_host_game(4242) is False


class _ExitSupervisor(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class FakeProc:
    """Minimal Popen double recording termination requests."""

    _next_pid = 60000

    def __init__(self, *, already_exited: bool = False) -> None:
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.terminated = False
        self.killed = False
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
        if self.terminated or self.killed:
            self._exited = True
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
    state: dict[str, Any] = {"pipe_errors": [], "removed": [], "handlers": {}}
    monkeypatch.setattr(supervisor_module, "_close_extra_fds", lambda keep: None)
    monkeypatch.setattr(os, "dup2", lambda old, new: None)
    monkeypatch.setattr(supervisor_module, "_list_session_names", lambda p, i: [])
    monkeypatch.setattr(supervisor_module, "is_session_running", lambda p, i, n: False)
    monkeypatch.setattr(supervisor_module, "poll_launch_status", lambda p, i, n: None)
    monkeypatch.setattr(
        signal, "signal", lambda signum, handler: state["handlers"].setdefault(signum, handler)
    )

    def fake_pipe_error(pipe: int, message: str) -> None:
        state["pipe_errors"].append(message)

    def fake_remove(p: Any, r: Path, parent_descriptor: int, session_descriptor: int) -> None:
        state["removed"].append(r)

    def fake_exit(code: int) -> None:
        raise _ExitSupervisor(code)

    monkeypatch.setattr(supervisor_module, "_pipe_error", fake_pipe_error)
    monkeypatch.setattr(supervisor_module, "_remove_supervised", fake_remove)
    monkeypatch.setattr(os, "_exit", fake_exit)
    state["read_fd"] = read_fd
    return paths, identifier, name, root, parent_fd, session_fd, write_fd, state


def _mock_gamemode_registration(
    monkeypatch: pytest.MonkeyPatch,
    *,
    register_side_effect: Any = None,
    unregister_side_effect: Any = None,
) -> tuple[list[int], list[int]]:
    registered: list[int] = []
    unregistered: list[int] = []

    def fake_register(pid: int, **kwargs: Any) -> None:
        registered.append(pid)
        if isinstance(register_side_effect, BaseException):
            raise register_side_effect

    def fake_unregister(pid: int, **kwargs: Any) -> bool:
        unregistered.append(pid)
        if isinstance(unregister_side_effect, BaseException):
            raise unregister_side_effect
        return True

    monkeypatch.setattr(supervisor_module._gamemode, "register_host_game", fake_register)
    monkeypatch.setattr(supervisor_module._gamemode, "unregister_host_game", fake_unregister)
    return registered, unregistered


def test_supervisor_registers_host_bwrap_pid_and_unregisters_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    registered, unregistered = _mock_gamemode_registration(monkeypatch)
    proxy = FakeProc()
    bwrap = FakeProc(already_exited=True)
    order: list[str] = []
    real_terminate = supervisor_module._terminate_proxy

    def tracking_terminate(proc: Any) -> None:
        order.append("terminate-proxy")
        real_terminate(proc)

    def tracking_unregister(pid: int, **kwargs: Any) -> bool:
        order.append("unregister")
        unregistered.append(pid)
        return True

    monkeypatch.setattr(supervisor_module, "_terminate_proxy", tracking_terminate)
    monkeypatch.setattr(supervisor_module._gamemode, "unregister_host_game", tracking_unregister)
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
    # Host PID (bwrap parent view), never an in-sandbox PID: the supervisor
    # registers exactly proc.pid it just spawned.
    assert registered == [bwrap.pid]
    assert unregistered == [bwrap.pid]
    assert order == ["unregister", "terminate-proxy"]
    assert state["removed"]
    os.close(state["read_fd"])
    with suppress(OSError):
        os.close(pipe_write)


def test_supervisor_fails_closed_when_bus_client_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")

    def missing() -> str:
        raise LaunchError(
            "GameMode requires busctl (/usr/bin/busctl); "
            "install systemd or retry without --gamemode"
        )

    monkeypatch.setattr(supervisor_module._gamemode, "require_bus_client", missing)
    calls: list[list[str]] = []

    def fake_popen(args: list[str], **kwargs: Any) -> FakeProc:
        calls.append(list(args))
        raise AssertionError("missing bus client must not spawn")

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
    assert excinfo.value.code == 1
    assert calls == []
    assert state["removed"]
    assert any("busctl" in message for message in state["pipe_errors"])
    os.close(state["read_fd"])
    with suppress(OSError):
        os.close(pipe_write)


def test_supervisor_fails_closed_when_host_register_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    registered, unregistered = _mock_gamemode_registration(
        monkeypatch,
        register_side_effect=LaunchError(
            "cannot register game with GameMode; retry without --gamemode"
        ),
    )
    proxy = FakeProc()
    bwrap = FakeProc(already_exited=True)
    terminated: list[Any] = []
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
    assert excinfo.value.code == 1
    assert registered == [bwrap.pid]
    assert unregistered == []
    assert terminated == [proxy]
    assert bwrap.terminated is True
    assert state["removed"]
    assert any("GameMode" in message for message in state["pipe_errors"])
    os.close(state["read_fd"])
    with suppress(OSError):
        os.close(pipe_write)


def test_supervisor_unregisters_when_ready_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    registered, unregistered = _mock_gamemode_registration(monkeypatch)
    proxy = FakeProc()
    bwrap = FakeProc(already_exited=True)
    monkeypatch.setattr(supervisor_module, "_terminate_proxy", lambda proc: None)
    monkeypatch.setattr(
        supervisor_module, "_wait_for_proxy_socket", lambda path, proc, timeout=2.0: True
    )

    def fake_popen(args: list[str], **kwargs: Any) -> FakeProc:
        return proxy if str(args[0]).endswith("xdg-dbus-proxy") else bwrap

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)

    def failing_write(fd: int, data: bytes) -> int:
        raise OSError("pipe broken")

    monkeypatch.setattr(os, "write", failing_write)
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
    assert registered == [bwrap.pid]
    assert unregistered == [bwrap.pid]
    os.close(state["read_fd"])
    with suppress(OSError):
        os.close(pipe_write)


def test_supervisor_skips_host_registration_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    registered, unregistered = _mock_gamemode_registration(monkeypatch)
    bwrap = FakeProc(already_exited=True)
    monkeypatch.setattr(supervisor_module, "_terminate_proxy", lambda proc: None)
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", lambda args, **kw: bwrap)
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
            use_gamemode=False,
        )
    assert excinfo.value.code == 0
    assert registered == []
    assert unregistered == []
    os.close(state["read_fd"])
    with suppress(OSError):
        os.close(pipe_write)


def test_supervisor_unregister_failure_stays_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    registered, _ = _mock_gamemode_registration(
        monkeypatch, unregister_side_effect=RuntimeError("bus down")
    )
    proxy = FakeProc()
    bwrap = FakeProc(already_exited=True)
    monkeypatch.setattr(supervisor_module, "_terminate_proxy", lambda proc: None)
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
    assert registered == [bwrap.pid]
    os.close(state["read_fd"])
    with suppress(OSError):
        os.close(pipe_write)
