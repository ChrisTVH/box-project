"""Detached supervision for sandboxed game processes.

The launcher double-forks a supervisor that becomes the Bubblewrap parent,
so the game survives launcher exit. Liveness uses an exclusive flock on
``session.lock`` (PID-reuse safe, never a bare PID file). Exit status is
reported through an atomically written ``status.json`` that the launcher
owns and the sandbox can never write.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import select
import signal
import stat
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from box.errors import ConfigurationError, LaunchError
from box.launch import cimount as _cimount
from box.launch import gamemode as _gamemode
from box.launch.process import runtime_environment
from box.launch.sandbox import BWRAP
from box.paths import AppPaths
from box.utils.i18n import _

SESSION_LOCK_NAME = "session.lock"
STATUS_NAME = "status.json"
STATUS_LIMIT = 64 * 1024
STATUS_GRACE_SECONDS = 5.0
_SUPERVISOR_READY_TIMEOUT = 15.0
_GAMEMODE_PROXY_READY_TIMEOUT = 2.0
_GAMEMODE_PROXY_STOP_TIMEOUT = 5.0

_STATE_RUNNING = "running"
_STATE_EXITED = "exited"


@dataclass(frozen=True, slots=True)
class LaunchedSession:
    """Handle returned once a detached supervisor confirmed startup.

    ``identifier`` is the stable per-game directory name below
    ``sessions_root`` and ``name`` is the per-launch directory name below
    it. Poll ``poll_launch_status`` with this handle; it returns the game
    exit code once the game exits and ``None`` while it runs.
    """

    identifier: str
    name: str
    root: Path


def _validate_component(value: str, label: str) -> str:
    if not value or Path(value).name != value or value in {"", ".", ".."}:
        raise LaunchError(_("refusing to manage invalid session path"))
    if value != value.lower():
        raise LaunchError(_("refusing to manage invalid session path"))
    if label:
        return value
    return value


def session_root(paths: AppPaths, identifier: str, name: str) -> Path:
    """Return the validated depth-2 session directory for one handle."""
    _validate_component(identifier, "identifier")
    _validate_component(name, "name")
    root = paths.sessions_root / identifier / name
    try:
        managed = paths.ensure_managed_session_path(root)
    except ConfigurationError as exc:
        raise LaunchError(str(exc)) from exc
    try:
        relative = managed.relative_to(paths.sessions_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise LaunchError(_("refusing to manage invalid session path")) from exc
    if len(relative.parts) != 2:
        raise LaunchError(_("refusing to manage invalid session path"))
    return managed


def _lock_path(paths: AppPaths, identifier: str, name: str) -> Path | None:
    try:
        return session_root(paths, identifier, name) / SESSION_LOCK_NAME
    except LaunchError:
        return None


def _status_path(paths: AppPaths, identifier: str, name: str) -> Path | None:
    try:
        return session_root(paths, identifier, name) / STATUS_NAME
    except LaunchError:
        return None


def is_session_running(paths: AppPaths, identifier: str, name: str) -> bool:
    """Report liveness via flock; True only while a supervisor holds the lock."""
    lock = _lock_path(paths, identifier, name)
    if lock is None:
        return False
    try:
        descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return False
        raise LaunchError(_("cannot check launch session: {error}").format(error=exc)) from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EACCES):
                return True
            raise LaunchError(_("cannot check launch session: {error}").format(error=exc)) from exc
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _read_status_document(path: Path) -> dict[str, object] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        if metadata.st_size > STATUS_LIMIT:
            return None
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            data = stream.read(STATUS_LIMIT + 1)
    except OSError:
        return None
    if len(data) > STATUS_LIMIT:
        return None
    try:
        decoded: object = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    return {str(key): value for key, value in cast("dict[str, object]", decoded).items()}


def poll_launch_status(paths: AppPaths, identifier: str, name: str) -> int | None:
    """Return the game exit code, or None while the game runs.

    Reads ``status.json`` with bounded typed checks. Missing, oversized,
    corrupt, or ``running`` documents report None. Only an ``exited``
    document with an integer exit code reports that code.
    """
    status = _status_path(paths, identifier, name)
    if status is None:
        return None
    document = _read_status_document(status)
    if document is None:
        return None
    state = document.get("state")
    if state != _STATE_EXITED:
        return None
    exit_code = document.get("exit_code")
    if type(exit_code) is not int:
        return None
    if not -255 <= exit_code <= 255:
        return None
    return exit_code


def stop_session(paths: AppPaths, identifier: str, name: str) -> None:
    """Send SIGTERM to a live supervisor and its Bubblewrap child.

    No-op when no supervisor holds the session lock, which keeps stale
    PID reuse safe: a dead supervisor never causes a signal to an
    unrelated process.
    """
    if not is_session_running(paths, identifier, name):
        return
    status = _status_path(paths, identifier, name)
    if status is None:
        return
    document = _read_status_document(status) if status.exists() else None
    if document is None:
        raise LaunchError(_("cannot stop launch session without supervisor status"))
    pid = document.get("pid")
    if type(pid) is not int or pid <= 0:
        raise LaunchError(_("cannot stop launch session without supervisor status"))
    # Re-check the lock just before signalling to narrow PID reuse races.
    if not is_session_running(paths, identifier, name):
        return
    child = document.get("child_pid")
    for target in (pid, child if type(child) is int and child > 0 else None):
        if target is None:
            continue
        try:
            _signal_pid(target)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise LaunchError(_("cannot stop launch session: {error}").format(error=exc)) from exc
        except OSError as exc:
            raise LaunchError(_("cannot stop launch session: {error}").format(error=exc)) from exc


def _signal_pid(pid: int) -> None:
    """Send SIGTERM pinned to one PID, immune to PID reuse when possible.

    Uses pidfd_send_signal when available so a PID recycled between the
    liveness check and the kill cannot receive the signal. Falls back to
    os.kill on platforms without pidfd support.
    """
    pidfd_open: Callable[[int, int], int] | None = getattr(os, "pidfd_open", None)
    pidfd_send: Callable[..., None] | None = getattr(os, "pidfd_send_signal", None)
    if pidfd_open is not None and pidfd_send is not None:
        fd = pidfd_open(pid, 0)
        try:
            pidfd_send(fd, signal.SIGTERM, None, None, 0)
        finally:
            with suppress(OSError):
                os.close(fd)
        return
    os.kill(pid, signal.SIGTERM)


def _list_session_names(paths: AppPaths, identifier: str) -> list[str]:
    try:
        _validate_component(identifier, "identifier")
        parent = paths.sessions_root / identifier
        managed = paths.ensure_managed_session_path(parent)
    except LaunchError, ConfigurationError:
        return []
    try:
        entries = os.listdir(managed)
    except OSError:
        return []
    names: list[str] = []
    for entry in entries:
        if not entry or Path(entry).name != entry or entry in {"", ".", ".."}:
            continue
        if entry != entry.lower():
            continue
        if entry in (SESSION_LOCK_NAME, STATUS_NAME):
            continue
        candidate = managed / entry
        try:
            if not candidate.is_dir() or candidate.is_symlink():
                continue
        except OSError:
            continue
        names.append(entry)
    return sorted(names)


def find_live_sessions(paths: AppPaths, identifier: str) -> list[str]:
    """Return live session names below one game identifier.

    A session counts as live only while its supervisor holds the flock
    and its status still reports running. Supervisors in post-exit grace
    (lock held, status exited) do not block a new launch.
    """
    live: list[str] = []
    for name in _list_session_names(paths, identifier):
        if not is_session_running(paths, identifier, name):
            continue
        if poll_launch_status(paths, identifier, name) is not None:
            continue
        live.append(name)
    return live


def check_no_live_session(paths: AppPaths, identifier: str) -> None:
    """Enforce one session per game entry; raise when a game already runs."""
    if find_live_sessions(paths, identifier):
        raise LaunchError(_("game already running"))


def create_supervisor_session(paths: AppPaths, identifier: str) -> tuple[str, Path, int, int]:
    """Create one private depth-2 session directory for EasyRPG launches."""
    _validate_component(identifier, "identifier")
    paths.ensure()
    try:
        parent = paths.ensure_managed_session_path(paths.sessions_root / identifier)
    except ConfigurationError as exc:
        raise LaunchError(str(exc)) from exc
    parent_descriptor = paths.open_or_create_private_cache_directory("sessions", identifier)
    try:
        for _attempt in range(8):
            name = uuid4().hex
            _validate_component(name, "name")
            try:
                os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                continue
            except OSError as exc:
                raise LaunchError(
                    _("cannot create launch session: {error}").format(error=exc)
                ) from exc
            try:
                session_descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_descriptor,
                )
            except OSError as exc:
                with suppress(OSError):
                    os.rmdir(name, dir_fd=parent_descriptor)
                raise LaunchError(
                    _("cannot create launch session: {error}").format(error=exc)
                ) from exc
            try:
                metadata = os.fstat(session_descriptor)
                if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise LaunchError(_("launch session directory has unsafe ownership"))
            except Exception:
                os.close(session_descriptor)
                with suppress(OSError):
                    os.rmdir(name, dir_fd=parent_descriptor)
                raise
            root = parent / name
            try:
                paths.ensure_managed_session_path(root)
            except ConfigurationError as exc:
                os.close(session_descriptor)
                with suppress(OSError):
                    os.rmdir(name, dir_fd=parent_descriptor)
                raise LaunchError(str(exc)) from exc
            return name, root, parent_descriptor, session_descriptor
        raise LaunchError(_("cannot create launch session: name collision"))
    except Exception:
        os.close(parent_descriptor)
        raise


def _write_status_atomic(root: Path, document: dict[str, object]) -> None:
    payload = (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > STATUS_LIMIT:
        raise LaunchError(_("cannot record launch status"))
    descriptor, temporary_name = _mkstemp_in(root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, root / STATUS_NAME)
        with suppress(OSError):
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        if temporary.exists():
            with suppress(OSError):
                temporary.unlink()


def _mkstemp_in(root: Path) -> tuple[int, str]:
    import tempfile

    # mkstemp creates 0600; keep the writer pattern (mkstemp/fsync/chmod/replace).
    return tempfile.mkstemp(prefix=".status-", suffix=".tmp", dir=root)


def _initial_status(supervisor_pid: int) -> dict[str, object]:
    return {
        "pid": supervisor_pid,
        "child_pid": None,
        "state": _STATE_RUNNING,
        "exit_code": None,
        "started_at": time.time(),
        "ended_at": None,
    }


def _pipe_error(pipe_write: int, message: str) -> None:
    with suppress(OSError):
        os.write(pipe_write, f"ERROR {message}\n".encode("utf-8", "replace"))


def _terminate_proxy(proxy: subprocess.Popen[bytes] | None) -> None:
    """Stop the GameMode proxy without ever propagating its errors."""
    if proxy is None:
        return
    if proxy.poll() is not None:
        with suppress(OSError, ValueError):
            proxy.wait()
        return
    with suppress(OSError):
        proxy.terminate()
    with suppress(OSError, subprocess.TimeoutExpired):
        proxy.wait(timeout=_GAMEMODE_PROXY_STOP_TIMEOUT)
    if proxy.poll() is None:
        with suppress(OSError):
            proxy.kill()
        with suppress(OSError, subprocess.TimeoutExpired, ValueError):
            proxy.wait(timeout=_GAMEMODE_PROXY_STOP_TIMEOUT)


def _teardown_ci_mount(mountpoint: Path | None) -> None:
    """Detach a case-insensitive mount without ever propagating its errors.

    Runs after the game exits and the GameMode proxy is terminated. There is
    no terminal I/O available here and the supervisor always ends via
    os._exit, so failures stay silent best-effort like _terminate_proxy.
    Only the mountpoint path is needed; no descriptor ever crosses into the
    supervisor.
    """
    if mountpoint is None:
        return
    with suppress(Exception):
        _cimount.force_unmount(mountpoint)


def _wait_for_proxy_socket(
    socket_path: Path,
    proxy: subprocess.Popen[bytes],
    timeout: float = _GAMEMODE_PROXY_READY_TIMEOUT,
) -> bool:
    """Wait for the proxy socket to appear as a uid-owned socket."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proxy.poll() is not None:
            return False
        try:
            metadata = os.stat(socket_path, follow_symlinks=False)
        except OSError:
            pass
        else:
            if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.getuid():
                return True
        time.sleep(0.05)
    if proxy.poll() is not None:
        return False
    try:
        metadata = os.stat(socket_path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.getuid()


def _close_extra_fds(keep: set[int]) -> None:
    """Close inherited launcher descriptors; only the allowlist survives."""
    try:
        entries = os.listdir("/proc/self/fd")
    except OSError:
        return
    for entry in entries:
        try:
            descriptor = int(entry)
        except ValueError:
            continue
        if descriptor in keep:
            continue
        with suppress(OSError):
            os.close(descriptor)


def _supervisor_main(
    paths: AppPaths,
    identifier: str,
    name: str,
    root: Path,
    command: list[str],
    pass_fds: tuple[int, ...],
    parent_descriptor: int,
    session_descriptor: int,
    pipe_write: int,
    *,
    use_gamemode: bool = False,
    gamemode_proxy: Path | None = None,
    ci_mountpoint: Path | None = None,
) -> None:
    """Run detached; never returns, always terminates via os._exit."""
    # Detach standard streams so the supervisor never holds the terminal.
    try:
        null = os.open("/dev/null", os.O_RDWR | os.O_CLOEXEC)
    except OSError:
        null = -1
    if null >= 0:
        try:
            for target in (0, 1, 2):
                with suppress(OSError):
                    os.dup2(null, target)
        finally:
            if null > 2:
                os.close(null)
    for descriptor in (parent_descriptor, session_descriptor, pipe_write):
        with suppress(OSError):
            flags = fcntl.fcntl(descriptor, fcntl.F_GETFD)
            fcntl.fcntl(descriptor, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
    _close_extra_fds({0, 1, 2, pipe_write, parent_descriptor, session_descriptor, *pass_fds})
    lock_path = root / SESSION_LOCK_NAME
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        _pipe_error(pipe_write, f"cannot start game runtime: {exc}")
        os.close(pipe_write)
        os._exit(1)
    try:
        with suppress(OSError):
            os.fchmod(lock_fd, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            _pipe_error(pipe_write, f"cannot start game runtime: {exc}")
            os.close(pipe_write)
            os._exit(1)
        try:
            _write_status_atomic(root, _initial_status(os.getpid()))
        except LaunchError as exc:
            _pipe_error(pipe_write, str(exc))
            os.close(pipe_write)
            os._exit(1)
        # Close the post-exit race: a concurrent launcher may have created
        # its own session just now. Fail closed instead of multi-launching.
        for other in _list_session_names(paths, identifier):
            if other == name:
                continue
            if (
                is_session_running(paths, identifier, other)
                and poll_launch_status(paths, identifier, other) is None
            ):
                _pipe_error(pipe_write, str(_("game already running")))
                os.close(pipe_write)
                _remove_supervised(paths, root, parent_descriptor, session_descriptor)
                os._exit(1)
        if not command or command[0] != str(BWRAP):
            _pipe_error(pipe_write, "refusing to execute a runtime without Bubblewrap")
            os.close(pipe_write)
            _remove_supervised(paths, root, parent_descriptor, session_descriptor)
            os._exit(1)
        terminated = False
        child_pid: int | None = None
        proxy: subprocess.Popen[bytes] | None = None
        gamemode_registered = False

        def _handle_terminate(signum: int, frame: object) -> None:
            # Only flag and signal here; host Unregister happens once after
            # proc.wait(), before proxy termination, so the handler never
            # blocks on D-Bus and both SIGTERM/stop and natural exits share
            # the same balanced cleanup path below.
            nonlocal terminated
            terminated = True
            if proxy is not None and proxy.poll() is None:
                with suppress(OSError):
                    os.kill(proxy.pid, signal.SIGTERM)
            if child_pid is not None:
                with suppress(OSError):
                    os.kill(child_pid, signal.SIGTERM)

        with suppress(OSError):
            signal.signal(signal.SIGTERM, _handle_terminate)
        if use_gamemode:
            try:
                address = _gamemode.resolve_session_bus_address(os.environ)
                _gamemode.require_gamemode()
                _gamemode.require_bus_client()
                expected_proxy = root / _gamemode.GAMEMODE_PROXY_SOCKET_NAME
                proxy_path = expected_proxy if gamemode_proxy is None else Path(gamemode_proxy)
                if proxy_path != expected_proxy:
                    raise LaunchError("unsafe GameMode proxy path")
            except LaunchError as exc:
                _pipe_error(pipe_write, str(exc))
                os.close(pipe_write)
                _remove_supervised(paths, root, parent_descriptor, session_descriptor)
                os._exit(1)
            with suppress(OSError):
                os.unlink(proxy_path)
            try:
                proxy = subprocess.Popen(
                    _gamemode.proxy_argv(address, proxy_path),
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                _pipe_error(
                    pipe_write,
                    str(_("cannot start game runtime: {error}").format(error=exc)),
                )
                os.close(pipe_write)
                _remove_supervised(paths, root, parent_descriptor, session_descriptor)
                os._exit(1)
            assert proxy is not None
            if not _wait_for_proxy_socket(proxy_path, proxy):
                _pipe_error(pipe_write, "cannot start GameMode proxy")
                _terminate_proxy(proxy)
                os.close(pipe_write)
                _remove_supervised(paths, root, parent_descriptor, session_descriptor)
                os._exit(1)
        try:
            proc = subprocess.Popen(
                command,
                close_fds=True,
                pass_fds=pass_fds,
                env=runtime_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            _pipe_error(
                pipe_write,
                str(_("cannot start game runtime: {error}").format(error=exc)),
            )
            _terminate_proxy(proxy)
            os.close(pipe_write)
            _remove_supervised(paths, root, parent_descriptor, session_descriptor)
            os._exit(1)
        child_pid = proc.pid
        for descriptor in pass_fds:
            with suppress(OSError):
                os.close(descriptor)
        try:
            running = _initial_status(os.getpid())
            running["child_pid"] = child_pid
            _write_status_atomic(root, running)
        except LaunchError as exc:
            _pipe_error(pipe_write, str(exc))
            with suppress(OSError):
                proc.terminate()
            with suppress(OSError):
                proc.wait(timeout=5)
            _terminate_proxy(proxy)
            os.close(pipe_write)
            _remove_supervised(paths, root, parent_descriptor, session_descriptor)
            os._exit(1)
        # Host-side GameMode registration (additive, fail-closed). bwrap
        # --unshare-pid hides the in-sandbox getpid() from the host daemon,
        # so the in-sandbox gamemoderun prefix alone can never resolve to a
        # live host /proc/<pid>. Register the host bwrap PID here, where the
        # supervisor is the bwrap parent. Fail closed on any registration
        # error instead of launching silently unboosted; the in-sandbox
        # prefix plus the filtered proxy stay as harmless fallback.
        if use_gamemode:
            try:
                _gamemode.register_host_game(child_pid)
                gamemode_registered = True
            except (LaunchError, OSError, ValueError) as exc:
                _pipe_error(pipe_write, str(exc))
                with suppress(OSError):
                    proc.terminate()
                with suppress(OSError):
                    proc.wait(timeout=5)
                _terminate_proxy(proxy)
                os.close(pipe_write)
                _remove_supervised(paths, root, parent_descriptor, session_descriptor)
                os._exit(1)
        try:
            os.write(pipe_write, f"READY {identifier} {name}\n".encode("ascii"))
        except OSError:
            if gamemode_registered:
                with suppress(Exception):
                    _gamemode.unregister_host_game(child_pid)
            with suppress(OSError):
                proc.terminate()
            with suppress(OSError):
                proc.wait(timeout=5)
            _terminate_proxy(proxy)
            os.close(pipe_write)
            _remove_supervised(paths, root, parent_descriptor, session_descriptor)
            os._exit(1)
        with suppress(OSError):
            os.close(pipe_write)
        if terminated and proc.poll() is None:
            with suppress(OSError):
                proc.terminate()
        try:
            exit_code = proc.wait()
        except OSError:
            exit_code = 1
        if gamemode_registered:
            # Best-effort Unregister covers natural exit and SIGTERM/stop
            # paths alike; it runs before proxy termination, independently of
            # it, and never affects the exit code. Success stays silent
            # (hook firing is the oracle).
            with suppress(Exception):
                _gamemode.unregister_host_game(child_pid)
        _terminate_proxy(proxy)
        _teardown_ci_mount(ci_mountpoint)
        finished: dict[str, object] = {
            "pid": os.getpid(),
            "child_pid": child_pid,
            "state": _STATE_EXITED,
            "exit_code": int(exit_code),
            "started_at": running.get("started_at", time.time()),
            "ended_at": time.time(),
        }
        with suppress(LaunchError, OSError):
            _write_status_atomic(root, finished)
        # Grace keeps the final status readable for foreground pollers that
        # started just after a fast exit, while the held flock still proves
        # supervisor lifetime. The single-instance guard treats this exited
        # grace as not-live, so relaunches stay prompt.
        deadline = time.time() + STATUS_GRACE_SECONDS
        while time.time() < deadline:
            if terminated:
                break
            time.sleep(0.1)
        _remove_supervised(paths, root, parent_descriptor, session_descriptor)
        os._exit(0)
    finally:
        with suppress(OSError):
            os.close(lock_fd)


def _remove_supervised(
    paths: AppPaths, root: Path, parent_descriptor: int, session_descriptor: int
) -> None:
    from box.launch.cleanup import remove_session

    with suppress(LaunchError, OSError):
        remove_session(
            paths,
            root,
            parent_descriptor=parent_descriptor,
            session_descriptor=session_descriptor,
        )
    with suppress(OSError):
        os.close(session_descriptor)
    with suppress(OSError):
        os.close(parent_descriptor)


def spawn_detached(
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
    """Double-fork a supervisor that parents the exact Bubblewrap command.

    Validates like ``run_process`` (Bubblewrap guard, fixed environment, no
    shell, only the ``pass_fds`` allowlist crosses), then returns once the
    supervisor confirms startup. The first child exits so the launcher can
    reap it; the orphaned grandchild (reparented to init) holds the session
    flock for its lifetime and owns cleanup. The optional ``ci_mountpoint``
    hands a case-insensitive mount path to the supervisor for post-exit
    teardown via ``force_unmount``; only the path crosses, never a descriptor.
    """
    if not command or command[0] != str(BWRAP):
        raise LaunchError("refusing to execute a runtime without Bubblewrap")
    root = session_root(paths, identifier, name)
    check_no_live_session(paths, identifier)
    try:
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    except OSError as exc:
        raise LaunchError(_("cannot start game runtime: {error}").format(error=exc)) from exc
    try:
        first = os.fork()
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise LaunchError(_("cannot start game runtime: {error}").format(error=exc)) from exc
    if first != 0:
        os.close(write_fd)
        try:
            while True:
                try:
                    os.waitpid(first, 0)
                    break
                except InterruptedError:
                    continue
                except OSError:
                    break
            response = _read_ready(read_fd)
        finally:
            os.close(read_fd)
        if response.startswith("READY"):
            return LaunchedSession(identifier=identifier, name=name, root=root)
        message = response.removeprefix("ERROR ").strip()
        raise LaunchError(message or _("cannot start game runtime"))
    # First child: new session, then fork the orphaned supervisor.
    try:
        os.close(read_fd)
        try:
            os.setsid()
        except OSError as exc:
            _pipe_error(write_fd, f"cannot start game runtime: {exc}")
            os.close(write_fd)
            os._exit(1)
        try:
            second = os.fork()
        except OSError as exc:
            _pipe_error(write_fd, f"cannot start game runtime: {exc}")
            os.close(write_fd)
            os._exit(1)
        if second != 0:
            os._exit(0)
        _supervisor_main(
            paths,
            identifier,
            name,
            root,
            list(command),
            tuple(pass_fds),
            parent_descriptor,
            session_descriptor,
            write_fd,
            use_gamemode=use_gamemode,
            gamemode_proxy=gamemode_proxy,
            ci_mountpoint=ci_mountpoint,
        )
        os._exit(0)
    finally:
        # Only reached on unexpected local failure before _supervisor_main.
        with suppress(BaseException):
            os._exit(1)


def _read_ready(read_fd: int) -> str:
    deadline = time.time() + _SUPERVISOR_READY_TIMEOUT
    chunks = bytearray()
    while time.time() < deadline:
        remaining = max(0.1, deadline - time.time())
        ready, _, _ = select.select([read_fd], [], [], min(remaining, 1.0))
        if not ready:
            continue
        try:
            data = os.read(read_fd, 4096)
        except InterruptedError:
            continue
        except OSError:
            break
        if not data:
            break
        chunks += data
        if b"\n" in chunks or len(chunks) >= 4096:
            break
    try:
        return bytes(chunks).decode("utf-8", "replace").strip()
    except ValueError:
        return ""
