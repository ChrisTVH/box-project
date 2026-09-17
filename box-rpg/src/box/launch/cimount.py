"""Read-only case-insensitive FUSE passthrough over the NW.js game root.

This module is the core of the ``--ci-mount`` feature: a read-only FUSE
mount that exposes the game root verbatim except that a failed exact
lookup falls back to :func:`resolve_insensitive_name`, so games shipped
with the wrong filename case keep working under a case-sensitive
filesystem.

Ownership and handover contract (critical):

* The launcher mounts *before* Bubblewrap starts and immediately hands
  the loop to a double-forked daemon child, so the FUSE loop outlives
  launcher exit.  The daemon inherits its own copies of the backing
  game-root descriptor and the ``/dev/fuse`` descriptor across the fork.
* The launcher side owns a :class:`CiMountSession` handle.  After
  :meth:`CiMountSession.disown` the launcher must NEVER call
  ``fuse_session_unmount``/``fuse_session_destroy`` on its copy; it only
  releases its own references (``disown``/``close`` never unmount).
  Exiting the launcher (including through the context manager) therefore
  never tears the mount down.
* Teardown belongs exclusively to the supervisor path: it triggers the
  end of the loop with :func:`force_unmount` (``fusermount3 -u`` first,
  ``umount2`` fallback), which makes the daemon's
  ``fuse_session_loop`` return; the daemon then runs its own copy's
  :meth:`CiMountSession.unmount` (real ``fuse_session_unmount`` plus
  ``fuse_session_destroy``) and exits on its own.  A calling
  ``disown`` handle can never ``unmount`` again (fail closed).

Descriptor-inheritance constraint:

* The FUSE descriptor (``fuse_session_fd``) and the backing game-root
  descriptor must be propagated to whichever process owns the loop
  through *explicit* parameters that survive
  :func:`box.launch.supervisor._close_extra_fds` (see
  :attr:`CiMountSession.inherit_fds`).  They must never be smuggled
  through the Bubblewrap ``pass_fds`` allowlist, which would leak host
  descriptors into the sandboxed game.  Wiring them into
  ``spawn_detached``/``_supervisor_main`` is future work; this module
  only exposes the descriptors and documents the rule.

Locking discipline:

* :func:`ensure_profile_ci_mount_path` and :func:`drop_stale_ci_mount`
  operate on launcher-owned storage below ``profiles/<identifier>/``.
  Callers must hold ``cache_lock(profile_fd, CI_MOUNT_DIRNAME)`` the way
  ``evb.py`` holds the per-profile lock around publication; this module
  invents no new lock primitive.

Read-only nature: every low-level operation is a 1:1 descriptor-relative
passthrough (``openat``/``fstatat``/``pread`` against the already-open
game-root descriptor duplicated into the session, never by absolute
path) except ``lookup``, which resolves through
:func:`resolve_insensitive_name`.  Write opens are refused with
``EROFS``, symlinks are never resolved, and the mount itself carries
``ro`` plus ``default_permissions``.

Descriptor economy: directory inodes (plus the game-root descriptor
itself) retain one open descriptor each; file inodes retain none
(``fd=-1`` with the resolved basename kept for per-request
resolution).  File metadata and I/O resolve descriptor-relatively off
the parent directory descriptor on every request (``fstatat`` for
``getattr``/``open``, a transient ``openat`` plus ``pread`` for
``read``), so a full-tree walk holds only directory descriptors and
cannot exhaust ``RLIMIT_NOFILE`` through file inodes (only live
directory descriptors accumulate, dropped with their inode).  A rename between ``lookup`` and a
later request fails closed exactly like a raw filesystem read.  The
daemon additionally raises its soft ``RLIMIT_NOFILE`` toward 65536 at
startup (best effort, never fatal to the mount).

Readdir consistency: a directory listing is built once per inode on the
first ``readdir`` (a single ``scandir`` pass sorted by name, with one
``stat`` snapshot per entry) and memoized by synthetic inode, shared by
concurrent opens of the same inode. Every ``readdir`` revalidates the
memoized listing against the directory mtime with a single ``fstat``: a
changed mtime rebuilds the listing in place, so listings share the same
name-freshness as ``lookup``. The snapshot is additionally dropped by the
last ``release``/``releasedir`` of any open session or when ``forget``
removes the inode itself — never by a lone ``release`` while sibling
handles stay open, and never by a ``forget`` that leaves the inode alive.
A residual scan-then-use gap stays irreducible (the same as any raw
filesystem read): entries may change between the scan and a later lookup
or emission.

Limitations and assumptions:

* Only ``x86_64`` Linux is supported for real mounts: the ``struct
  stat`` layout handed to libfuse3 is defined for that ABI and mounting
  anywhere else fails closed with :class:`box.errors.LaunchError`.
* libfuse3 forks its ``fusermount3`` helper to establish the mount; that
  short-lived helper briefly inherits the launcher's open descriptors,
  which libfuse does not allow us to suppress.
* The daemon is reparented to init by design, so teardown is by unmount
  only, never by signal; a stale mount is recovered with
  :func:`drop_stale_ci_mount` under the per-profile lock.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import resource
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from ctypes.util import find_library
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from box.errors import ConfigurationError, LaunchError
from box.paths import AppPaths
from box.utils.i18n import _

__all__ = [
    "CI_MOUNT_DIRNAME",
    "PROC_MOUNTS",
    "CiMountSession",
    "clear_resolver_cache",
    "drop_stale_ci_mount",
    "ensure_profile_ci_mount_path",
    "force_unmount",
    "is_available",
    "is_mountpoint_active",
    "mount_ci_mount",
    "require_libfuse3",
    "resolve_insensitive_name",
]

#: Directory name of the case-insensitive mount below one game profile.
CI_MOUNT_DIRNAME = "ci-mount"

#: Mount table consulted by :func:`is_mountpoint_active`.
PROC_MOUNTS = Path("/proc/mounts")

_FUSE_DEVICE_NODE = Path("/dev/fuse")
_FUSERMOUNT3_CANDIDATES = (Path("/usr/bin/fusermount3"), Path("/bin/fusermount3"))
_FUSERMOUNT_TIMEOUT = 10.0
_READY_TIMEOUT = 10.0
_READY_POLL_INTERVAL = 0.05
_UMOUNT_NOFOLLOW = 8
_FUSE_ROOT_ID = 1
# fuse_file_info layout is intentionally opaque: replies carry a zeroed
# oversized buffer (libfuse only ever reads its own smaller struct out of
# it) and the passthrough keeps no per-handle state, so only the stable
# leading fields (flags at offset 0) are ever inspected, never written.
_FILE_INFO_REPLY_SIZE = 128
_O_TRUNC = 0o1000

#: Env var naming a file for readdir/lookup decision logging (live diagnosis
#: only).  Unset (or empty) means no logging at all; see :func:`_debug_log`.
_DEBUG_LOG_ENV = "BOX_CIMOUNT_DEBUG_LOG"

_OCTAL_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


# ---------------------------------------------------------------------------
# Case-insensitive resolver (pure, mount-free).
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CachedListing:
    """Last readdir listing of one queried directory."""

    mtime_ns: int
    folded: dict[str, str]


_resolver_lock = threading.Lock()
_resolver_cache: dict[tuple[int, int], _CachedListing] = {}


def clear_resolver_cache() -> None:
    """Drop every cached directory listing (test hook and memory hygiene)."""
    with _resolver_lock:
        _resolver_cache.clear()


def _is_single_component(name: str) -> bool:
    """Reject empty names, dot names, and anything containing a separator."""
    return bool(name) and name not in (".", "..") and "/" not in name


def _scan_directory(dir_fd: int) -> dict[str, str]:
    """Read one directory once, mapping case-folded names to actual names.

    The first name wins in readdir order.  Symlinks and special files are
    skipped so the resolver can never hand out a symlink-escape hole; the
    FUSE layer additionally opens everything with ``O_NOFOLLOW``.
    """
    folded: dict[str, str] = {}
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if not entry.is_file(follow_symlinks=False) and not entry.is_dir(
                    follow_symlinks=False
                ):
                    continue
            except OSError:
                continue
            key = entry.name.casefold()
            if key not in folded:
                folded[key] = entry.name
    return folded


def resolve_insensitive_name(dir_fd: int, name: str) -> str | None:
    """Resolve ``name`` inside the pinned ``dir_fd``, case-insensitively.

    The exact name is tried first with a single ``stat`` (zero overhead
    for correctly-cased games); only on miss is the directory read once
    and matched with ``casefold``.  Everything stays relative to
    ``dir_fd`` and symlinks are never followed or returned, so a lookup
    cannot escape the pinned directory.  At most the last readdir
    listing per actually-queried directory is cached, keyed by
    ``(st_dev, st_ino)`` of the directory descriptor and invalidated by
    directory mtime; there is no whole-tree cache.  Any miss, invalid
    name, or filesystem error returns ``None`` (the FUSE layer reports
    ``ENOENT``), never raises.
    """
    if not _is_single_component(name):
        return None
    try:
        exact = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except NotADirectoryError:
        return None
    except OSError:
        return None
    else:
        if stat.S_ISLNK(exact.st_mode):
            return None
        return name
    try:
        directory = os.fstat(dir_fd)
    except OSError:
        return None
    key = (directory.st_dev, directory.st_ino)
    with _resolver_lock:
        cached = _resolver_cache.get(key)
        if cached is not None and cached.mtime_ns == directory.st_mtime_ns:
            return cached.folded.get(name.casefold())
    try:
        folded = _scan_directory(dir_fd)
    except OSError:
        return None
    try:
        refreshed = os.fstat(dir_fd)
    except OSError:
        return None
    with _resolver_lock:
        _resolver_cache[(refreshed.st_dev, refreshed.st_ino)] = _CachedListing(
            refreshed.st_mtime_ns, folded
        )
    return folded.get(name.casefold())


# ---------------------------------------------------------------------------
# libfuse3 availability (fail closed, never degrade silently).
# ---------------------------------------------------------------------------


def _find_libfuse3() -> str | None:
    """Return the libfuse3 path or ``None`` when it cannot be located."""
    try:
        return find_library("fuse3")
    except Exception:
        return None


def is_available() -> bool:
    """Report whether the libfuse3 backend required for ``--ci-mount`` exists."""
    return _find_libfuse3() is not None


def require_libfuse3() -> str:
    """Return the libfuse3 path or fail closed when it is unavailable."""
    path = _find_libfuse3()
    if path is None:
        raise LaunchError(
            _(
                "case-insensitive mount requires libfuse3; "
                "install libfuse3 and retry without --ci-mount"
            )
        )
    return path


# ---------------------------------------------------------------------------
# Mountpoint inspection and teardown.
# ---------------------------------------------------------------------------


def _decode_mount_field(field: str) -> str:
    """Decode the octal escapes (``\\040``) used in ``/proc/mounts`` fields."""
    return _OCTAL_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), field)


def _proc_mountpoints() -> set[str]:
    """Return the mountpoints listed in the mount table, if readable."""
    try:
        text = PROC_MOUNTS.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    points: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            points.add(_decode_mount_field(parts[1]))
    return points


def is_mountpoint_active(path: Path) -> bool:
    """Report whether ``path`` currently holds a mounted filesystem.

    The mount table is consulted first (both the literal and the fully
    resolved spellings of ``path``); when it is unreadable, a ``statvfs``
    style device comparison between ``path`` and its parent is used as a
    fallback.  Missing or unreadable paths report ``False``.
    """
    absolute = os.path.abspath(path)
    candidates = {absolute}
    with suppress(OSError):
        candidates.add(str(Path(absolute).resolve(strict=False)))
    if candidates & _proc_mountpoints():
        return True
    try:
        here = os.stat(absolute)
        parent = os.stat(os.path.dirname(absolute) or os.path.sep)
    except OSError:
        return False
    return here.st_dev != parent.st_dev


def _fusermount3_path() -> Path | None:
    """Return a fixed-path ``fusermount3`` helper or ``None`` when absent.

    Only absolute candidate paths are considered (no ``PATH`` lookup), so
    a hostile ``PATH`` cannot redirect the unmount helper.
    """
    for candidate in _FUSERMOUNT3_CANDIDATES:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def _run_fusermount3(executable: Path, target: str) -> bool:
    """Run ``fusermount3 -u`` without a shell; ``True`` only on success."""
    try:
        completed = subprocess.run(
            [str(executable), "-u", target],
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_FUSERMOUNT_TIMEOUT,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return completed.returncode == 0


def _libc_umount2(target: str) -> None:
    """Detach ``target`` with the ``umount2`` syscall; fail closed."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError as exc:
        raise LaunchError(
            _("cannot unmount the case-insensitive mount at {path}: {error}").format(
                path=target, error=exc
            )
        ) from exc
    libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.umount2.restype = ctypes.c_int
    if libc.umount2(target.encode("utf-8", "surrogateescape"), _UMOUNT_NOFOLLOW) != 0:
        code = ctypes.get_errno()
        raise LaunchError(
            _("cannot unmount the case-insensitive mount at {path}: {error}").format(
                path=target, error=os.strerror(code) if code else code
            )
        )


def force_unmount(path: Path) -> None:
    """Detach a stale case-insensitive mount; fail closed on any failure.

    ``fusermount3 -u`` runs first and the ``umount2`` syscall is the
    fallback.  Failures raise :class:`box.errors.LaunchError` naming the
    mountpoint and the underlying error instead of degrading silently.
    """
    target = str(path)
    executable = _fusermount3_path()
    if executable is not None and _run_fusermount3(executable, target):
        return
    _libc_umount2(target)


# ---------------------------------------------------------------------------
# Launcher-owned mountpoint paths.
# ---------------------------------------------------------------------------


def ensure_profile_ci_mount_path(paths: AppPaths, identifier: str) -> Path:
    """Resolve and create ``profiles/<identifier>/ci-mount/`` for one game.

    The directory lives in launcher-owned cache storage; nothing is ever
    written to the user game folder.  The result is validated through
    :meth:`AppPaths.ensure_managed_profile_ci_mount_path`.  Callers must
    hold ``cache_lock(profile_fd, CI_MOUNT_DIRNAME)`` the way ``evb.py``
    holds the per-profile lock around publication; this function invents
    no new lock primitive.
    """
    if (
        not identifier
        or identifier in {".", ".."}
        or Path(identifier).name != identifier
        or identifier != identifier.lower()
    ):
        raise ConfigurationError(
            _("invalid game profile identifier: {identifier}").format(identifier=identifier)
        )
    profile_fd = paths.open_or_create_private_cache_directory("profiles", identifier)
    try:
        with suppress(FileExistsError):
            os.mkdir(CI_MOUNT_DIRNAME, 0o700, dir_fd=profile_fd)
    finally:
        os.close(profile_fd)
    return paths.ensure_managed_profile_ci_mount_path(
        paths.profiles_root / identifier / CI_MOUNT_DIRNAME
    )


def drop_stale_ci_mount(mountpoint: Path) -> None:
    """Clear a leftover ci-mount tree while holding the per-profile lock.

    An active stale mount is detached first (fail closed: a
    :class:`box.errors.LaunchError` propagates and nothing is deleted).
    Afterwards the underlying launcher-owned directory is emptied and
    recreated with private permissions; only plain launcher-owned
    content is ever removed, never game files or saves.
    """
    if is_mountpoint_active(mountpoint):
        force_unmount(mountpoint)
    try:
        metadata = os.lstat(mountpoint)
    except FileNotFoundError:
        mountpoint.mkdir(parents=True, mode=0o700)
        return
    except OSError as exc:
        raise LaunchError(
            _("cannot clear the stale case-insensitive mount at {path}: {error}").format(
                path=mountpoint, error=exc
            )
        ) from exc
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        with suppress(OSError):
            shutil.rmtree(mountpoint)
        with suppress(FileExistsError):
            mountpoint.mkdir(parents=True, mode=0o700)
    else:
        with suppress(OSError):
            os.unlink(mountpoint)
        with suppress(FileExistsError):
            mountpoint.mkdir(parents=True, mode=0o700)
    try:
        final = os.lstat(mountpoint)
    except OSError as exc:
        raise LaunchError(
            _("cannot clear the stale case-insensitive mount at {path}: {error}").format(
                path=mountpoint, error=exc
            )
        ) from exc
    if not stat.S_ISDIR(final.st_mode) or stat.S_ISLNK(final.st_mode):
        raise LaunchError(
            _("cannot clear the stale case-insensitive mount at {path}").format(path=mountpoint)
        )
    try:
        with os.scandir(mountpoint) as entries:
            residual = any(True for _ in entries)
    except OSError as exc:
        raise LaunchError(
            _("cannot clear the stale case-insensitive mount at {path}: {error}").format(
                path=mountpoint, error=exc
            )
        ) from exc
    if residual:
        raise LaunchError(
            _("cannot clear the stale case-insensitive mount at {path}").format(path=mountpoint)
        )


# ---------------------------------------------------------------------------
# libfuse3 low-level ctypes bindings (minimal read-only passthrough).
# ---------------------------------------------------------------------------


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_int64), ("tv_nsec", ctypes.c_int64)]


class _Stat(ctypes.Structure):
    """``struct stat`` for the x86_64 Linux ABI (see module limitations)."""

    _fields_ = [
        ("st_dev", ctypes.c_uint64),
        ("st_ino", ctypes.c_uint64),
        ("st_nlink", ctypes.c_uint64),
        ("st_mode", ctypes.c_uint32),
        ("st_uid", ctypes.c_uint32),
        ("st_gid", ctypes.c_uint32),
        ("__pad0", ctypes.c_int32),
        ("st_rdev", ctypes.c_uint64),
        ("st_size", ctypes.c_int64),
        ("st_blksize", ctypes.c_int64),
        ("st_blocks", ctypes.c_int64),
        ("st_atim", _Timespec),
        ("st_mtim", _Timespec),
        ("st_ctim", _Timespec),
        ("__reserved", ctypes.c_int64 * 3),
    ]


class _FuseEntryParam(ctypes.Structure):
    _fields_ = [
        ("ino", ctypes.c_uint64),
        ("generation", ctypes.c_uint64),
        ("attr", _Stat),
        ("attr_timeout", ctypes.c_double),
        ("entry_timeout", ctypes.c_double),
    ]


class _FuseArgs(ctypes.Structure):
    _fields_ = [
        ("argc", ctypes.c_int),
        ("argv", ctypes.POINTER(ctypes.c_char_p)),
        ("allocated", ctypes.c_int),
    ]


# Order matches ``struct fuse_lowlevel_ops`` in ``fuse_lowlevel.h``; new
# handlers are always appended upstream, so the stable prefix through
# ``lseek`` keeps working against newer libfuse3 releases (the trailing
# ``tmpfile``/``statx``/``syncfs`` handlers simply stay unimplemented).
_OP_NAMES = (
    "init",
    "destroy",
    "lookup",
    "forget",
    "getattr",
    "setattr",
    "readlink",
    "mknod",
    "mkdir",
    "unlink",
    "rmdir",
    "symlink",
    "rename",
    "link",
    "open",
    "read",
    "write",
    "flush",
    "release",
    "fsync",
    "opendir",
    "readdir",
    "releasedir",
    "fsyncdir",
    "statfs",
    "setxattr",
    "getxattr",
    "listxattr",
    "removexattr",
    "access",
    "create",
    "getlk",
    "setlk",
    "bmap",
    "ioctl",
    "poll",
    "write_buf",
    "retrieve_reply",
    "forget_multi",
    "flock",
    "fallocate",
    "readdirplus",
    "copy_file_range",
    "lseek",
)


class _FuseOps(ctypes.Structure):
    _fields_ = [(name, ctypes.c_void_p) for name in _OP_NAMES]


_LookupProto = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_char_p)
_ForgetProto = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64)
_GetattrProto = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p)
_OpenProto = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p)
_ReadProto = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_int64, ctypes.c_void_p
)
_ReleaseProto = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p)
_ReaddirProto = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_int64, ctypes.c_void_p
)


def _fill_stat(target: _Stat, source: os.stat_result) -> None:
    """Copy an ``os.stat`` result into the C ``struct stat`` for libfuse3."""
    target.st_dev = source.st_dev
    target.st_ino = source.st_ino
    target.st_nlink = source.st_nlink
    target.st_mode = source.st_mode
    target.st_uid = source.st_uid
    target.st_gid = source.st_gid
    target.st_rdev = source.st_rdev
    target.st_size = source.st_size
    target.st_blksize = source.st_blksize
    target.st_blocks = source.st_blocks
    target.st_atim.tv_sec = source.st_atime_ns // 1_000_000_000
    target.st_atim.tv_nsec = source.st_atime_ns % 1_000_000_000
    target.st_mtim.tv_sec = source.st_mtime_ns // 1_000_000_000
    target.st_mtim.tv_nsec = source.st_mtime_ns % 1_000_000_000
    target.st_ctim.tv_sec = source.st_ctime_ns // 1_000_000_000
    target.st_ctim.tv_nsec = source.st_ctime_ns % 1_000_000_000


@dataclass(slots=True)
class _InodeRecord:
    """One live FUSE inode: its parent, lookup refs, and how to reach it.

    Directory inodes (and root) retain an open descriptor in ``fd``;
    file inodes retain none (``fd == -1``) and are instead resolved per
    request off the parent directory descriptor through ``name``, the
    resolved basename within the parent (``None`` only for root).
    """

    fd: int
    parent: int
    nlookup: int
    opens: int = 0
    name: str | None = None


@dataclass(slots=True)
class _ReaddirListing:
    """One memoized readdir snapshot plus the directory mtime it was taken at."""

    mtime_ns: int
    entries: list[tuple[str, os.stat_result]]


class _FuseState:
    """Inode table backing the loop; owned by the daemon child after fork."""

    def __init__(self, backing_fd: int) -> None:
        self.backing_fd = backing_fd
        self.inodes: dict[int, _InodeRecord] = {
            _FUSE_ROOT_ID: _InodeRecord(backing_fd, _FUSE_ROOT_ID, 1)
        }
        self.keys: dict[tuple[int, int], int] = {}
        self.next_ino = _FUSE_ROOT_ID + 1
        # Listings live here keyed by synthetic ino (rather than on
        # _InodeRecord) so file inodes pay no storage cost and the cache
        # lifetime stays explicit via release/forget drops.  Each entry
        # carries the directory mtime its snapshot was taken at, so every
        # readdir revalidates with a single fstat (same name-freshness as
        # the lookup resolver cache).
        self.listings: dict[int, _ReaddirListing] = {}
        # Lazily opened debug-log descriptor for BOX_CIMOUNT_DEBUG_LOG
        # (see _debug_log); -1 while logging stays disabled or unopened.
        self.debug_fd: int = -1


def _debug_log(state: _FuseState, message: str) -> None:
    """Append one metadata-only line to the debug log; never raises.

    A complete no-op unless the ``BOX_CIMOUNT_DEBUG_LOG`` env var names a
    file.  The file is opened lazily exactly once per daemon
    (``O_WRONLY|O_CREAT|O_APPEND``, ``0o600``) and the descriptor is kept
    on ``state``; every step is guarded so a logging failure can never
    propagate out of a FUSE callback.  Callers pass names, inodes,
    offsets, and mtimes only — never file contents.
    """
    try:
        path = os.environ.get(_DEBUG_LOG_ENV)
        if not path:
            return
        pending = state.debug_fd
        if pending < 0:
            try:
                pending = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            except OSError:
                return
            state.debug_fd = pending
        with suppress(OSError):
            os.write(pending, (message + "\n").encode("utf-8", "backslashreplace"))
    except Exception:
        pass


def _close_state_debug_log(state: _FuseState) -> None:
    """Best-effort close of the debug-log descriptor; never raises."""
    try:
        pending = state.debug_fd
    except Exception:
        return
    if pending < 0:
        return
    with suppress(Exception):
        state.debug_fd = -1
    with suppress(OSError):
        os.close(pending)


def _close_live_debug_logs(live: object) -> None:
    """Close any debug-log descriptor pinned by a session handle; never raises."""
    try:
        items = cast("tuple[object, ...]", live) if isinstance(live, tuple) else (live,)
        for candidate in items:
            if isinstance(candidate, _FuseState):
                _close_state_debug_log(candidate)
    except Exception:
        pass


class _LibFuse:
    """Typed handles for the libfuse3 symbols used by the passthrough."""

    def __init__(self, library: ctypes.CDLL) -> None:
        self.raw = library
        library.fuse_reply_err.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.fuse_reply_err.restype = ctypes.c_int
        library.fuse_reply_none.argtypes = [ctypes.c_void_p]
        library.fuse_reply_none.restype = None
        library.fuse_reply_entry.argtypes = [ctypes.c_void_p, ctypes.POINTER(_FuseEntryParam)]
        library.fuse_reply_entry.restype = ctypes.c_int
        library.fuse_reply_attr.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_Stat),
            ctypes.c_double,
        ]
        library.fuse_reply_attr.restype = ctypes.c_int
        library.fuse_reply_open.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        library.fuse_reply_open.restype = ctypes.c_int
        library.fuse_reply_buf.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        library.fuse_reply_buf.restype = ctypes.c_int
        library.fuse_add_direntry.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.POINTER(_Stat),
            ctypes.c_int64,
        ]
        library.fuse_add_direntry.restype = ctypes.c_size_t
        library.fuse_session_new.argtypes = [
            ctypes.POINTER(_FuseArgs),
            ctypes.POINTER(_FuseOps),
            ctypes.c_size_t,
            ctypes.c_void_p,
        ]
        library.fuse_session_new.restype = ctypes.c_void_p
        library.fuse_session_mount.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.fuse_session_mount.restype = ctypes.c_int
        library.fuse_session_loop.argtypes = [ctypes.c_void_p]
        library.fuse_session_loop.restype = ctypes.c_int
        library.fuse_session_unmount.argtypes = [ctypes.c_void_p]
        library.fuse_session_unmount.restype = None
        library.fuse_session_destroy.argtypes = [ctypes.c_void_p]
        library.fuse_session_destroy.restype = None

    def session_fd(self, session: int) -> int:
        """Return the ``/dev/fuse`` descriptor owned by ``session`` (-1 if unknown)."""
        getter = getattr(self.raw, "fuse_session_fd", None)
        if getter is None:
            return -1
        getter.argtypes = [ctypes.c_void_p]
        getter.restype = ctypes.c_int
        try:
            return int(getter(session))
        except Exception:
            return -1


@dataclass(frozen=True, slots=True)
class _OpenChildResult:
    """Outcome of :func:`_open_child`: an open descriptor or a named failure.

    Success carries ``fd`` plus ``metadata`` with an empty ``step``;
    failure carries the pipeline ``step`` that rejected the entry
    (``open``, ``fstat``, ``symlink``, ``reopen``, or ``special``) and the
    ``errno_code`` when the step raised (``None`` for deliberate policy
    rejections).  Only step names plus ints travel — never file contents.
    """

    fd: int = -1
    metadata: os.stat_result | None = None
    step: str = ""
    errno_code: int | None = None


def _open_child(backing_parent_fd: int, name: str) -> _OpenChildResult:
    """Open one directory entry descriptor-relative; never follow symlinks."""
    try:
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=backing_parent_fd,
        )
    except OSError as exc:
        return _OpenChildResult(step="open", errno_code=exc.errno)
    try:
        metadata = os.fstat(child_fd)
    except OSError as exc:
        code = exc.errno
        with suppress(OSError):
            os.close(child_fd)
        return _OpenChildResult(step="fstat", errno_code=code)
    if stat.S_ISLNK(metadata.st_mode):
        with suppress(OSError):
            os.close(child_fd)
        return _OpenChildResult(step="symlink")
    if stat.S_ISDIR(metadata.st_mode):
        # Reopen directories without O_NONBLOCK so later scans behave like
        # the descriptor-pinned listings used across the launcher. The
        # reopened descriptor gets its own variable so a failed reopen can
        # never close the already-closed first descriptor a second time.
        with suppress(OSError):
            os.close(child_fd)
        try:
            reopened_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=backing_parent_fd,
            )
        except OSError as exc:
            return _OpenChildResult(step="reopen", errno_code=exc.errno)
        try:
            metadata = os.fstat(reopened_fd)
        except OSError as exc:
            code = exc.errno
            with suppress(OSError):
                os.close(reopened_fd)
            return _OpenChildResult(step="reopen", errno_code=code)
        return _OpenChildResult(fd=reopened_fd, metadata=metadata)
    elif not stat.S_ISREG(metadata.st_mode):
        with suppress(OSError):
            os.close(child_fd)
        return _OpenChildResult(step="special")
    return _OpenChildResult(fd=child_fd, metadata=metadata)


def _scan_readdir_listing(dir_fd: int) -> list[tuple[str, os.stat_result]]:
    """Read one directory once for ``readdir``, sorted by name for stable offsets.

    A single ``os.scandir`` pass classifies entries via ``d_type`` (no extra
    stat) with an ``os.stat`` fallback when the type is unknown or races;
    symlinks and non-regular/dir entries are skipped exactly like the
    uncached path, and per-entry failures skip that entry.  The returned
    ``stat`` snapshots are immutable, so later emission needs no re-stat.
    """
    children: list[tuple[str, os.stat_result]] = []
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            try:
                try:
                    if entry.is_symlink():
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_reg = entry.is_file(follow_symlinks=False) if not is_dir else False
                    if not is_dir and not is_reg:
                        continue
                except OSError:
                    try:
                        fallback = os.stat(entry.name, dir_fd=dir_fd, follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(fallback.st_mode):
                        continue
                    if not stat.S_ISREG(fallback.st_mode) and not stat.S_ISDIR(fallback.st_mode):
                        continue
                    children.append((entry.name, fallback))
                    continue
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                if not stat.S_ISREG(metadata.st_mode) and not stat.S_ISDIR(metadata.st_mode):
                    continue
                children.append((entry.name, metadata))
            except OSError:
                continue
    children.sort(key=lambda item: item[0])
    return children


class _CallbackSet:
    """Build the low-level handlers closing over one fuse state and library."""

    def __init__(self, library: _LibFuse, state: _FuseState) -> None:
        self._library = library
        self._state = state
        self._references: list[Any] = []
        self.ops = _FuseOps()
        self._install("lookup", _LookupProto(self._on_lookup))
        self._install("forget", _ForgetProto(self._on_forget))
        self._install("getattr", _GetattrProto(self._on_getattr))
        self._install("open", _OpenProto(self._on_open))
        self._install("read", _ReadProto(self._on_read))
        self._install("release", _ReleaseProto(self._on_release))
        self._install("opendir", _OpenProto(self._on_opendir))
        self._install("readdir", _ReaddirProto(self._on_readdir))
        self._install("releasedir", _ReleaseProto(self._on_release))

    def _install(self, name: str, callback: Any) -> None:
        """Pin one callback and publish its address in the ops struct."""
        self._references.append(callback)
        address = ctypes.cast(callback, ctypes.c_void_p).value
        setattr(self.ops, name, address)

    def _reply_entry(self, req: int, ino: int, metadata: os.stat_result) -> None:
        param = _FuseEntryParam()
        param.ino = ino
        param.generation = 0
        _fill_stat(param.attr, metadata)
        param.attr_timeout = 0.0
        param.entry_timeout = 0.0
        self._library.raw.fuse_reply_entry(req, ctypes.byref(param))

    def _register(
        self, child_fd: int, metadata: os.stat_result, parent: int, name: str | None
    ) -> int:
        """File one opened descriptor as a FUSE inode, reusing stable keys.

        Directory lookups hand over their open descriptor (retained on
        the record); file lookups hand over ``-1`` (the transient open
        used for the entry reply is already closed by the caller, and
        ``name`` carries the resolved basename for per-request parent
        relative resolution instead).
        """
        state = self._state
        key = (metadata.st_dev, metadata.st_ino)
        existing = state.keys.get(key)
        if existing is not None and existing in state.inodes:
            if child_fd >= 0:
                with suppress(OSError):
                    os.close(child_fd)
            state.inodes[existing].nlookup += 1
            return existing
        if state.next_ino >= (1 << 64) - 1:
            if child_fd >= 0:
                with suppress(OSError):
                    os.close(child_fd)
            raise OverflowError("inode space exhausted")
        ino = state.next_ino
        state.next_ino += 1
        state.inodes[ino] = _InodeRecord(child_fd, parent, 1, name=name)
        state.keys[key] = ino
        return ino

    def _on_lookup(self, req: int, parent: int, raw_name: bytes | None) -> None:
        reply_err = self._library.raw.fuse_reply_err
        try:
            record = self._state.inodes.get(parent)
            if record is None or raw_name is None:
                reply_err(req, errno.ENOENT)
                return
            if record.fd < 0:
                # A file can never be a legitimate lookup parent (only
                # directories retain descriptors; root always holds its
                # own), so refuse instead of issuing openat(-1).
                reply_err(req, errno.ENOTDIR)
                return
            try:
                name = raw_name.decode("utf-8", "surrogateescape")
            except Exception:
                reply_err(req, errno.ENOENT)
                return
            if name == ".":
                try:
                    metadata = os.fstat(record.fd)
                except OSError as exc:
                    reply_err(req, exc.errno or errno.EIO)
                    return
                record.nlookup += 1
                self._reply_entry(req, parent, metadata)
                return
            if name == "..":
                grandparent = self._state.inodes.get(record.parent)
                if grandparent is None:
                    reply_err(req, errno.ENOENT)
                    return
                try:
                    metadata = os.fstat(grandparent.fd)
                except OSError as exc:
                    reply_err(req, exc.errno or errno.EIO)
                    return
                grandparent.nlookup += 1
                self._reply_entry(req, record.parent, metadata)
                return
            resolved = resolve_insensitive_name(record.fd, name)
            _debug_log(
                self._state,
                f"lookup parent={parent} name={name!r} name_resolved={resolved!r}",
            )
            if resolved is None:
                reply_err(req, errno.ENOENT)
                return
            opened = _open_child(record.fd, resolved)
            if opened.metadata is None or opened.fd < 0:
                _debug_log(
                    self._state,
                    f"lookup parent={parent} name={name!r} "
                    f"open_failed step={opened.step} errno={opened.errno_code}",
                )
                reply_err(req, errno.ENOENT)
                return
            child_fd, metadata = opened.fd, opened.metadata
            if not stat.S_ISDIR(metadata.st_mode):
                # File records retain no descriptor: close the transient
                # open used for the entry reply and register with fd=-1.
                # Later getattr/open/read resolve off the parent
                # descriptor per request through the resolved name.
                with suppress(OSError):
                    os.close(child_fd)
                child_fd = -1
            try:
                ino = self._register(child_fd, metadata, parent, resolved)
            except OverflowError:
                _debug_log(
                    self._state,
                    f"lookup parent={parent} name={name!r} "
                    f"open_failed step=register errno={errno.ENOSPC}",
                )
                reply_err(req, errno.ENOSPC)
                return
            _debug_log(
                self._state,
                f"lookup parent={parent} name={name!r} opened ino={ino}",
            )
            self._reply_entry(req, ino, metadata)
        except Exception:
            with suppress(Exception):
                reply_err(req, errno.EIO)

    def _on_forget(self, req: int, ino: int, nlookup: int) -> None:
        try:
            if ino != _FUSE_ROOT_ID:
                record = self._state.inodes.get(ino)
                if record is not None:
                    record.nlookup = max(0, record.nlookup - nlookup)
                    if record.nlookup == 0:
                        self._state.inodes.pop(ino, None)
                        # The listing dies with the inode: its snapshots were
                        # taken through a descriptor that is closed below, and
                        # no live open session can reference this ino anymore.
                        # While the inode stays alive the listing entry is
                        # kept, so forget-while-open only preserves identity:
                        # an mtime change still revalidates content mid-open.
                        self._state.listings.pop(ino, None)
                        for key, candidate in list(self._state.keys.items()):
                            if candidate == ino:
                                self._state.keys.pop(key, None)
                                break
                        with suppress(OSError):
                            os.close(record.fd)
        finally:
            with suppress(Exception):
                self._library.raw.fuse_reply_none(req)

    def _stat_record(self, record: _InodeRecord) -> os.stat_result:
        """Stat one inode without following symlinks.

        Retained descriptors (directories, root) use ``fstat`` exactly
        as before; file records (``fd == -1``) use ``fstatat`` off the
        parent directory descriptor through the resolved basename.  A
        missing parent (or a missing name) fails closed with ``EIO``; a
        rename between ``lookup`` and this call surfaces the raw
        ``stat`` errno, like a raw filesystem.
        """
        if record.fd >= 0:
            return os.fstat(record.fd)
        parent = self._state.inodes.get(record.parent)
        if parent is None or parent.fd < 0 or record.name is None:
            raise OSError(errno.EIO, "unresolvable file parent")
        return os.stat(record.name, dir_fd=parent.fd, follow_symlinks=False)

    def _on_getattr(self, req: int, ino: int, _file_info: int) -> None:
        reply_err = self._library.raw.fuse_reply_err
        try:
            record = self._state.inodes.get(ino)
            if record is None:
                reply_err(req, errno.ENOENT)
                return
            try:
                metadata = self._stat_record(record)
            except OSError as exc:
                reply_err(req, exc.errno or errno.EIO)
                return
            cstat = _Stat()
            _fill_stat(cstat, metadata)
            self._library.raw.fuse_reply_attr(req, ctypes.byref(cstat), 0.0)
        except Exception:
            with suppress(Exception):
                reply_err(req, errno.EIO)

    def _on_open(self, req: int, ino: int, file_info: int) -> None:
        reply_err = self._library.raw.fuse_reply_err
        try:
            record = self._state.inodes.get(ino)
            if record is None:
                reply_err(req, errno.ENOENT)
                return
            flags = ctypes.c_int.from_address(file_info).value if file_info else 0
            if (flags & 0x3) != 0 or (flags & _O_TRUNC):
                reply_err(req, errno.EROFS)
                return
            try:
                metadata = self._stat_record(record)
            except OSError as exc:
                reply_err(req, exc.errno or errno.EIO)
                return
            if not stat.S_ISREG(metadata.st_mode):
                if stat.S_ISDIR(metadata.st_mode):
                    reply_err(req, errno.EISDIR)
                else:
                    reply_err(req, errno.EACCES)
                return
            # Stateless I/O: reads transiently open off the parent plus
            # pread, keeping no handle, so the reply carries no handle,
            # only default (zeroed) open flags.
            self._library.raw.fuse_reply_open(
                req, ctypes.create_string_buffer(_FILE_INFO_REPLY_SIZE)
            )
        except Exception:
            with suppress(Exception):
                reply_err(req, errno.EIO)

    def _on_read(self, req: int, ino: int, size: int, offset: int, _file_info: int) -> None:
        reply_err = self._library.raw.fuse_reply_err
        try:
            record = self._state.inodes.get(ino)
            if record is None:
                reply_err(req, errno.ENOENT)
                return
            if record.fd >= 0:
                try:
                    data = os.pread(record.fd, size, offset)
                except OSError as exc:
                    reply_err(req, exc.errno or errno.EIO)
                    return
            else:
                # File records hold no descriptor: transiently open off
                # the parent per request (O_NOFOLLOW, never following
                # symlinks), pread statelessly, and always close again.
                parent = self._state.inodes.get(record.parent)
                if parent is None or parent.fd < 0 or record.name is None:
                    reply_err(req, errno.EIO)
                    return
                try:
                    child_fd = os.open(
                        record.name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent.fd,
                    )
                except OSError as exc:
                    reply_err(req, exc.errno or errno.EIO)
                    return
                try:
                    data = os.pread(child_fd, size, offset)
                except OSError as exc:
                    reply_err(req, exc.errno or errno.EIO)
                    return
                finally:
                    with suppress(OSError):
                        os.close(child_fd)
            payload = ctypes.create_string_buffer(bytes(data), len(data) if data else 1)
            self._library.raw.fuse_reply_buf(req, payload, len(data))
        except Exception:
            with suppress(Exception):
                reply_err(req, errno.EIO)

    def _on_release(self, req: int, ino: int, _file_info: int) -> None:
        try:
            # Listings are shared per inode, not per handle: only the last
            # release of an open session drops the snapshot, so one handle's
            # release never exposes a rescan to a still-open sibling handle.
            # Release for an unknown inode (or a file, which never caches)
            # simply drops any leftover entry.
            record = self._state.inodes.get(ino)
            if record is not None and record.opens > 0:
                record.opens -= 1
            if record is None or record.opens == 0:
                self._state.listings.pop(ino, None)
        finally:
            with suppress(Exception):
                self._library.raw.fuse_reply_err(req, 0)

    def _on_opendir(self, req: int, ino: int, _file_info: int) -> None:
        reply_err = self._library.raw.fuse_reply_err
        try:
            record = self._state.inodes.get(ino)
            if record is None:
                reply_err(req, errno.ENOENT)
                return
            if record.fd < 0:
                # Only directories retain descriptors, so an
                # unretained inode is always a regular file: keep the
                # historical ENOTDIR answer without a descriptor.
                reply_err(req, errno.ENOTDIR)
                return
            try:
                is_dir = stat.S_ISDIR(os.fstat(record.fd).st_mode)
            except OSError as exc:
                reply_err(req, exc.errno or errno.EIO)
                return
            if not is_dir:
                reply_err(req, errno.ENOTDIR)
                return
            record.opens += 1
            self._library.raw.fuse_reply_open(
                req, ctypes.create_string_buffer(_FILE_INFO_REPLY_SIZE)
            )
        except Exception:
            with suppress(Exception):
                reply_err(req, errno.EIO)

    def _on_readdir(self, req: int, ino: int, size: int, offset: int, _file_info: int) -> None:
        reply_err = self._library.raw.fuse_reply_err
        try:
            record = self._state.inodes.get(ino)
            if record is None:
                reply_err(req, errno.ENOENT)
                return
            if record.fd < 0:
                # Only directories retain descriptors (see _on_opendir):
                # an unretained inode is a regular file, never listable.
                reply_err(req, errno.ENOTDIR)
                return
            try:
                observed_mtime = os.fstat(record.fd).st_mtime_ns
            except OSError as exc:
                _debug_log(
                    self._state,
                    f"readdir ino={ino} error=fstat errno={exc.errno or errno.EIO}",
                )
                reply_err(req, exc.errno or errno.EIO)
                return
            cached = self._state.listings.get(ino)
            if cached is None:
                try:
                    entries = _scan_readdir_listing(record.fd)
                except OSError as exc:
                    _debug_log(
                        self._state,
                        f"readdir ino={ino} error=scan errno={exc.errno or errno.EIO}",
                    )
                    reply_err(req, exc.errno or errno.EIO)
                    return
                cached = _ReaddirListing(observed_mtime, entries)
                self._state.listings[ino] = cached
                _debug_log(
                    self._state,
                    f"readdir ino={ino} miss mtime_ns={observed_mtime} entries={len(entries)}",
                )
            elif cached.mtime_ns != observed_mtime:
                previous_mtime = cached.mtime_ns
                try:
                    entries = _scan_readdir_listing(record.fd)
                except OSError as exc:
                    _debug_log(
                        self._state,
                        f"readdir ino={ino} error=rescan errno={exc.errno or errno.EIO}",
                    )
                    reply_err(req, exc.errno or errno.EIO)
                    return
                cached = _ReaddirListing(observed_mtime, entries)
                self._state.listings[ino] = cached
                _debug_log(
                    self._state,
                    f"readdir ino={ino} rescan observed_mtime_ns={observed_mtime} "
                    f"cached_mtime_ns={previous_mtime} entries={len(entries)}",
                )
            else:
                _debug_log(
                    self._state,
                    f"readdir ino={ino} hit mtime_ns={observed_mtime} "
                    f"entries={len(cached.entries)}",
                )
            entries = cached.entries
            capacity = size if size > 0 else 4096
            buffer = ctypes.create_string_buffer(capacity)
            used = 0
            index = max(offset, 0)
            total = len(entries) + 2
            while index < total:
                child_stat: os.stat_result | None
                if index == 0:
                    name = "."
                    child_stat = None
                    fixed = ino
                elif index == 1:
                    name = ".."
                    child_stat = None
                    fixed = record.parent
                else:
                    name, snapshot = entries[index - 2]
                    child_stat = snapshot
                    fixed = 0
                next_offset = index + 1
                cstat = _Stat()
                if child_stat is None:
                    cstat.st_ino = fixed
                    cstat.st_mode = stat.S_IFDIR | 0o555
                else:
                    _fill_stat(cstat, child_stat)
                needed = self._library.raw.fuse_add_direntry(
                    req,
                    ctypes.byref(buffer, used),
                    capacity - used,
                    name.encode("utf-8", "surrogateescape"),
                    ctypes.byref(cstat),
                    next_offset,
                )
                if used + needed > capacity:
                    break
                used += needed
                index += 1
            self._library.raw.fuse_reply_buf(req, buffer, used)
        except Exception:
            with suppress(Exception):
                reply_err(req, errno.EIO)


# ---------------------------------------------------------------------------
# Session ownership handle.
# ---------------------------------------------------------------------------


class CiMountSession:
    """Ownership handle for one case-insensitive mount.

    The launcher side calls :meth:`disown` (and may call :meth:`close`)
    before exiting; neither ever invokes the unmount/destroy callbacks,
    so launcher exit can never tear the handed-over mount down.  Only
    the supervisor teardown path (in practice the loop daemon after
    :func:`force_unmount` ends its loop) calls :meth:`unmount`, exactly
    once.  A disowned handle refuses ``unmount`` (fail closed).

    :attr:`inherit_fds` exposes the live descriptors (backing game-root
    descriptor plus the ``/dev/fuse`` descriptor, ``-1`` entries omitted)
    that any future supervisor-side owner must propagate through
    explicit ``spawn_detached``/``_supervisor_main`` parameters surviving
    ``_close_extra_fds`` — never through the Bubblewrap ``pass_fds``
    allowlist.  The handle is also a context manager whose exit only
    releases references (``close``), never unmounts.
    """

    def __init__(
        self,
        mountpoint: Path,
        *,
        backing_fd: int = -1,
        fuse_fd: int = -1,
        on_unmount: Callable[[], None] | None = None,
        on_destroy: Callable[[], None] | None = None,
        live: object = None,
    ) -> None:
        self._mountpoint = mountpoint
        self._backing_fd = backing_fd
        self._fuse_fd = fuse_fd
        self._on_unmount = on_unmount
        self._on_destroy = on_destroy
        self._disowned = False
        self._unmounted = False
        # Opaque lifetime pin for the ctypes objects behind a real mount
        # (kept alive with the launcher-side handle; the loop daemon owns
        # its own forked copies).  Internal; callers leave it unset.
        self._live: object = live

    @property
    def mountpoint(self) -> Path:
        """Return the mountpoint this handle refers to."""
        return self._mountpoint

    @property
    def backing_fd(self) -> int:
        """Return the session's duplicated game-root descriptor (``-1`` when released)."""
        return self._backing_fd

    @property
    def fuse_fd(self) -> int:
        """Return the ``/dev/fuse`` descriptor owned by the session (``-1`` when unknown)."""
        return self._fuse_fd

    @property
    def disowned(self) -> bool:
        """Report whether launcher ownership was handed over via :meth:`disown`."""
        return self._disowned

    @property
    def unmounted(self) -> bool:
        """Report whether supervisor-side teardown already ran on this handle."""
        return self._unmounted

    @property
    def inherit_fds(self) -> tuple[int, ...]:
        """Return the live descriptors a future owner must propagate explicitly."""
        return tuple(fd for fd in (self._backing_fd, self._fuse_fd) if fd >= 0)

    def disown(self) -> None:
        """Hand the running mount over: release our references, never unmount.

        Idempotent.  After this call the handle can no longer
        :meth:`unmount`; the mount stays up for the supervisor teardown
        path.  The unmount/destroy callbacks are never invoked here.
        """
        self._disowned = True
        self._live = None
        if self._backing_fd >= 0:
            with suppress(OSError):
                os.close(self._backing_fd)
            self._backing_fd = -1
        # The /dev/fuse descriptor is owned by the libfuse session (and by
        # the daemon's inherited copy); the launcher drops its number too so
        # inherit_fds never reports a descriptor it no longer owns.
        self._fuse_fd = -1

    def unmount(self) -> None:
        """Run supervisor-side teardown exactly once (fail closed when disowned).

        Invokes the unmount callback, then the destroy callback, then
        releases the backing descriptor.  A second call is a no-op.
        """
        if self._unmounted:
            return
        if self._disowned:
            raise LaunchError(
                _(
                    "case-insensitive mount ownership was handed over; only the supervisor path may unmount {path}"
                ).format(path=self._mountpoint)
            )
        _close_live_debug_logs(self._live)
        try:
            if self._on_unmount is not None:
                self._on_unmount()
        finally:
            try:
                if self._on_destroy is not None:
                    self._on_destroy()
            finally:
                self._unmounted = True
                self._live = None
                self._fuse_fd = -1
                if self._backing_fd >= 0:
                    with suppress(OSError):
                        os.close(self._backing_fd)
                    self._backing_fd = -1

    def close(self) -> None:
        """Release our descriptor references without unmounting (always safe)."""
        _close_live_debug_logs(self._live)
        self._live = None
        self._fuse_fd = -1
        if self._backing_fd >= 0:
            with suppress(OSError):
                os.close(self._backing_fd)
            self._backing_fd = -1

    def __enter__(self) -> CiMountSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        # Never unmount on scope exit: the launcher leaving a ``with``
        # block must not tear down a handed-over mount.
        self.close()


# ---------------------------------------------------------------------------
# Mount orchestration.
# ---------------------------------------------------------------------------


def _raise_daemon_nofile_limit() -> None:
    """Raise the soft descriptor ceiling toward 65536, never lower it.

    Raising soft up to the hard limit needs no privilege.  A ceiling that
    is already higher stays untouched: lowering it could starve an
    unrelated inherited table for no benefit.
    """
    limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = min(limits[1], 65536)
    if limits[0] < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, limits[1]))


def _retain_only(keep: set[int]) -> None:
    """Close every inherited descriptor except the explicit keep set."""
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


def _redirect_stdio_to_null() -> None:
    """Detach the daemon's standard streams so it never holds the terminal."""
    try:
        null = os.open("/dev/null", os.O_RDWR)
    except OSError:
        return
    try:
        for target in (0, 1, 2):
            with suppress(OSError):
                os.dup2(null, target)
    finally:
        if null > 2:
            with suppress(OSError):
                os.close(null)


def mount_ci_mount(
    game_root_fd: int, mountpoint: Path, *, ready_timeout: float = _READY_TIMEOUT
) -> CiMountSession:
    """Mount a read-only case-insensitive view of ``game_root_fd`` at ``mountpoint``.

    The FUSE loop runs in a double-forked daemon holding its own copies
    of the descriptors, so the mount outlives launcher exit; the
    returned handle belongs to the launcher side and must be
    :meth:`CiMountSession.disown`\\ ed before exit.  Fails closed with
    :class:`box.errors.LaunchError` (missing libfuse3 or ``/dev/fuse``,
    unsafe mountpoint, stale active mount, or a loop that never comes
    up within ``ready_timeout``) instead of degrading silently.  Callers
    compose :func:`drop_stale_ci_mount` before this call while holding
    ``cache_lock(profile_fd, CI_MOUNT_DIRNAME)``.
    """
    library_path = require_libfuse3()
    if not _FUSE_DEVICE_NODE.exists():
        raise LaunchError(_("case-insensitive mount requires /dev/fuse; retry without --ci-mount"))
    try:
        mount_metadata = os.lstat(mountpoint)
    except OSError as exc:
        raise LaunchError(
            _("case-insensitive mountpoint is missing or unsafe: {path}").format(path=mountpoint)
        ) from exc
    if not stat.S_ISDIR(mount_metadata.st_mode) or stat.S_ISLNK(mount_metadata.st_mode):
        raise LaunchError(
            _("case-insensitive mountpoint is not a directory: {path}").format(path=mountpoint)
        )
    if is_mountpoint_active(mountpoint):
        raise LaunchError(
            _("case-insensitive mount already active at {path}; stop it before retrying").format(
                path=mountpoint
            )
        )
    try:
        backing_fd = os.dup(game_root_fd)
    except OSError as exc:
        raise LaunchError(
            _("cannot pin the game root for the case-insensitive mount: {error}").format(error=exc)
        ) from exc
    try:
        try:
            backing_metadata = os.fstat(backing_fd)
        except OSError as exc:
            raise LaunchError(
                _("cannot pin the game root for the case-insensitive mount: {error}").format(
                    error=exc
                )
            ) from exc
        if not stat.S_ISDIR(backing_metadata.st_mode):
            raise LaunchError(_("cannot mount a game root that is not a directory"))
        if os.uname().machine != "x86_64":
            raise LaunchError(
                _("case-insensitive mount requires an x86_64 host; retry without --ci-mount")
            )
        try:
            library = _LibFuse(ctypes.CDLL(library_path))
        except OSError as exc:
            raise LaunchError(
                _("cannot load libfuse3 for the case-insensitive mount: {error}").format(error=exc)
            ) from exc
        state = _FuseState(backing_fd)
        callbacks = _CallbackSet(library, state)
        raw_argv = (b"box-cimount", b"-o", b"ro", b"-o", b"default_permissions")
        argv = (ctypes.c_char_p * len(raw_argv))(*raw_argv)
        args = _FuseArgs(len(raw_argv), argv, 0)
        raw_library = library.raw
        raw_session = raw_library.fuse_session_new(
            ctypes.byref(args),
            ctypes.byref(callbacks.ops),
            ctypes.sizeof(callbacks.ops),
            None,
        )
        session = int(raw_session) if raw_session else 0
        if not session:
            raise LaunchError(
                _("cannot create the case-insensitive mount session for {path}").format(
                    path=mountpoint
                )
            )
        if raw_library.fuse_session_mount(session, os.fspath(mountpoint).encode("utf-8")) != 0:
            with suppress(Exception):
                raw_library.fuse_session_destroy(session)
            raise LaunchError(
                _("cannot mount the case-insensitive view at {path}").format(path=mountpoint)
            )
        fuse_fd = library.session_fd(session)
        handle = CiMountSession(
            mountpoint,
            backing_fd=backing_fd,
            fuse_fd=fuse_fd,
            on_unmount=lambda: raw_library.fuse_session_unmount(session),
            on_destroy=lambda: raw_library.fuse_session_destroy(session),
            # Keep the ctypes objects alive with the launcher-side handle;
            # the daemon inherits its own copies across the fork below.
            live=(library, callbacks, argv, args, state),
        )
        try:
            middle = os.fork()
        except OSError as exc:
            with suppress(Exception):
                raw_library.fuse_session_unmount(session)
            with suppress(Exception):
                raw_library.fuse_session_destroy(session)
            raise LaunchError(
                _("cannot start the case-insensitive mount loop: {error}").format(error=exc)
            ) from exc
        if middle != 0:
            # Launcher side: reap the intermediate child, then probe.
            while True:
                try:
                    os.waitpid(middle, 0)
                    break
                except InterruptedError:
                    continue
                except OSError:
                    break
        else:
            try:
                with suppress(OSError):
                    os.setsid()
                try:
                    second = os.fork()
                except OSError:
                    os._exit(1)
                if second != 0:
                    os._exit(0)
                # Loop daemon: own the session from here on.
                _redirect_stdio_to_null()
                # Belt and braces: raise the soft descriptor ceiling
                # toward 65536 so even a pathological walk stays clear
                # of EMFILE.  Raising soft up to the hard limit needs no
                # privilege, and any failure here must never fail the
                # mount, so every step is best effort.
                with suppress(Exception):
                    _raise_daemon_nofile_limit()
                keep = {0, 1, 2, backing_fd}
                if fuse_fd >= 0:
                    keep.add(fuse_fd)
                _retain_only(keep)
                with suppress(Exception):
                    raw_library.fuse_session_loop(session)
                with suppress(Exception):
                    handle.unmount()
                with suppress(Exception):
                    handle.close()
                os._exit(0)
            finally:
                with suppress(BaseException):
                    os._exit(1)
        deadline = time.monotonic() + ready_timeout
        while True:
            if is_mountpoint_active(mountpoint):
                try:
                    os.stat(mountpoint)
                    break
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                # The daemon already forked and the mount may be up: tear the
                # session down here, mirroring the fork-failure path above.
                # Nobody else can own it (no handle escapes on this path, so
                # no supervisor will ever unmount it).
                with suppress(Exception):
                    raw_library.fuse_session_unmount(session)
                with suppress(Exception):
                    raw_library.fuse_session_destroy(session)
                raise LaunchError(
                    _(
                        "case-insensitive mount did not come up at {path}; retry without --ci-mount"
                    ).format(path=mountpoint)
                )
            time.sleep(_READY_POLL_INTERVAL)
        return handle
    except BaseException:
        with suppress(OSError):
            os.close(backing_fd)
        raise
