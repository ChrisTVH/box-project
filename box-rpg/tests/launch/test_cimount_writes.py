"""Writable case-insensitive mount layer tests (mutating ops and policy)."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import ctypes
import errno
import os
import stat
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from box.launch import cimount
from box.launch.cimount import (
    CI_MOUNT_DIRNAME,
    clear_resolver_cache,
    drop_stale_ci_mount,
    force_unmount,
    is_available,
    mount_ci_mount,
)


class _WriteLog:
    """Record low-level replies so write-path units run without a real mount."""

    def __init__(self) -> None:
        self.errors: list[tuple[int, int]] = []
        self.entries: list[int] = []
        self.creates: list[tuple[int, int]] = []
        self.opens: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int]] = []
        self.attrs: list[tuple[int, int, int]] = []
        self.bufs: list[tuple[int, bytes, int]] = []
        self.added: list[tuple[bytes, int]] = []

    def fuse_reply_err(self, req: int, code: int) -> int:
        self.errors.append((req, code))
        return 0

    def fuse_reply_none(self, req: int) -> None:
        return None

    def fuse_reply_entry(self, req: int, param: object) -> int:
        self.entries.append(req)
        return 0

    def fuse_reply_create(self, req: int, param: object, info: object) -> int:
        self.creates.append((req, _reply_fh(info)))
        return 0

    def fuse_reply_attr(self, req: int, attr: object, timeout: object) -> int:
        with suppress(Exception):
            recovered = ctypes.cast(cast("Any", attr), ctypes.POINTER(cimount._Stat)).contents
            self.attrs.append((req, int(recovered.st_size), int(recovered.st_mode)))
        return 0

    def fuse_reply_open(self, req: int, info: object) -> int:
        self.opens.append((req, _reply_fh(info)))
        return 0

    def fuse_reply_write(self, req: int, count: int) -> int:
        self.writes.append((req, int(count)))
        return 0

    def fuse_reply_buf(self, req: int, buf: object, size: int) -> int:
        with suppress(Exception):
            self.bufs.append((req, bytes(cast("Any", buf)[:size]), size))
        return 0

    def fuse_add_direntry(
        self, req: int, buf: object, bufsize: int, name: bytes, stbuf: object, off: int
    ) -> int:
        """Mimic the documented sizing: always return the padded space needed."""
        needed = ((24 + len(name) + 7) // 8) * 8
        if needed <= bufsize:
            self.added.append((name, off))
        return needed


def _reply_fh(info: object) -> int:
    """Read the fh id out of a recorded open/create reply buffer."""
    try:
        address = ctypes.addressof(cast("Any", info))
    except Exception:
        return 0
    return int(ctypes.c_uint64.from_address(address + cimount._FH_OFFSET).value)


def _harness(root: Path) -> tuple[cimount._CallbackSet, cimount._FuseState, _WriteLog]:
    """Build real callbacks over a stub library; needs no libfuse3 or mount."""
    raw = _WriteLog()
    library = cast("cimount._LibFuse", SimpleNamespace(raw=raw))
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    state = cimount._FuseState(descriptor)
    return cimount._CallbackSet(library, state), state, raw


def _close_all(state: cimount._FuseState) -> None:
    """Release every descriptor a write-path unit test retained (dirs and handles)."""
    for handle in list(state.handles.values()):
        with suppress(OSError):
            os.close(handle.fd)
    state.handles.clear()
    for record in list(state.inodes.values()):
        if record.fd >= 0:
            with suppress(OSError):
                os.close(record.fd)
    cimount._close_state_debug_log(state)


def _fi_block(handle_id: int) -> Any:
    """Build a fake fuse_file_info block carrying one fh id (caller keeps it alive)."""
    block = ctypes.create_string_buffer(cimount._FILE_INFO_REPLY_SIZE)
    ctypes.c_uint64.from_address(ctypes.addressof(block) + cimount._FH_OFFSET).value = handle_id
    return block


def _flags_block(flags: int) -> Any:
    """Build a fake fuse_file_info leading word carrying open flags."""
    return ctypes.c_int(flags)


def _ino_of(state: cimount._FuseState, parent: int, name: str) -> int:
    """Find the synthetic ino registered for one (parent, name) pair."""
    for ino, record in state.inodes.items():
        if record.parent == parent and record.name == name:
            return ino
    raise AssertionError(f"no inode registered for {name!r}")


def _listed_names(raw: _WriteLog) -> list[str]:
    """Return the non-dot names recorded by the last readdir reply."""
    return sorted(name.decode("utf-8") for name, _ in raw.added if name not in (b".", b".."))


def _failures(raw: _WriteLog) -> list[tuple[int, int]]:
    """Return only the failing replies (success is ``(req, 0)``)."""
    return [entry for entry in raw.errors if entry[1] != 0]


def _live_handle_id(raw: _WriteLog) -> int:
    """Return the last nonzero fh id from open/create replies."""
    for _, handle_id in reversed(raw.opens + raw.creates):
        if handle_id:
            return handle_id
    raise AssertionError("no fh id was replied")


@pytest.fixture(autouse=True)
def _clean_cache() -> Generator[None]:  # pyright: ignore[reportUnusedFunction]
    clear_resolver_cache()
    yield
    clear_resolver_cache()


def _open_dir(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a/bc", "/lead", "trail/"])
def test_mutation_names_reject_non_single_components(tmp_path: Path, name: str) -> None:
    descriptor = _open_dir(tmp_path)
    try:
        assert cimount._check_mutation_name(descriptor, name) == errno.ENOENT
    finally:
        os.close(descriptor)


def test_mutation_names_accept_plain_names_and_enforce_name_max(tmp_path: Path) -> None:
    descriptor = _open_dir(tmp_path)
    try:
        limit = os.pathconf(descriptor, "PC_NAME_MAX")
        assert limit > 0
        assert cimount._check_mutation_name(descriptor, "save.dat") is None
        assert cimount._check_mutation_name(descriptor, "x" * limit) is None
        assert cimount._check_mutation_name(descriptor, "x" * (limit + 1)) == errno.ENAMETOOLONG
    finally:
        os.close(descriptor)


def test_symlink_hardlink_special_xattr_ops_stay_unimplemented(tmp_path: Path) -> None:
    """Symlinks, hardlinks, specials, and xattrs keep NULL handlers (ENOSYS)."""
    root = tmp_path / "game"
    root.mkdir()
    callbacks, state, _raw = _harness(root)
    try:
        for name in (
            "symlink",
            "link",
            "mknod",
            "setxattr",
            "getxattr",
            "listxattr",
            "removexattr",
        ):
            assert not getattr(callbacks.ops, name), name
        for name in (
            "lookup",
            "forget",
            "getattr",
            "setattr",
            "mkdir",
            "unlink",
            "rmdir",
            "rename",
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
            "create",
        ):
            assert getattr(callbacks.ops, name), name
    finally:
        _close_all(state)


def test_create_write_read_release_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    callbacks, state, raw = _harness(root)
    try:
        start = len(os.listdir("/proc/self/fd"))
        callbacks._on_create(1, cimount._FUSE_ROOT_ID, b"save.dat", 0o644, 0)
        assert not _failures(raw)
        assert len(raw.creates) == 1
        child = _ino_of(state, cimount._FUSE_ROOT_ID, "save.dat")
        handle_id = raw.creates[-1][1]
        assert handle_id != 0
        info = _fi_block(handle_id)
        callbacks._on_write(2, child, b"hello-bytes", 11, 0, ctypes.addressof(info))
        assert raw.writes == [(2, 11)]
        assert not [code for _, code in raw.errors if code]
        callbacks._on_read(3, child, 64, 0, ctypes.addressof(info))
        assert not [code for _, code in raw.errors if code]
        assert raw.bufs[-1][1] == b"hello-bytes"
        assert (root / "save.dat").read_bytes() == b"hello-bytes"
        callbacks._on_release(4, child, ctypes.addressof(info))
        assert (4, 0) in raw.errors
        assert state.handles == {}
        assert len(os.listdir("/proc/self/fd")) - start == 0
    finally:
        _close_all(state)


def test_create_folded_equal_name_refuses_eexist_and_leaves_backing_untouched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "Save.dat").write_bytes(b"original")
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_create(1, cimount._FUSE_ROOT_ID, b"save.dat", 0o644, 0)
        assert raw.errors == [(1, errno.EEXIST)]
        assert (root / "Save.dat").read_bytes() == b"original"
        assert not (root / "save.dat").exists()
        raw.errors.clear()
        callbacks._on_create(2, cimount._FUSE_ROOT_ID, b"Save.dat", 0o644, 0)
        assert raw.errors == [(2, errno.EEXIST)]
        assert (root / "Save.dat").read_bytes() == b"original"
    finally:
        _close_all(state)


def test_create_and_mkdir_strip_group_other_write_bits(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    previous = os.umask(0)
    try:
        callbacks, state, raw = _harness(root)
        try:
            callbacks._on_create(1, cimount._FUSE_ROOT_ID, b"f.dat", 0o666, 0)
            assert not _failures(raw)
            assert stat.S_IMODE(os.stat(root / "f.dat").st_mode) == 0o644
            callbacks._on_mkdir(2, cimount._FUSE_ROOT_ID, b"sub", 0o777)
            assert not _failures(raw)
            assert stat.S_IMODE(os.stat(root / "sub").st_mode) == 0o755
        finally:
            _close_all(state)
    finally:
        os.umask(previous)


def test_open_truncates_atomically_and_append_emulates_at_end(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "data.bin").write_bytes(b"0123456789")
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_lookup(1, cimount._FUSE_ROOT_ID, b"data.bin")
        assert not _failures(raw)
        child = _ino_of(state, cimount._FUSE_ROOT_ID, "data.bin")
        trunc_flags = _flags_block(os.O_WRONLY | cimount._O_TRUNC)
        callbacks._on_open(2, child, ctypes.addressof(trunc_flags))
        assert not _failures(raw)
        assert (root / "data.bin").read_bytes() == b""
        release = _fi_block(_live_handle_id(raw))
        callbacks._on_release(3, child, ctypes.addressof(release))
        (root / "data.bin").write_bytes(b"AB")
        append_flags = _flags_block(os.O_WRONLY | os.O_APPEND)
        callbacks._on_open(4, child, ctypes.addressof(append_flags))
        assert not _failures(raw)
        append = _fi_block(_live_handle_id(raw))
        append_addr = ctypes.addressof(append)
        callbacks._on_write(5, child, b"cd", 2, 0, append_addr)
        callbacks._on_write(6, child, b"ef", 2, 0, append_addr)
        assert raw.writes[-2:] == [(5, 2), (6, 2)]
        assert (root / "data.bin").read_bytes() == b"ABcdef"
        callbacks._on_release(7, child, append_addr)
        rw_flags = _flags_block(os.O_RDWR)
        callbacks._on_open(8, child, ctypes.addressof(rw_flags))
        assert not _failures(raw)
        rw = _fi_block(_live_handle_id(raw))
        callbacks._on_write(9, child, b"XY", 2, 1, ctypes.addressof(rw))
        assert (root / "data.bin").read_bytes() == b"AXYdef"
    finally:
        _close_all(state)


def test_write_with_unknown_handle_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "note.txt").write_bytes(b"kept")
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_lookup(1, cimount._FUSE_ROOT_ID, b"note.txt")
        assert not _failures(raw)
        child = _ino_of(state, cimount._FUSE_ROOT_ID, "note.txt")
        bogus = _fi_block(0x9F7D3A1B)
        callbacks._on_write(2, child, b"evil", 4, 0, ctypes.addressof(bogus))
        assert raw.errors[-1] == (2, errno.EIO)
        assert (root / "note.txt").read_bytes() == b"kept"
        callbacks._on_write(3, 0xDEAD, b"evil", 4, 0, ctypes.addressof(bogus))
        assert raw.errors[-1] == (3, errno.ENOENT)
    finally:
        _close_all(state)


def test_setattr_truncates_and_retimes_but_refuses_mode_owner(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "blob.bin").write_bytes(b"0123456789abcdefghij")
    before_mode = stat.S_IMODE(os.stat(root / "blob.bin").st_mode)
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_lookup(1, cimount._FUSE_ROOT_ID, b"blob.bin")
        assert not _failures(raw)
        child = _ino_of(state, cimount._FUSE_ROOT_ID, "blob.bin")
        size_attr = cimount._Stat()
        size_attr.st_size = 6
        callbacks._on_setattr(2, child, ctypes.addressof(size_attr), cimount._FUSE_SET_ATTR_SIZE, 0)
        assert not _failures(raw)
        assert (root / "blob.bin").read_bytes() == b"012345"
        assert raw.attrs[-1][1] == 6
        time_attr = cimount._Stat()
        time_attr.st_atim.tv_sec = 1234567
        time_attr.st_atim.tv_nsec = 890123
        time_attr.st_mtim.tv_sec = 2345678
        time_attr.st_mtim.tv_nsec = 456789
        callbacks._on_setattr(
            3,
            child,
            ctypes.addressof(time_attr),
            cimount._FUSE_SET_ATTR_ATIME | cimount._FUSE_SET_ATTR_MTIME,
            0,
        )
        assert not _failures(raw)
        live = os.stat(root / "blob.bin")
        assert live.st_atime_ns == 1234567 * 1_000_000_000 + 890123
        assert live.st_mtime_ns == 2345678 * 1_000_000_000 + 456789
        mode_attr = cimount._Stat()
        mode_attr.st_mode = 0o600
        for req, bit in (
            (4, cimount._FUSE_SET_ATTR_MODE),
            (5, cimount._FUSE_SET_ATTR_UID),
            (6, cimount._FUSE_SET_ATTR_GID),
        ):
            callbacks._on_setattr(req, child, ctypes.addressof(mode_attr), bit, 0)
        assert raw.errors[-3:] == [(4, errno.EPERM), (5, errno.EPERM), (6, errno.EPERM)]
        assert stat.S_IMODE(os.stat(root / "blob.bin").st_mode) == before_mode
    finally:
        _close_all(state)


def test_mkdir_unlink_rmdir_kind_checks(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "note.txt").write_bytes(b"note")
    os.symlink("note.txt", root / "link.txt")
    os.mkfifo(root / "pipe.fifo")
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_mkdir(1, cimount._FUSE_ROOT_ID, b"sub", 0o755)
        assert not _failures(raw)
        assert (root / "sub").is_dir()
        callbacks._on_mkdir(2, cimount._FUSE_ROOT_ID, b"SUB", 0o755)
        assert raw.errors == [(2, errno.EEXIST)]
        assert not (root / "SUB").exists()
        raw.errors.clear()
        callbacks._on_unlink(3, cimount._FUSE_ROOT_ID, b"sub")
        assert raw.errors == [(3, errno.EISDIR)]
        callbacks._on_rmdir(4, cimount._FUSE_ROOT_ID, b"note.txt")
        assert raw.errors[-1] == (4, errno.ENOTDIR)
        callbacks._on_unlink(5, cimount._FUSE_ROOT_ID, b"link.txt")
        assert raw.errors[-1] == (5, errno.EPERM)
        assert os.path.islink(root / "link.txt")
        callbacks._on_rmdir(6, cimount._FUSE_ROOT_ID, b"link.txt")
        assert raw.errors[-1] == (6, errno.EPERM)
        callbacks._on_unlink(7, cimount._FUSE_ROOT_ID, b"pipe.fifo")
        assert raw.errors[-1] == (7, errno.EPERM)
        callbacks._on_unlink(8, cimount._FUSE_ROOT_ID, b"missing.txt")
        assert raw.errors[-1][1] == errno.ENOENT
        raw.errors.clear()
        # A non-empty directory refuses with ENOTEMPTY, naturally.
        callbacks._on_lookup(9, cimount._FUSE_ROOT_ID, b"sub")
        assert not [code for _, code in raw.errors if code]
        sub = _ino_of(state, cimount._FUSE_ROOT_ID, "sub")
        callbacks._on_create(10, sub, b"inner.txt", 0o644, 0)
        assert not [code for _, code in raw.errors if code]
        callbacks._on_rmdir(11, cimount._FUSE_ROOT_ID, b"sub")
        assert raw.errors[-1] == (11, errno.ENOTEMPTY)
        callbacks._on_unlink(12, sub, b"inner.txt")
        assert raw.errors[-1] == (12, 0)
        callbacks._on_rmdir(13, cimount._FUSE_ROOT_ID, b"sub")
        assert raw.errors[-1] == (13, 0)
        assert not (root / "sub").exists()
        callbacks._on_unlink(14, cimount._FUSE_ROOT_ID, b"note.txt")
        assert raw.errors[-1] == (14, 0)
        assert not (root / "note.txt").exists()
    finally:
        _close_all(state)


def test_rename_case_only_cross_dir_and_cache_invalidation(tmp_path: Path) -> None:
    root = tmp_path / "game"
    (root / "sub").mkdir(parents=True)
    (root / "save.dat").write_bytes(b"payload")
    callbacks, state, raw = _harness(root)
    try:
        parent_fd = state.inodes[cimount._FUSE_ROOT_ID].fd
        assert cimount.resolve_insensitive_name(parent_fd, "SAVE.DAT") == "save.dat"
        parent_key = os.fstat(parent_fd)
        assert (parent_key.st_dev, parent_key.st_ino) in cimount._resolver_cache
        callbacks._on_lookup(1, cimount._FUSE_ROOT_ID, b"save.dat")
        callbacks._on_lookup(2, cimount._FUSE_ROOT_ID, b"sub")
        assert not _failures(raw)
        child = _ino_of(state, cimount._FUSE_ROOT_ID, "save.dat")
        sub = _ino_of(state, cimount._FUSE_ROOT_ID, "sub")
        callbacks._on_rename(
            3, cimount._FUSE_ROOT_ID, b"save.dat", cimount._FUSE_ROOT_ID, b"SAVE.DAT", 0
        )
        assert raw.errors == [(3, 0)]
        assert (root / "SAVE.DAT").read_bytes() == b"payload"
        assert not (root / "save.dat").exists()
        # The live record followed the case-only rename: getattr still resolves.
        callbacks._on_getattr(4, child, 0)
        assert not [code for _, code in raw.errors if code]
        assert raw.attrs[-1][1] == len(b"payload")
        callbacks._on_rename(5, cimount._FUSE_ROOT_ID, b"SAVE.DAT", sub, b"moved.dat", 0)
        assert raw.errors[-1] == (5, 0)
        assert (root / "sub" / "moved.dat").read_bytes() == b"payload"
        callbacks._on_getattr(6, child, 0)
        assert not [code for _, code in raw.errors if code]
        callbacks._on_rename(7, cimount._FUSE_ROOT_ID, b"gone.dat", sub, b"x.dat", 0)
        assert raw.errors[-1][1] == errno.ENOENT
        callbacks._on_rename(8, cimount._FUSE_ROOT_ID, b"SAVE.DAT", sub, b"y.dat", 1)
        assert raw.errors[-1] == (8, errno.EINVAL)
        # Both parents were invalidated through the central helper.
        assert (parent_key.st_dev, parent_key.st_ino) not in cimount._resolver_cache
    finally:
        _close_all(state)


def test_rename_refuses_symlink_source(tmp_path: Path) -> None:
    root = tmp_path / "game"
    (root / "sub").mkdir(parents=True)
    (root / "real.txt").write_bytes(b"real")
    os.symlink("real.txt", root / "link.txt")
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_lookup(1, cimount._FUSE_ROOT_ID, b"sub")
        assert not _failures(raw)
        sub = _ino_of(state, cimount._FUSE_ROOT_ID, "sub")
        callbacks._on_rename(2, cimount._FUSE_ROOT_ID, b"link.txt", sub, b"moved.txt", 0)
        assert raw.errors == [(2, errno.EPERM)]
        assert os.path.islink(root / "link.txt")
        assert not (root / "sub" / "moved.txt").exists()
    finally:
        _close_all(state)


def test_flush_succeeds_and_fsync_syncs(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_create(1, cimount._FUSE_ROOT_ID, b"sync.dat", 0o644, 0)
        assert not _failures(raw)
        child = _ino_of(state, cimount._FUSE_ROOT_ID, "sync.dat")
        handle_id = raw.creates[-1][1]
        info = _fi_block(handle_id)
        addr = ctypes.addressof(info)
        callbacks._on_write(2, child, b"data", 4, 0, addr)
        callbacks._on_flush(3, child, addr)
        assert raw.errors[-1] == (3, 0)
        callbacks._on_fsync(4, child, 0, addr)
        assert raw.errors[-1] == (4, 0)
        callbacks._on_release(5, child, addr)
        # Without a live handle the sync falls back to a transient open.
        callbacks._on_fsync(6, child, 1, 0)
        assert raw.errors[-1] == (6, 0)
        callbacks._on_lookup(7, cimount._FUSE_ROOT_ID, b"sync.dat")
        callbacks._on_fsyncdir(8, cimount._FUSE_ROOT_ID, 0, 0)
        assert raw.errors[-1] == (8, 0)
        callbacks._on_fsyncdir(9, child, 0, 0)
        assert raw.errors[-1] == (9, errno.ENOTDIR)
        callbacks._on_flush(10, 0xDEAD, 0)
        assert raw.errors[-1] == (10, errno.ENOENT)
    finally:
        _close_all(state)


def test_write_open_release_cycles_do_not_grow_descriptors(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "cycle.dat").write_bytes(b"x")
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_lookup(1, cimount._FUSE_ROOT_ID, b"cycle.dat")
        assert not _failures(raw)
        child = _ino_of(state, cimount._FUSE_ROOT_ID, "cycle.dat")
        start = len(os.listdir("/proc/self/fd"))
        for index in range(20):
            req = 10 + index * 3
            rw_flags = _flags_block(os.O_RDWR)
            callbacks._on_open(req, child, ctypes.addressof(rw_flags))
            info = _fi_block(_live_handle_id(raw))
            addr = ctypes.addressof(info)
            callbacks._on_write(req + 1, child, b"y", 1, 0, addr)
            callbacks._on_release(req + 2, child, addr)
        assert all(code == 0 for _, code in raw.errors)
        assert state.handles == {}
        assert len(os.listdir("/proc/self/fd")) - start == 0
        assert (root / "cycle.dat").read_bytes() == b"y"
    finally:
        _close_all(state)


def test_mutations_invalidate_caches_and_readdir_reflects_them(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    callbacks, state, raw = _harness(root)
    try:
        parent = cimount._FUSE_ROOT_ID
        parent_fd = state.inodes[parent].fd
        assert cimount.resolve_insensitive_name(parent_fd, "A.TXT") == "a.txt"
        key = os.fstat(parent_fd)
        assert (key.st_dev, key.st_ino) in cimount._resolver_cache
        callbacks._on_opendir(1, parent, 0)
        callbacks._on_readdir(2, parent, 4096, 0, 0)
        assert not _failures(raw)
        assert _listed_names(raw) == ["a.txt"]
        callbacks._on_create(3, parent, b"b.txt", 0o644, 0)
        assert not _failures(raw)
        assert (key.st_dev, key.st_ino) not in cimount._resolver_cache
        raw.added.clear()
        callbacks._on_readdir(4, parent, 4096, 0, 0)
        assert not _failures(raw)
        assert _listed_names(raw) == ["a.txt", "b.txt"]
        callbacks._on_lookup(5, parent, b"B.TXT")
        assert not _failures(raw)
        assert _ino_of(state, parent, "b.txt")
        callbacks._on_unlink(6, parent, b"a.txt")
        assert raw.errors[-1] == (6, 0)
        raw.added.clear()
        callbacks._on_readdir(7, parent, 4096, 0, 0)
        assert not _failures(raw)
        assert _listed_names(raw) == ["b.txt"]
        callbacks._on_lookup(8, parent, b"a.txt")
        assert raw.errors[-1] == (8, errno.ENOENT)
    finally:
        _close_all(state)


@contextmanager
def _live_mount(tmp_path: Path) -> Generator[tuple[Path, Path]]:
    """Mount a small game tree; skip when the host cannot mount FUSE."""
    if not Path("/dev/fuse").exists():
        pytest.skip("no /dev/fuse device node")
    if not is_available():
        pytest.skip("no libfuse3 backend")
    game = tmp_path / "game"
    (game / "WWW").mkdir(parents=True)
    (game / "Save.dat").write_bytes(b"v1-payload")
    (game / "WWW" / "Main.js").write_bytes(b"console.log(1);")
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir(exist_ok=True)
    drop_stale_ci_mount(mountpoint)
    game_fd = _open_dir(game)
    try:
        session = mount_ci_mount(game_fd, mountpoint)
        try:
            yield game, mountpoint
        finally:
            session.disown()
            session.close()
            force_unmount(mountpoint)
    finally:
        os.close(game_fd)


def test_live_write_create_append_truncate_utime(tmp_path: Path) -> None:
    with _live_mount(tmp_path) as (game, mount):
        (mount / "new.txt").write_bytes(b"hello-live")
        assert (game / "new.txt").read_bytes() == b"hello-live"
        assert (mount / "NEW.txt").read_bytes() == b"hello-live"
        with (mount / "new.txt").open("ab") as handle:
            handle.write(b"++")
        assert (game / "new.txt").read_bytes() == b"hello-live++"
        with (mount / "new.txt").open("wb") as handle:
            handle.write(b"tiny")
        assert (game / "new.txt").read_bytes() == b"tiny"
        os.truncate(mount / "new.txt", 2)
        assert (mount / "new.txt").read_bytes() == b"ti"
        os.utime(mount / "new.txt", ns=(1234567890123, 2345678901234))
        live = os.stat(mount / "new.txt")
        assert live.st_atime_ns == 1234567890123
        assert live.st_mtime_ns == 2345678901234
        assert (mount / "WWW" / "main.js").read_bytes() == b"console.log(1);"
        assert "new.txt" in sorted(entry.name for entry in mount.iterdir())


def test_live_mkdir_rename_unlink_rmdir(tmp_path: Path) -> None:
    with _live_mount(tmp_path) as (game, mount):
        (mount / "sub").mkdir()
        assert (game / "sub").is_dir()
        (mount / "sub" / "inner.txt").write_text("inner", encoding="utf-8")
        # Note: a case-only rename (Save.dat -> SAVE.DAT) is a VFS-level
        # no-op that never reaches the daemon: both spellings resolve to the
        # same inode, so the kernel short-circuits rename-to-self with
        # success.  Case-only renames are covered at the handler layer
        # instead; live renames always use a distinct folded name.
        os.rename(mount / "Save.dat", mount / "renamed.dat")
        assert (game / "renamed.dat").read_bytes() == b"v1-payload"
        assert not (game / "Save.dat").exists()
        os.rename(mount / "renamed.dat", mount / "sub" / "moved.dat")
        assert (game / "sub" / "moved.dat").read_bytes() == b"v1-payload"
        os.unlink(mount / "sub" / "moved.dat")
        assert not (game / "sub" / "moved.txt").exists()
        assert not (game / "sub" / "moved.dat").exists()
        with pytest.raises(IsADirectoryError):
            os.unlink(mount / "sub")
        try:
            os.rmdir(mount / "sub")
        except OSError as exc:
            assert exc.errno == errno.ENOTEMPTY
        else:
            raise AssertionError("rmdir of a non-empty directory must fail")
        os.unlink(mount / "sub" / "inner.txt")
        os.rmdir(mount / "sub")
        assert not (game / "sub").exists()


def test_live_policy_refusals(tmp_path: Path) -> None:
    with _live_mount(tmp_path) as (game, mount):
        before_mode = stat.S_IMODE(os.stat(game / "Save.dat").st_mode)
        with pytest.raises(PermissionError):
            os.chmod(mount / "Save.dat", 0o600)
        assert stat.S_IMODE(os.stat(game / "Save.dat").st_mode) == before_mode
        with pytest.raises(OSError) as symlink_error:
            os.symlink("Save.dat", mount / "link.dat")
        assert symlink_error.value.errno == errno.ENOSYS
        assert not (mount / "link.dat").exists() and not os.path.lexists(mount / "link.dat")
        with pytest.raises(OSError) as mknod_error:
            os.mknod(mount / "node.dat")
        assert mknod_error.value.errno == errno.ENOSYS
        with pytest.raises(OSError) as xattr_error:
            os.setxattr(mount / "Save.dat", "user.box-test", b"value")
        assert xattr_error.value.errno in (errno.ENOSYS, errno.EOPNOTSUPP)
        with pytest.raises(FileExistsError):
            os.open(mount / "Save.dat", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with pytest.raises(FileExistsError):
            os.open(mount / "save.dat", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        assert (game / "Save.dat").read_bytes() == b"v1-payload"


def test_forget_while_open_still_serves_retained_handle(tmp_path: Path) -> None:
    """Reads and writes through a live handle survive forget of the inode."""
    root = tmp_path / "game"
    root.mkdir()
    (root / "stream.dat").write_bytes(b"0123456789")
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_lookup(1, cimount._FUSE_ROOT_ID, b"stream.dat")
        assert not _failures(raw)
        child = _ino_of(state, cimount._FUSE_ROOT_ID, "stream.dat")
        rw_flags = _flags_block(os.O_RDWR)
        callbacks._on_open(2, child, ctypes.addressof(rw_flags))
        assert not _failures(raw)
        info = _fi_block(_live_handle_id(raw))
        info_addr = ctypes.addressof(info)
        # The kernel forgets the inode while the description stays open.
        callbacks._on_forget(3, child, 1)
        assert child not in state.inodes
        callbacks._on_read(4, child, 4, 0, info_addr)
        assert not [code for _, code in raw.errors if code]
        assert raw.bufs[-1][1] == b"0123"
        callbacks._on_write(5, child, b"AB", 2, 0, info_addr)
        assert raw.writes == [(5, 2)]
        assert (root / "stream.dat").read_bytes() == b"AB23456789"
        callbacks._on_release(6, child, info_addr)
        assert (6, 0) in raw.errors
        assert state.handles == {}
    finally:
        _close_all(state)


def test_read_through_write_only_handle_fails_with_ebadf(tmp_path: Path) -> None:
    """A write-only open retains a handle that refuses reads."""
    root = tmp_path / "game"
    root.mkdir()
    (root / "wo.dat").write_bytes(b"secret-bytes")
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_lookup(1, cimount._FUSE_ROOT_ID, b"wo.dat")
        assert not _failures(raw)
        child = _ino_of(state, cimount._FUSE_ROOT_ID, "wo.dat")
        wo_flags = _flags_block(os.O_WRONLY)
        callbacks._on_open(2, child, ctypes.addressof(wo_flags))
        assert not _failures(raw)
        info = _fi_block(_live_handle_id(raw))
        callbacks._on_read(3, child, 64, 0, ctypes.addressof(info))
        assert raw.errors == [(3, errno.EBADF)]
        assert (root / "wo.dat").read_bytes() == b"secret-bytes"
    finally:
        _close_all(state)


def test_write_throttle_full_bucket_covers_rate(tmp_path: Path) -> None:
    """A full burst bucket covers one full rate worth of bytes with no sleep."""
    root = tmp_path / "game"
    root.mkdir()
    _callbacks, state, _raw = _harness(root)
    try:
        rate = cimount._WRITE_RATE_BYTES_PER_SEC
        now_ns = 5_000_000_000
        state.write_tokens = float(cimount._WRITE_BURST_BYTES)
        state.write_last_ns = now_ns
        assert cimount._throttle_consume(state, rate, now_ns) == 0.0
        assert state.write_tokens == 0.0
    finally:
        _close_all(state)


def test_write_throttle_empty_bucket_deficit_is_one_second(tmp_path: Path) -> None:
    """An empty bucket turns one rate-sized request into exactly 1.0 s."""
    root = tmp_path / "game"
    root.mkdir()
    _callbacks, state, _raw = _harness(root)
    try:
        rate = cimount._WRITE_RATE_BYTES_PER_SEC
        now_ns = 7_000_000_000
        state.write_tokens = 0.0
        state.write_last_ns = now_ns
        assert cimount._throttle_consume(state, rate, now_ns) == 1.0
        assert state.write_tokens == float(-rate)
    finally:
        _close_all(state)


def test_write_throttle_refill_across_calls(tmp_path: Path) -> None:
    """Elapsed time refills the bucket; same-timestamp overuse sleeps."""
    root = tmp_path / "game"
    root.mkdir()
    _callbacks, state, _raw = _harness(root)
    try:
        rate = cimount._WRITE_RATE_BYTES_PER_SEC
        start_ns = 9_000_000_000
        state.write_tokens = float(cimount._WRITE_BURST_BYTES)
        state.write_last_ns = start_ns
        # Drain the full burst at t=0: covered, bucket now empty.
        assert cimount._throttle_consume(state, rate, start_ns) == 0.0
        assert state.write_tokens == 0.0
        # Half a second later half a rate is covered again by refill.
        half_ns = start_ns + 500_000_000
        assert cimount._throttle_consume(state, rate // 2, half_ns) == 0.0
        assert state.write_tokens == 0.0
        # Same timestamp with no refill: one more byte must sleep.
        assert cimount._throttle_consume(state, 1, half_ns) == pytest.approx(1.0 / float(rate))
    finally:
        _close_all(state)


def test_write_throttle_small_writes_within_burst_never_sleep(tmp_path: Path) -> None:
    """Small writes well below burst capacity never sleep on a full bucket."""
    root = tmp_path / "game"
    root.mkdir()
    _callbacks, state, _raw = _harness(root)
    try:
        now_ns = 11_000_000_000
        state.write_tokens = float(cimount._WRITE_BURST_BYTES)
        state.write_last_ns = now_ns
        chunk = 1024 * 1024
        for _ in range(10):
            assert cimount._throttle_consume(state, chunk, now_ns) == 0.0
        assert state.write_tokens == float(cimount._WRITE_BURST_BYTES - 10 * chunk)
    finally:
        _close_all(state)


def test_write_throttle_on_write_sleeps_deficit_and_keeps_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drained bucket sleeps the computed deficit; bytes still land intact."""
    root = tmp_path / "game"
    root.mkdir()
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_create(1, cimount._FUSE_ROOT_ID, b"throttle.dat", 0o644, 0)
        assert not _failures(raw)
        child = _ino_of(state, cimount._FUSE_ROOT_ID, "throttle.dat")
        info = _fi_block(_live_handle_id(raw))
        info_addr = ctypes.addressof(info)
        fake_now = 13_000_000_000
        state.write_tokens = 0.0
        state.write_last_ns = fake_now
        monkeypatch.setattr(cimount.time, "monotonic_ns", lambda: fake_now)
        slept: list[float] = []
        monkeypatch.setattr(cimount.time, "sleep", slept.append)
        callbacks._on_write(2, child, b"xy", 2, 0, info_addr)
        expected = 2.0 / float(cimount._WRITE_RATE_BYTES_PER_SEC)
        assert slept == pytest.approx([expected])
        assert raw.writes == [(2, 2)]
        assert not _failures(raw)
        assert (root / "throttle.dat").read_bytes() == b"xy"
    finally:
        _close_all(state)


def test_write_throttle_zero_length_write_skips_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero-length writes reply 0 without consuming tokens or sleeping."""
    root = tmp_path / "game"
    root.mkdir()
    callbacks, state, raw = _harness(root)
    try:
        callbacks._on_create(1, cimount._FUSE_ROOT_ID, b"empty.dat", 0o644, 0)
        assert not _failures(raw)
        child = _ino_of(state, cimount._FUSE_ROOT_ID, "empty.dat")
        info = _fi_block(_live_handle_id(raw))
        info_addr = ctypes.addressof(info)
        fake_now = 17_000_000_000
        state.write_tokens = float(cimount._WRITE_BURST_BYTES)
        state.write_last_ns = fake_now
        monkeypatch.setattr(cimount.time, "monotonic_ns", lambda: fake_now)
        slept: list[float] = []
        monkeypatch.setattr(cimount.time, "sleep", slept.append)
        callbacks._on_write(2, child, b"", 0, 0, info_addr)
        assert slept == []
        assert raw.writes == [(2, 0)]
        assert raw.errors == []
        assert state.write_tokens == float(cimount._WRITE_BURST_BYTES)
        assert (root / "empty.dat").read_bytes() == b""
    finally:
        _close_all(state)
