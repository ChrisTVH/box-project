# pyright: reportPrivateUsage=false
"""Parallel GameMode registration repro (no network, no real D-Bus)."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from box.errors import LaunchError
from box.launch import gamemode as gamemode_module
from box.launch import supervisor as supervisor_module
from box.launch.gamemode import GAMEMODE_PROXY_SOCKET_NAME
from box.paths import AppPaths


class _ExitSupervisor(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class _HangingProc:
    """Popen double whose bounded wait always times out."""

    _next_pid = 70000

    def __init__(self) -> None:
        _HangingProc._next_pid += 1
        self.pid = _HangingProc._next_pid
        self.terminated = False
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(cmd=["bwrap"], timeout=timeout)


class _QuickProc:
    """Popen double that exits immediately."""

    _next_pid = 71000

    def __init__(self) -> None:
        _QuickProc._next_pid += 1
        self.pid = _QuickProc._next_pid
        self.terminated = False

    def poll(self) -> int | None:
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


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

    def _keep_none(keep: set[int]) -> None:
        return None

    def _fake_dup2(old: int, new: int) -> None:
        return None

    def _no_sessions(paths: AppPaths, identifier: str) -> list[str]:
        return []

    def _session_idle(paths: AppPaths, identifier: str, name: str) -> bool:
        return False

    def _no_launch_status(paths: AppPaths, identifier: str, name: str) -> int | None:
        return None

    def _record_handler(signum: int, handler: Any) -> Any:
        return state["handlers"].setdefault(signum, handler)

    monkeypatch.setattr(supervisor_module, "_close_extra_fds", _keep_none)
    monkeypatch.setattr(os, "dup2", _fake_dup2)
    monkeypatch.setattr(supervisor_module, "_list_session_names", _no_sessions)
    monkeypatch.setattr(supervisor_module, "is_session_running", _session_idle)
    monkeypatch.setattr(supervisor_module, "poll_launch_status", _no_launch_status)
    monkeypatch.setattr(signal, "signal", _record_handler)

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


def test_gamemode_teardown_wait_timeout_reaches_pipe_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hanging game during gamemode teardown still reports and cleans up."""
    paths, identifier, name, root, parent_fd, session_fd, pipe_write, state = _supervisor_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")

    def failing_register(pid: int, **kwargs: Any) -> None:
        raise LaunchError("cannot register game with GameMode; retry without --gamemode")

    monkeypatch.setattr(supervisor_module._gamemode, "register_host_game", failing_register)
    proxy = _QuickProc()
    bwrap = _HangingProc()

    def fake_popen(args: list[str], **kwargs: Any) -> Any:
        return proxy if str(args[0]).endswith("xdg-dbus-proxy") else bwrap

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor_module, "_wait_for_proxy_socket", lambda path, proc: True)
    terminated_proxies: list[Any] = []

    def _record_terminate_proxy(proc: Any) -> None:
        terminated_proxies.append(proc)

    monkeypatch.setattr(supervisor_module, "_terminate_proxy", _record_terminate_proxy)
    try:
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
    finally:
        os.close(state["read_fd"])
        with suppress(OSError):
            os.close(pipe_write)
    assert excinfo.value.code == 1
    assert bwrap.terminated is True
    assert bwrap.wait_timeouts == [5]
    assert terminated_proxies == [proxy]
    assert state["removed"]
    assert any("GameMode" in message for message in state["pipe_errors"])


def test_parallel_gamemode_register_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent busctl timeouts keep the GameMode cause and stay bounded."""
    monkeypatch.setattr(gamemode_module, "require_bus_client", lambda: str(gamemode_module.BUSCTL))

    def fake_run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert "timeout" in kwargs
        assert kwargs.get("timeout") == gamemode_module.HOST_DBUS_TIMEOUT
        timeout = kwargs.get("timeout", 5.0)
        raise subprocess.TimeoutExpired(cmd=list(args), timeout=timeout)

    monkeypatch.setattr(gamemode_module.subprocess, "run", fake_run)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(gamemode_module.register_host_game, 3000 + i) for i in range(2)]
        causes: list[str] = []
        for future in futures:
            try:
                future.result(timeout=10.0)
            except LaunchError as exc:
                causes.append(str(exc))
    elapsed = time.monotonic() - started
    assert len(causes) == 2
    assert all("GameMode" in cause for cause in causes)
    assert elapsed < supervisor_module._SUPERVISOR_READY_TIMEOUT


@pytest.mark.parametrize("payload", ["i 0\n", "i -1\n", "i -2\n"])
def test_parallel_second_register_payload_status_fails_closed_unless_boosted(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """A RegisterGame reply that leaves the game unboosted must fail closed.

    Upstream gamemoded always answers RegisterGame with a D-Bus success
    carrying an int32 status (``i 0`` registered, ``i -1`` accepted but not
    registered, ``i -2`` rejected), so busctl exits 0 even when the second
    game was never boosted. The returncode-only check lets the supervisor
    send READY and start silently unboosted.

    Manual repro (real games, never automated here): launch Grimm's Hollow
    with ``--gamemode``, then the Summer Memories DLC with ``--ci-mount
    --gamemode``; compare ``busctl --user call com.feralinteractive.GameMode
    /com/feralinteractive/GameMode com.feralinteractive.GameMode ListGames``
    against both bwrap PIDs (``status.json`` ``child_pid`` below
    ``~/.cache/box-rpg/sessions/``). A missing second PID with no launcher
    error is this bug.
    """
    monkeypatch.setattr(gamemode_module, "require_bus_client", lambda: str(gamemode_module.BUSCTL))
    seen_timeouts: list[float | None] = []

    def fake_run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("timeout") == 0.5
        seen_timeouts.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(list(args), 0, stdout=payload, stderr="")

    monkeypatch.setattr(gamemode_module.subprocess, "run", fake_run)
    if payload == "i 0\n":
        gamemode_module.register_host_game(4242, timeout=0.5)
    else:
        with pytest.raises(LaunchError, match="GameMode"):
            gamemode_module.register_host_game(4242, timeout=0.5)
    assert seen_timeouts == [0.5]
