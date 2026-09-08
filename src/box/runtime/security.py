"""Pinned private files and persistent, cooperative cache locks."""

from __future__ import annotations

import fcntl
import os
import stat
import threading
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from box.errors import RuntimeError


def validate_private_file(descriptor: int) -> None:
    """Validate the opened inode before changing its contents or permissions."""
    entry = os.fstat(descriptor)
    if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_nlink != 1:
        raise RuntimeError("refusing unsafe download path: expected a private regular file")


def validate_runtime_links(root: Path) -> None:
    """Confine links to the normalized root that will actually be relocated.

    Unlike data filtering against the extraction directory, this also excludes
    siblings left in staging. Resolve components within the final namespace,
    including missing targets; even leaving and reentering the staging root is
    unsafe when its name changes at publication. Absolute links cannot relocate.
    """
    try:
        root = root.resolve(strict=True)
        pending = [root]
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        _validate_link_target(root, Path(entry.path))
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
    except OSError as exc:
        raise RuntimeError(f"cannot resolve runtime symlink safely: {exc}") from exc


def _validate_link_target(root: Path, link: Path) -> None:
    """Resolve without ever consulting the staging root's external neighbors."""
    pending = deque(link.relative_to(root).parts)
    current = root
    followed = 0
    while pending:
        component = pending.popleft()
        if component == "..":
            if current == root:
                raise RuntimeError("runtime symlink escapes the final runtime root")
            current = current.parent
            continue
        candidate = current / component
        try:
            entry = candidate.lstat()
        except FileNotFoundError:
            current = candidate
            continue
        if not stat.S_ISLNK(entry.st_mode):
            current = candidate
            continue
        followed += 1
        if followed > 40:
            raise RuntimeError("cannot resolve runtime symlink safely: cyclic or excessive chain")
        target = Path(os.readlink(candidate))
        if target.is_absolute():
            raise RuntimeError("runtime symlink escapes the final runtime root")
        pending.extendleft(reversed(target.parts))


class _Locks(threading.local):
    def __init__(self) -> None:
        self.held: set[tuple[int, int, int, str]] = set()


_locks = _Locks()


@contextmanager
def cache_lock(directory: int, name: str) -> Generator[None]:
    """Fail fast when busy; never unlink lock inodes, including after failures.

    Reentrant only in the owning thread, so installers can call download helpers
    while holding the same lock through publication and staging cleanup.
    """
    if not name or name in {".", ".."} or "/" in name or name != name.lower():
        raise RuntimeError("refusing unsafe cache lock name")
    name = name.removesuffix(".part")
    entry = os.fstat(directory)
    key = (os.getpid(), entry.st_dev, entry.st_ino, name)
    if key in _locks.held:
        yield
        return
    descriptor = os.open(
        f".{name}.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
        dir_fd=directory,
    )
    try:
        validate_private_file(descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"runtime cache entry is busy: {name}") from exc
        _locks.held.add(key)
        try:
            yield
        finally:
            _locks.held.remove(key)
    finally:
        os.close(descriptor)
