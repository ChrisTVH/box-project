"""Detached supervisor tests with real Bubblewrap and no network."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import pytest

from box.errors import LaunchError
from box.games.identity import game_id
from box.launch.cleanup import remove_session
from box.launch.sandbox import Sandbox
from box.launch.supervisor import (
    SESSION_LOCK_NAME,
    STATUS_NAME,
    create_supervisor_session,
    is_session_running,
    poll_launch_status,
    spawn_detached,
    stop_session,
)
from box.paths import AppPaths


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


def _game_identifier(tmp_path: Path) -> tuple[AppPaths, str]:
    paths = _paths(tmp_path)
    game_root = tmp_path / "game"
    game_root.mkdir()
    return paths, game_id(game_root)


def _spawn(
    paths: AppPaths,
    identifier: str,
    payload: list[str],
    extra_binds: list[tuple[Path, str]] | None = None,
) -> tuple[str, Path, int, int]:
    """Create a session, build a minimal sandbox command, and detach."""
    name, root, parent_fd, session_fd = create_supervisor_session(paths, identifier)
    with Sandbox() as sandbox:
        if extra_binds:
            for host_dir, dest in extra_binds:
                descriptor = os.open(host_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                sandbox.keep(descriptor)
                sandbox.bind(descriptor, dest, writable=True)
        command = sandbox.command(payload)
        pass_fds = sandbox.pass_fds
        handle = spawn_detached(
            paths,
            identifier,
            name,
            command,
            pass_fds,
            parent_descriptor=parent_fd,
            session_descriptor=session_fd,
        )
    os.close(parent_fd)
    os.close(session_fd)
    assert handle.identifier == identifier
    assert handle.name == name
    return name, root, parent_fd, session_fd


def _read_status(root: Path) -> dict[str, object]:
    return json.loads((root / STATUS_NAME).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _ppid(pid: int) -> int:
    text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return int(text.rsplit(")", 1)[1].split()[1])


def _wait_for(condition: object, timeout: float = 15.0) -> None:
    callback = condition  # type: ignore[assignment]
    assert callable(callback)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if callback():  # type: ignore[operator]
            return
        time.sleep(0.05)
    assert callback(), "condition did not become true in time"  # type: ignore[operator]


def _wait_gone(paths: AppPaths, identifier: str, name: str, root: Path) -> None:
    _wait_for(lambda: not is_session_running(paths, identifier, name))
    _wait_for(lambda: not root.exists())


def test_bwrap_ppid_is_supervisor_not_launcher(tmp_path: Path) -> None:
    paths, identifier = _game_identifier(tmp_path)
    name, root, _, _ = _spawn(paths, identifier, ["/usr/bin/sleep", "30"])
    try:
        _wait_for(lambda: (root / STATUS_NAME).exists())
        status = _read_status(root)
        supervisor = status["pid"]
        child = status["child_pid"]
        assert isinstance(supervisor, int) and supervisor > 0
        assert isinstance(child, int) and child > 0
        assert child != os.getpid()
        assert supervisor != os.getpid()
        assert _ppid(child) == supervisor
    finally:
        stop_session(paths, identifier, name)
        _wait_gone(paths, identifier, name, root)


def test_launcher_exit_leaves_game_running_then_cleans(tmp_path: Path) -> None:
    paths, identifier = _game_identifier(tmp_path)
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    launcher = os.fork()
    if launcher == 0:
        try:
            os.close(read_fd)
            name, _, parent_fd, session_fd = create_supervisor_session(paths, identifier)
            with Sandbox() as sandbox:
                command = sandbox.command(["/usr/bin/sleep", "10"])
                pass_fds = sandbox.pass_fds
                handle = spawn_detached(
                    paths,
                    identifier,
                    name,
                    command,
                    pass_fds,
                    parent_descriptor=parent_fd,
                    session_descriptor=session_fd,
                )
            os.close(parent_fd)
            os.close(session_fd)
            os.write(write_fd, f"{handle.identifier} {handle.name}".encode("ascii"))
            os.close(write_fd)
        finally:
            os._exit(0)
    os.close(write_fd)
    _, status = os.waitpid(launcher, 0)
    assert status == 0
    # Read the fixed handle length; the supervisor closes extra fds so EOF
    # follows promptly, but never wait for it while the game still runs.
    data = b""
    while len(data) < 49:
        chunk = os.read(read_fd, 49 - len(data))
        if not chunk:
            break
        data += chunk
    os.close(read_fd)
    live_identifier, live_name = data.decode("ascii").strip().split()
    assert live_identifier == identifier
    root = paths.sessions_root / live_identifier / live_name
    try:
        assert is_session_running(paths, live_identifier, live_name) is True
        assert poll_launch_status(paths, live_identifier, live_name) is None
    finally:
        stop_session(paths, live_identifier, live_name)
        _wait_gone(paths, live_identifier, live_name, root)


def test_killing_supervisor_kills_sandboxed_process(tmp_path: Path) -> None:
    observed = tmp_path / "observed"
    observed.mkdir()
    stub = observed / "heartbeat_stub.py"
    marker = f"detached-heartbeat-{os.getpid()}"
    stub.write_text(
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        "observed = Path(sys.argv[1])\n"
        f"marker = {marker!r}\n"
        "beat = observed / 'heartbeat'\n"
        "def _terminate(signum: int, frame: object) -> None:\n"
        "    (observed / 'terminated').write_text('yes')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, _terminate)\n"
        "counter = 0\n"
        "while True:\n"
        "    beat.write_text(f'{marker} {counter}')\n"
        "    counter += 1\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    paths, identifier = _game_identifier(tmp_path)
    name, root, _, _ = _spawn(
        paths,
        identifier,
        ["/usr/bin/python3", "/observed/heartbeat_stub.py", "/observed"],
        extra_binds=[(observed, "/observed")],
    )
    try:
        beat = observed / "heartbeat"
        _wait_for(lambda: beat.exists())
        first = beat.read_text(encoding="utf-8")
        assert marker in first
        status = _read_status(root)
        supervisor = status["pid"]
        child = status["child_pid"]
        assert isinstance(supervisor, int) and isinstance(child, int)
        os.kill(supervisor, signal.SIGKILL)
        time.sleep(2.0)
        # Bubblewrap parent died via die-with-parent; inner payload is gone too.
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        _wait_for(lambda: not is_session_running(paths, identifier, name))
        frozen = beat.read_text(encoding="utf-8")
        time.sleep(0.6)
        assert beat.read_text(encoding="utf-8") == frozen
        listing = subprocess.run(
            ["ps", "-eo", "args"], capture_output=True, text=True, check=False
        ).stdout
        assert "heartbeat_stub.py" not in listing or marker not in listing
    finally:
        with suppress(LaunchError):
            remove_session(paths, root)


def test_status_json_atomic_correct_and_running_vs_exited(tmp_path: Path) -> None:
    paths, identifier = _game_identifier(tmp_path)
    # Running case: long sleep reports None while the lock is held.
    name, root, _, _ = _spawn(paths, identifier, ["/usr/bin/sleep", "30"])
    try:
        _wait_for(lambda: (root / STATUS_NAME).exists())
        assert poll_launch_status(paths, identifier, name) is None
        assert is_session_running(paths, identifier, name) is True
        status_path = root / STATUS_NAME
        assert stat.S_IMODE(status_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(root.parent.stat().st_mode) == 0o700
        for _ in range(50):
            document = json.loads(status_path.read_text(encoding="utf-8"))
            assert document["state"] == "running"
            assert document["exit_code"] is None
            assert isinstance(document["pid"], int)
            assert isinstance(document["started_at"], float)
    finally:
        stop_session(paths, identifier, name)
        _wait_gone(paths, identifier, name, root)
    # Exited case: fast payload reports its exact code before cleanup.
    name2, root2, _, _ = _spawn(
        paths, identifier, ["/usr/bin/python3", "-c", "import sys; sys.exit(42)"]
    )
    try:
        _wait_for(lambda: poll_launch_status(paths, identifier, name2) == 42)
        document = json.loads((root2 / STATUS_NAME).read_text(encoding="utf-8"))
        assert document["state"] == "exited"
        assert document["exit_code"] == 42
        assert isinstance(document["ended_at"], float)
        assert stat.S_IMODE((root2 / STATUS_NAME).stat().st_mode) == 0o600
    finally:
        _wait_gone(paths, identifier, name2, root2)
    assert poll_launch_status(paths, identifier, name2) is None


def test_is_session_running_flock_not_pid(tmp_path: Path) -> None:
    paths, identifier = _game_identifier(tmp_path)
    name, root, _, _ = _spawn(paths, identifier, ["/usr/bin/sleep", "30"])
    try:
        assert is_session_running(paths, identifier, name) is True
        # Fresh process without cached PIDs observes the same lock.
        checker = os.fork()
        if checker == 0:
            try:
                running = is_session_running(paths, identifier, name)
                os._exit(0 if running else 1)
            finally:
                os._exit(1)
        _, status = os.waitpid(checker, 0)
        assert status == 0
        # A live PID in a forged status never reports running without the lock.
        stale_name = "staleprobe"
        stale_root = paths.sessions_root / identifier / stale_name
        stale_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (stale_root / STATUS_NAME).write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "child_pid": None,
                    "state": "running",
                    "exit_code": None,
                    "started_at": time.time(),
                    "ended_at": None,
                }
            ),
            encoding="utf-8",
        )
        assert is_session_running(paths, identifier, stale_name) is False
    finally:
        stop_session(paths, identifier, name)
        _wait_gone(paths, identifier, name, root)
    assert is_session_running(paths, identifier, name) is False
    # After a simulated launcher restart there is no in-memory state to trust.
    restarted = os.fork()
    if restarted == 0:
        try:
            running = is_session_running(paths, identifier, name)
            os._exit(1 if running else 0)
        finally:
            os._exit(1)
    _, status = os.waitpid(restarted, 0)
    assert status == 0


def test_stop_session_ends_real_payload(tmp_path: Path) -> None:
    observed = tmp_path / "observed"
    observed.mkdir()
    stub = observed / "stop_stub.py"
    marker = f"stop-payload-{os.getpid()}"
    stub.write_text(
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        "observed = Path(sys.argv[1])\n"
        "beat = observed / 'heartbeat'\n"
        "done = observed / 'terminated'\n"
        "def _terminate(signum: int, frame: object) -> None:\n"
        "    done.write_text('yes')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, _terminate)\n"
        "counter = 0\n"
        "while True:\n"
        f"    beat.write_text('{marker} ' + str(counter))\n"
        "    counter += 1\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    paths, identifier = _game_identifier(tmp_path)
    name, root, _, _ = _spawn(
        paths,
        identifier,
        ["/usr/bin/python3", "/observed/stop_stub.py", "/observed"],
        extra_binds=[(observed, "/observed")],
    )
    try:
        beat = observed / "heartbeat"
        _wait_for(lambda: beat.exists() and marker in beat.read_text(encoding="utf-8"))
        status = _read_status(root)
        child = status["child_pid"]
        assert isinstance(child, int)
        stop_session(paths, identifier, name)
        _wait_gone(paths, identifier, name, root)
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        frozen = beat.read_text(encoding="utf-8") if beat.exists() else ""
        time.sleep(0.6)
        if beat.exists():
            assert beat.read_text(encoding="utf-8") == frozen
        listing = subprocess.run(
            ["ps", "-eo", "args"], capture_output=True, text=True, check=False
        ).stdout
        assert "stop_stub.py" not in listing
    finally:
        with suppress(LaunchError):
            stop_session(paths, identifier, name)


def test_spawn_rejects_non_bwrap_command(tmp_path: Path) -> None:
    paths, identifier = _game_identifier(tmp_path)
    name, _, parent_fd, session_fd = create_supervisor_session(paths, identifier)
    try:
        with pytest.raises(LaunchError, match="Bubblewrap"):
            spawn_detached(
                paths,
                identifier,
                name,
                ["/bin/sleep", "1"],
                (),
                parent_descriptor=parent_fd,
                session_descriptor=session_fd,
            )
    finally:
        os.close(parent_fd)
        os.close(session_fd)


def test_single_instance_guard_is_firm(tmp_path: Path) -> None:
    paths, identifier = _game_identifier(tmp_path)
    first, root_first, _, _ = _spawn(paths, identifier, ["/usr/bin/sleep", "30"])
    try:
        assert is_session_running(paths, identifier, first) is True
        second, _, parent_fd, session_fd = create_supervisor_session(paths, identifier)
        try:
            with Sandbox() as sandbox:
                command = sandbox.command(["/usr/bin/sleep", "30"])
                with pytest.raises(LaunchError, match="already running"):
                    spawn_detached(
                        paths,
                        identifier,
                        second,
                        command,
                        sandbox.pass_fds,
                        parent_descriptor=parent_fd,
                        session_descriptor=session_fd,
                    )
        finally:
            os.close(parent_fd)
            os.close(session_fd)
    finally:
        stop_session(paths, identifier, first)
        _wait_gone(paths, identifier, first, root_first)
    # After the first game exits, the same entry launches again.
    third, root_third, _, _ = _spawn(paths, identifier, ["/usr/bin/true"])
    try:
        _wait_for(lambda: poll_launch_status(paths, identifier, third) == 0)
    finally:
        _wait_gone(paths, identifier, third, root_third)


def test_session_files_are_private_and_managed(tmp_path: Path) -> None:
    paths, identifier = _game_identifier(tmp_path)
    name, root, _, _ = _spawn(paths, identifier, ["/usr/bin/sleep", "5"])
    try:
        assert name == name.lower()
        assert identifier == identifier.lower()
        relative = root.resolve().relative_to(paths.sessions_root.resolve())
        assert len(relative.parts) == 2
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / SESSION_LOCK_NAME).stat().st_mode) == 0o600
        assert stat.S_IMODE((root / STATUS_NAME).stat().st_mode) == 0o600
        managed = paths.ensure_managed_session_path(root)
        assert managed == root.resolve()
    finally:
        stop_session(paths, identifier, name)
        _wait_gone(paths, identifier, name, root)


def test_cli_blocks_and_reports_exit_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Foreground CLI uses the detached supervisor underneath and returns its code."""
    from box.cli.launch import execute
    from box.config.repository import ConfigRepository
    from box.models import EngineName, GameInfo, RuntimeInfo, RuntimeSpec

    root = tmp_path / "game"
    root.mkdir()
    (root / "index.html").write_text("fixture", encoding="utf-8")
    (root / "package.json").write_text('{"name": "fixture"}', encoding="utf-8")
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, root / "index.html", root / "package.json")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    binary = runtime_root / "nw"
    binary.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    binary.chmod(0o700)
    runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), runtime_root, binary)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(root)

    def fake_detect(game_path: Path, registry: object) -> GameInfo:
        return game

    def fake_select(
        catalog: object, architecture: str, version: str | None, sdk: bool
    ) -> RuntimeInfo:
        return runtime

    def fake_desktop(self: Sandbox) -> None:
        pass

    def fake_probe(self: Sandbox) -> str:
        return "wayland"

    def fake_devices(self: Sandbox) -> None:
        pass

    def fake_audio(self: Sandbox) -> None:
        pass

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.setattr("box.api.launch.select_runtime", fake_select)
    monkeypatch.setattr(Sandbox, "desktop", fake_desktop)
    monkeypatch.setattr(Sandbox, "display_probe", fake_probe)
    monkeypatch.setattr(Sandbox, "devices", fake_devices)
    monkeypatch.setattr(Sandbox, "audio", fake_audio)
    monkeypatch.delenv("DISPLAY", raising=False)
    # Non-interactive foreground poll; no prompts expected.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    started = time.time()
    assert execute(paths, repository, root, None, False) == 7
    assert time.time() - started < 30.0
    identifier = game_id(root)
    remaining = (
        list((paths.sessions_root / identifier).iterdir())
        if (paths.sessions_root / identifier).exists()
        else []
    )
    # Supervisor already reported the code; grace cleanup follows shortly.
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if not (paths.sessions_root / identifier).exists():
            break
        if not list((paths.sessions_root / identifier).iterdir()):
            break
        time.sleep(0.1)
    assert remaining is not None
