"""Case-insensitive mount module tests (resolver, guards, ownership, handover)."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import ctypes
import errno
import os
import resource
import stat
import subprocess
from collections.abc import Generator
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from box.errors import ConfigurationError, LaunchError
from box.launch import cimount
from box.launch.cimount import (
    CI_MOUNT_DIRNAME,
    CiMountSession,
    clear_resolver_cache,
    drop_stale_ci_mount,
    ensure_profile_ci_mount_path,
    force_unmount,
    is_available,
    is_mountpoint_active,
    mount_ci_mount,
    require_libfuse3,
    resolve_insensitive_name,
)
from box.launch.sandbox import validate_tree
from box.paths import AppPaths


def _open_dir(path: Path) -> int:
    """Open a real directory descriptor for resolver tests."""
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _game_tree(root: Path) -> Path:
    """Create a small game-like tree with mixed-case names."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "Readme.md").write_text("readme", encoding="utf-8")
    (root / "ASSET.dat").write_text("asset-bytes", encoding="utf-8")
    (root / "WWW").mkdir(exist_ok=True)
    (root / "WWW" / "Main.JS").write_text("console.log(1);", encoding="utf-8")
    return root


def _no_library(name: str) -> str | None:
    return None


def _fake_library(name: str) -> str | None:
    assert name == "fuse3"
    return "libfuse3.so.3"


def _fake_helper() -> Path | None:
    return Path("/usr/bin/fusermount3")


def _no_helper() -> Path | None:
    return None


def _always_active(path: Path) -> bool:
    return True


@pytest.fixture(autouse=True)
def _clean_cache() -> Generator[None]:  # pyright: ignore[reportUnusedFunction]
    clear_resolver_cache()
    yield
    clear_resolver_cache()


def test_resolve_exact_hit_without_scandir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = _game_tree(tmp_path / "game")
    calls = [0]
    real_scandir = os.scandir

    def counting(
        path: str | bytes | int | os.PathLike[str] | os.PathLike[bytes],
    ) -> object:
        calls[0] += 1
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting)
    descriptor = _open_dir(game)
    try:
        assert resolve_insensitive_name(descriptor, "Readme.md") == "Readme.md"
        assert resolve_insensitive_name(descriptor, "WWW") == "WWW"
    finally:
        os.close(descriptor)
    assert calls == [0]


def test_resolve_casefold_hit(tmp_path: Path) -> None:
    game = _game_tree(tmp_path / "game")
    descriptor = _open_dir(game)
    try:
        assert resolve_insensitive_name(descriptor, "readme.md") == "Readme.md"
        assert resolve_insensitive_name(descriptor, "README.MD") == "Readme.md"
        assert resolve_insensitive_name(descriptor, "asset.DAT") == "ASSET.dat"
        assert resolve_insensitive_name(descriptor, "www") == "WWW"
    finally:
        os.close(descriptor)


def test_resolve_miss_returns_none(tmp_path: Path) -> None:
    game = _game_tree(tmp_path / "game")
    descriptor = _open_dir(game)
    try:
        assert resolve_insensitive_name(descriptor, "missing.txt") is None
        assert resolve_insensitive_name(descriptor, "Readme.md.bak") is None
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "/abs", "WWW/Main.JS"])
def test_resolve_rejects_non_single_components(tmp_path: Path, name: str) -> None:
    game = _game_tree(tmp_path / "game")
    descriptor = _open_dir(game)
    try:
        assert resolve_insensitive_name(descriptor, name) is None
    finally:
        os.close(descriptor)


def test_resolve_refuses_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    game = _game_tree(tmp_path / "game")
    os.symlink(outside / "secret.txt", game / "leak.txt")
    os.symlink(game / "Readme.md", game / "inner.txt")
    os.symlink("nowhere-missing", game / "dangling.txt")
    descriptor = _open_dir(game)
    try:
        # Exact hits on symlinks are refused, even when the target is inside.
        assert resolve_insensitive_name(descriptor, "leak.txt") is None
        assert resolve_insensitive_name(descriptor, "inner.txt") is None
        assert resolve_insensitive_name(descriptor, "dangling.txt") is None
        # Case-fold scans never hand symlinks out either.
        assert resolve_insensitive_name(descriptor, "LEAK.TXT") is None
        assert resolve_insensitive_name(descriptor, "INNER.TXT") is None
    finally:
        os.close(descriptor)


def test_resolve_caches_last_readdir_per_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _game_tree(tmp_path / "game")
    calls = [0]
    real_scandir = os.scandir

    def counting(
        path: str | bytes | int | os.PathLike[str] | os.PathLike[bytes],
    ) -> object:
        calls[0] += 1
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting)
    descriptor = _open_dir(game)
    try:
        assert resolve_insensitive_name(descriptor, "readme.md") == "Readme.md"
        assert calls == [1]
        # Further folded lookups in the same directory reuse the listing.
        assert resolve_insensitive_name(descriptor, "README.MD") == "Readme.md"
        assert resolve_insensitive_name(descriptor, "asset.dat") == "ASSET.dat"
        assert resolve_insensitive_name(descriptor, "nope.txt") is None
        assert calls == [1]
        # Clearing the cache forces exactly one fresh scan.
        clear_resolver_cache()
        assert resolve_insensitive_name(descriptor, "readme.md") == "Readme.md"
        assert calls == [2]
    finally:
        os.close(descriptor)


def test_resolve_cache_invalidated_by_directory_mtime(tmp_path: Path) -> None:
    game = _game_tree(tmp_path / "game")
    descriptor = _open_dir(game)
    try:
        assert resolve_insensitive_name(descriptor, "late.txt") is None
        (game / "LATE.txt").write_text("late", encoding="utf-8")
        metadata = os.stat(game)
        os.utime(game, ns=(metadata.st_atime_ns + 1_000_000, metadata.st_mtime_ns + 1_000_000))
        assert resolve_insensitive_name(descriptor, "late.txt") == "LATE.txt"
    finally:
        os.close(descriptor)


def test_open_child_missing_name_reports_open_step(tmp_path: Path) -> None:
    game = _game_tree(tmp_path / "game")
    descriptor = _open_dir(game)
    try:
        result = cimount._open_child(descriptor, "no-such-file.txt")
    finally:
        os.close(descriptor)
    assert result.fd == -1
    assert result.metadata is None
    assert result.step == "open"
    assert result.errno_code == errno.ENOENT


def test_open_child_fstat_failure_reports_fstat_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _game_tree(tmp_path / "game")
    real_close = os.close
    closed: list[int] = []

    def flaky_fstat(fd: int) -> os.stat_result:
        raise OSError(errno.EIO, "forced fstat failure")

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "fstat", flaky_fstat)
    monkeypatch.setattr(os, "close", recording_close)
    descriptor = _open_dir(game)
    try:
        result = cimount._open_child(descriptor, "Readme.md")
    finally:
        os.close(descriptor)
    assert result.fd == -1
    assert result.metadata is None
    assert result.step == "fstat"
    assert result.errno_code == errno.EIO
    # The failed child descriptor is closed; only the harness fd follows.
    assert len(closed) == 2
    assert closed[0] != descriptor


def test_open_child_symlink_branch_reports_policy_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Branch-only unit: a real O_NOFOLLOW open fails with ELOOP before ever
    # reaching the S_ISLNK check (see the real-wire test below), so steer a
    # real open plus the real symlink lstat into the policy branch explicitly.
    game = _game_tree(tmp_path / "game")
    os.symlink(game / "Readme.md", game / "link.txt")
    link_metadata = os.stat(game / "link.txt", follow_symlinks=False)
    assert stat.S_ISLNK(link_metadata.st_mode)
    real_close = os.close
    closed: list[int] = []

    def symlink_fstat(fd: int) -> os.stat_result:
        # A real open plus the real symlink lstat steers into the policy branch.
        return link_metadata

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "fstat", symlink_fstat)
    monkeypatch.setattr(os, "close", recording_close)
    descriptor = _open_dir(game)
    try:
        result = cimount._open_child(descriptor, "Readme.md")
    finally:
        os.close(descriptor)
    assert result.fd == -1
    assert result.metadata is None
    assert result.step == "symlink"
    assert result.errno_code is None
    assert len(closed) == 2
    assert closed[0] != descriptor


def test_open_child_symlink_real_open_reports_open_step(tmp_path: Path) -> None:
    # Production wiring: O_NOFOLLOW makes the open itself fail with ELOOP,
    # so a real symlink surfaces as an open-step failure, not the policy step.
    game = _game_tree(tmp_path / "game")
    os.symlink(game / "Readme.md", game / "link.txt")
    descriptor = _open_dir(game)
    try:
        result = cimount._open_child(descriptor, "link.txt")
    finally:
        os.close(descriptor)
    assert result.fd == -1
    assert result.metadata is None
    assert result.step == "open"
    assert result.errno_code == errno.ELOOP


def test_open_child_dir_reopen_failure_reports_reopen_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _game_tree(tmp_path / "game")
    real_open = os.open
    real_close = os.close
    dir_opens = [0]
    closed: list[int] = []

    def flaky_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        **kwargs: Any,
    ) -> int:
        # Let the harness open its own directory descriptor; fail only the
        # directory reopen inside _open_child.
        if flags & os.O_DIRECTORY:
            dir_opens[0] += 1
            if dir_opens[0] > 1:
                raise OSError(errno.EACCES, "forced dir reopen failure")
        return real_open(path, flags, mode, **kwargs)

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", flaky_open)
    monkeypatch.setattr(os, "close", recording_close)
    descriptor = _open_dir(game)
    try:
        result = cimount._open_child(descriptor, "WWW")
    finally:
        os.close(descriptor)
    assert result.fd == -1
    assert result.metadata is None
    assert result.step == "reopen"
    assert result.errno_code == errno.EACCES
    # The first child descriptor is closed exactly once: a failed reopen must
    # never close the already-closed descriptor a second time.
    assert len(closed) == 2
    assert closed[0] != descriptor
    assert closed[0] != closed[1]


def test_open_child_dir_reopen_fstat_failure_reports_reopen_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _game_tree(tmp_path / "game")
    real_fstat = os.fstat
    real_close = os.close
    closed: list[int] = []
    calls = [0]

    def flaky_fstat(fd: int) -> os.stat_result:
        # Let the first stat (classifying the directory) through; fail only
        # the stat of the reopened descriptor.
        calls[0] += 1
        if calls[0] > 1:
            raise OSError(errno.EIO, "forced reopen fstat failure")
        return real_fstat(fd)

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "fstat", flaky_fstat)
    monkeypatch.setattr(os, "close", recording_close)
    descriptor = _open_dir(game)
    try:
        result = cimount._open_child(descriptor, "WWW")
    finally:
        os.close(descriptor)
    assert result.fd == -1
    assert result.metadata is None
    assert result.step == "reopen"
    assert result.errno_code == errno.EIO
    # Both the first and the reopened descriptor are closed exactly once.
    assert len(closed) == 3
    assert closed[0] != descriptor
    assert closed[1] != descriptor


def test_open_child_fifo_reports_special_step(tmp_path: Path) -> None:
    game = _game_tree(tmp_path / "game")
    os.mkfifo(game / "pipe.fifo")
    descriptor = _open_dir(game)
    try:
        result = cimount._open_child(descriptor, "pipe.fifo")
    finally:
        os.close(descriptor)
    assert result.fd == -1
    assert result.metadata is None
    assert result.step == "special"
    assert result.errno_code is None


def test_open_child_success_returns_descriptor_and_metadata(tmp_path: Path) -> None:
    game = _game_tree(tmp_path / "game")
    descriptor = _open_dir(game)
    try:
        result = cimount._open_child(descriptor, "Readme.md")
        try:
            assert result.step == ""
            assert result.errno_code is None
            assert result.fd >= 0
            assert result.metadata is not None
            assert stat.S_ISREG(result.metadata.st_mode)
        finally:
            if result.fd >= 0:
                os.close(result.fd)
        subdir = cimount._open_child(descriptor, "WWW")
        try:
            assert subdir.step == ""
            assert subdir.fd >= 0
            assert subdir.metadata is not None
            assert stat.S_ISDIR(subdir.metadata.st_mode)
        finally:
            if subdir.fd >= 0:
                os.close(subdir.fd)
    finally:
        os.close(descriptor)


def test_require_libfuse3_missing_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cimount, "find_library", _no_library)
    assert is_available() is False
    with pytest.raises(LaunchError) as excinfo:
        require_libfuse3()
    message = str(excinfo.value)
    assert "libfuse3" in message
    assert "--ci-mount" in message


def test_require_libfuse3_present_returns_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cimount, "find_library", _fake_library)
    assert is_available() is True
    assert require_libfuse3() == "libfuse3.so.3"


def test_is_mountpoint_active_matches_proc_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "my dir" / CI_MOUNT_DIRNAME
    target.mkdir(parents=True)
    fake_mounts = tmp_path / "mounts"
    fake_mounts.write_text(
        "box-cimount /other/fuse fuse ro 0 0\n"
        f"box-cimount {target.as_posix().replace(' ', chr(92) + '040')} fuse ro 0 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cimount, "PROC_MOUNTS", fake_mounts)
    assert is_mountpoint_active(target) is True
    assert is_mountpoint_active(tmp_path / "elsewhere") is False


def test_is_mountpoint_active_without_mount_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(cimount, "PROC_MOUNTS", tmp_path / "no-such-mounts")
    assert is_mountpoint_active(plain) is False
    assert is_mountpoint_active(tmp_path / "missing") is False


def test_force_unmount_prefers_fusermount3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: object, **kwargs: object) -> object:
        assert isinstance(argv, list)
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    def fail_umount2(target: str) -> None:
        raise AssertionError("umount2 fallback must not run on success")

    monkeypatch.setattr(cimount, "_fusermount3_path", _fake_helper)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(cimount, "_libc_umount2", fail_umount2)
    target = tmp_path / CI_MOUNT_DIRNAME
    force_unmount(target)
    assert seen == [["/usr/bin/fusermount3", "-u", str(target)]]


def test_force_unmount_falls_back_to_umount2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: object, **kwargs: object) -> object:
        assert isinstance(argv, list)
        return subprocess.CompletedProcess(argv, 1)

    detached: list[str] = []

    def record_umount2(target: str) -> None:
        detached.append(target)

    monkeypatch.setattr(cimount, "_fusermount3_path", _fake_helper)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(cimount, "_libc_umount2", record_umount2)
    target = tmp_path / CI_MOUNT_DIRNAME
    force_unmount(target)
    assert detached == [str(target)]


def test_force_unmount_without_helper_uses_umount2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detached: list[str] = []

    def record_umount2(target: str) -> None:
        detached.append(target)

    monkeypatch.setattr(cimount, "_fusermount3_path", _no_helper)
    monkeypatch.setattr(cimount, "_libc_umount2", record_umount2)
    target = tmp_path / CI_MOUNT_DIRNAME
    force_unmount(target)
    assert detached == [str(target)]


def test_force_unmount_failure_names_the_mountpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: object, **kwargs: object) -> object:
        assert isinstance(argv, list)
        return subprocess.CompletedProcess(argv, 1)

    def fail_umount2(target: str) -> None:
        raise LaunchError(f"cannot unmount the case-insensitive mount at {target}: busy")

    monkeypatch.setattr(cimount, "_fusermount3_path", _fake_helper)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(cimount, "_libc_umount2", fail_umount2)
    target = tmp_path / CI_MOUNT_DIRNAME
    with pytest.raises(LaunchError, match="ci-mount"):
        force_unmount(target)


def _recording_session(mountpoint: Path) -> tuple[CiMountSession, list[str]]:
    calls: list[str] = []
    session = CiMountSession(
        mountpoint,
        backing_fd=-1,
        fuse_fd=-1,
        on_unmount=lambda: calls.append("unmount"),
        on_destroy=lambda: calls.append("destroy"),
    )
    return session, calls


def test_disown_never_unmounts_while_supervisor_teardown_does(tmp_path: Path) -> None:
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir()
    launcher, launcher_calls = _recording_session(mountpoint)
    assert launcher.inherit_fds == ()
    launcher.disown()
    assert launcher.disowned is True
    launcher.close()
    with pytest.raises(LaunchError):
        launcher.unmount()
    assert launcher_calls == []

    worker, worker_calls = _recording_session(mountpoint)
    worker.unmount()
    assert worker.unmounted is True
    assert worker_calls == ["unmount", "destroy"]
    # Teardown is exactly once.
    worker.unmount()
    assert worker_calls == ["unmount", "destroy"]


def test_session_context_exit_releases_without_unmounting(tmp_path: Path) -> None:
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir()
    calls: list[str] = []
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        session = CiMountSession(
            mountpoint,
            backing_fd=os.dup(descriptor),
            fuse_fd=-1,
            on_unmount=lambda: calls.append("unmount"),
            on_destroy=lambda: calls.append("destroy"),
        )
    finally:
        os.close(descriptor)
    with session:
        assert session.backing_fd >= 0
        assert session.inherit_fds == (session.backing_fd,)
    assert session.backing_fd == -1
    assert calls == []
    assert session.inherit_fds == ()


def test_ensure_profile_ci_mount_path(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    mountpoint = ensure_profile_ci_mount_path(paths, "abc123")
    assert mountpoint == paths.profiles_root / "abc123" / CI_MOUNT_DIRNAME
    assert mountpoint.is_dir() and not mountpoint.is_symlink()
    assert stat.S_IMODE(mountpoint.stat().st_mode) == 0o700
    # Idempotent under the same identifier.
    assert ensure_profile_ci_mount_path(paths, "abc123") == mountpoint


@pytest.mark.parametrize("identifier", ["", ".", "..", "a/b", "/abs", "UPPER", "MixedCase"])
def test_ensure_profile_ci_mount_path_rejects_identifiers(tmp_path: Path, identifier: str) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    with pytest.raises(ConfigurationError):
        ensure_profile_ci_mount_path(paths, identifier)


def test_drop_stale_ci_mount_clears_launcher_owned_junk(tmp_path: Path) -> None:
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir(parents=True)
    (mountpoint / "leftover.txt").write_text("junk", encoding="utf-8")
    drop_stale_ci_mount(mountpoint)
    assert mountpoint.is_dir()
    assert list(mountpoint.iterdir()) == []


def test_drop_stale_ci_mount_creates_missing_directory(tmp_path: Path) -> None:
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    drop_stale_ci_mount(mountpoint)
    assert mountpoint.is_dir()


def test_drop_stale_ci_mount_detaches_active_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir(parents=True)
    detached: list[Path] = []

    def record_unmount(path: Path) -> None:
        detached.append(path)

    monkeypatch.setattr(cimount, "is_mountpoint_active", _always_active)
    monkeypatch.setattr(cimount, "force_unmount", record_unmount)
    drop_stale_ci_mount(mountpoint)
    assert detached == [mountpoint]


def test_mount_refuses_active_stale_mountpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _game_tree(tmp_path / "game")
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir()
    # Isolate the active-mount guard from host prerequisites (libfuse3,
    # /dev/fuse) so this test pins the guard itself on any machine.
    monkeypatch.setattr(cimount, "require_libfuse3", lambda: "libfuse3.so.3")
    fake_fuse = tmp_path / "fuse"
    fake_fuse.touch()
    monkeypatch.setattr(cimount, "_FUSE_DEVICE_NODE", fake_fuse)
    monkeypatch.setattr(cimount, "is_mountpoint_active", _always_active)
    game_fd = _open_dir(game)
    try:
        with pytest.raises(LaunchError, match="already active"):
            mount_ci_mount(game_fd, mountpoint)
    finally:
        os.close(game_fd)


def test_mount_timeout_tears_down_unready_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path("/dev/fuse").exists():
        pytest.skip("no /dev/fuse device node")
    if not is_available():
        pytest.skip("no libfuse3 backend")
    game = _game_tree(tmp_path / "game")
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir()
    drop_stale_ci_mount(mountpoint)
    real_active = cimount.is_mountpoint_active
    # The loop daemon forked but readiness never arrives: no supervisor will
    # ever own this mount, so the timeout path must tear the session down.
    monkeypatch.setattr(cimount, "is_mountpoint_active", lambda path: False)
    game_fd = _open_dir(game)
    try:
        with pytest.raises(LaunchError, match="did not come up"):
            mount_ci_mount(game_fd, mountpoint, ready_timeout=0.2)
    finally:
        os.close(game_fd)
    assert not real_active(mountpoint)


def test_real_mount_mixed_case_siblings_resolve_both_ways(tmp_path: Path) -> None:
    """Case-variant sibling directories each resolve exactly through a real mount.

    Mirrors a shipped tree the game references under both spellings (Voice
    and voice): exact lookups hit their own backing directory with
    byte-identical reads, and a third spelling falls back to whichever
    sibling the directory scan finds first.
    """
    if not Path("/dev/fuse").exists():
        pytest.skip("no /dev/fuse device node")
    if not is_available():
        pytest.skip("no libfuse3 backend")
    root = tmp_path / "game"
    lower = root / "www" / "audio" / "voice" / "Chara"
    upper = root / "www" / "audio" / "Voice" / "Chara"
    lower.mkdir(parents=True)
    upper.mkdir(parents=True)
    (lower / "Hondatunagi2_01.rpgmvo").write_bytes(b"lower-bytes")
    (upper / "Hondatunagi2_01.rpgmvo").write_bytes(b"upper-bytes")
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir()
    drop_stale_ci_mount(mountpoint)
    game_fd = _open_dir(root)
    try:
        session = mount_ci_mount(game_fd, mountpoint)
        try:
            siblings = sorted(entry.name for entry in (mountpoint / "www" / "audio").iterdir())
            assert siblings == ["Voice", "voice"]
            assert (
                mountpoint / "www" / "audio" / "voice" / "Chara" / "Hondatunagi2_01.rpgmvo"
            ).read_bytes() == b"lower-bytes"
            assert (
                mountpoint / "www" / "audio" / "Voice" / "Chara" / "Hondatunagi2_01.rpgmvo"
            ).read_bytes() == b"upper-bytes"
            fallback = (
                mountpoint / "www" / "audio" / "VOICE" / "Chara" / "Hondatunagi2_01.rpgmvo"
            ).read_bytes()
            assert fallback in (b"lower-bytes", b"upper-bytes")
            descriptor = _open_dir(mountpoint)
            try:
                validate_tree(descriptor)
            finally:
                os.close(descriptor)
        finally:
            session.disown()
            session.close()
            force_unmount(mountpoint)
    finally:
        os.close(game_fd)


def test_real_mount_handover_survives_launcher_exit(tmp_path: Path) -> None:
    if not Path("/dev/fuse").exists():
        pytest.skip("no /dev/fuse device node")
    if not is_available():
        pytest.skip("no libfuse3 backend")
    game = _game_tree(tmp_path / "game")
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir()
    drop_stale_ci_mount(mountpoint)
    game_fd = _open_dir(game)
    try:
        session = mount_ci_mount(game_fd, mountpoint)
        try:
            assert session.backing_fd >= 0
            assert session.inherit_fds
            # Simulate launcher exit: hand over, then release our references.
            session.disown()
            session.close()
            assert session.backing_fd == -1
            # The handed-over loop serves exact reads ...
            assert (mountpoint / "ASSET.dat").read_bytes() == b"asset-bytes"
            # ... case-insensitive lookups ...
            assert (mountpoint / "asset.dat").read_bytes() == b"asset-bytes"
            assert (mountpoint / "www" / "main.js").read_bytes() == b"console.log(1);"
            # ... and directory listings.
            assert sorted(p.name for p in mountpoint.iterdir()) == ["ASSET.dat", "Readme.md", "WWW"]
        finally:
            # Supervisor teardown path owns the unmount.
            force_unmount(mountpoint)
    finally:
        os.close(game_fd)


def test_real_mount_large_dir_lists_identically_across_repeated_listdir(tmp_path: Path) -> None:
    """A large directory paginates over the cached listing with stable results."""
    if not Path("/dev/fuse").exists():
        pytest.skip("no /dev/fuse device node")
    if not is_available():
        pytest.skip("no libfuse3 backend")
    root = tmp_path / "game"
    big = root / "bigdir"
    big.mkdir(parents=True)
    count = 300
    for index in range(count):
        (big / f"file-{index:04d}.dat").write_bytes(b"payload")
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir()
    drop_stale_ci_mount(mountpoint)
    game_fd = _open_dir(root)
    try:
        session = mount_ci_mount(game_fd, mountpoint)
        try:
            expected = [f"file-{index:04d}.dat" for index in range(count)]
            first = sorted(entry.name for entry in (mountpoint / "bigdir").iterdir())
            second = sorted(entry.name for entry in (mountpoint / "bigdir").iterdir())
            third = sorted(os.listdir(mountpoint / "bigdir"))
            assert first == expected
            assert second == expected
            assert third == expected
        finally:
            session.disown()
            session.close()
            force_unmount(mountpoint)
    finally:
        os.close(game_fd)


def test_real_mount_readdir_reflects_mutation_across_opens(tmp_path: Path) -> None:
    """A backing mutation between opendir sessions is visible after re-opendir."""
    if not Path("/dev/fuse").exists():
        pytest.skip("no /dev/fuse device node")
    if not is_available():
        pytest.skip("no libfuse3 backend")
    root = tmp_path / "game"
    mutable = root / "mutable"
    mutable.mkdir(parents=True)
    (mutable / "a.txt").write_text("a", encoding="utf-8")
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir()
    drop_stale_ci_mount(mountpoint)
    game_fd = _open_dir(root)
    try:
        session = mount_ci_mount(game_fd, mountpoint)
        try:
            before = sorted(entry.name for entry in (mountpoint / "mutable").iterdir())
            assert before == ["a.txt"]
            (mutable / "b.txt").write_text("b", encoding="utf-8")
            after = sorted(entry.name for entry in (mountpoint / "mutable").iterdir())
            assert after == ["a.txt", "b.txt"]
        finally:
            session.disown()
            session.close()
            force_unmount(mountpoint)
    finally:
        os.close(game_fd)


class _RawLog:
    """Record low-level replies so callback units run without libfuse3 mounted."""

    def __init__(self) -> None:
        self.errors: list[tuple[int, int]] = []
        self.added: list[tuple[bytes, int]] = []
        self.bufs: list[tuple[int, bytes, int]] = []
        self.attrs: list[tuple[int, int, int, float]] = []

    def fuse_reply_err(self, req: int, code: int) -> int:
        self.errors.append((req, code))
        return 0

    def fuse_reply_none(self, req: int) -> None:
        return None

    def fuse_reply_entry(self, req: int, param: object) -> int:
        return 0

    def fuse_reply_attr(self, req: int, attr: object, timeout: object) -> int:
        with suppress(Exception):
            recovered = ctypes.cast(cast("Any", attr), ctypes.POINTER(cimount._Stat)).contents
            self.attrs.append(
                (req, int(recovered.st_size), int(recovered.st_mode), float(cast("Any", timeout)))
            )
        return 0

    def fuse_reply_open(self, req: int, buf: object) -> int:
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


def _callback_harness(
    dir_fd: int,
) -> tuple[cimount._CallbackSet, cimount._FuseState, _RawLog]:
    """Build real callbacks over a stub library; needs no libfuse3 or mount."""
    raw = _RawLog()
    library = cast("cimount._LibFuse", SimpleNamespace(raw=raw))
    state = cimount._FuseState(dir_fd)
    return cimount._CallbackSet(library, state), state, raw


def _close_state_fds(state: cimount._FuseState) -> None:
    """Release every backing descriptor a callback unit test retained."""
    for record in list(state.inodes.values()):
        with suppress(OSError):
            os.close(record.fd)
    cimount._close_state_debug_log(state)


def _bump_dir_mtime(path: Path) -> None:
    """Force a deterministic directory-mtime change for revalidation tests."""
    metadata = os.stat(path)
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000))


def _listed_names(raw: _RawLog) -> list[str]:
    """Return the non-dot names recorded by the last readdir reply."""
    return sorted(name.decode("utf-8") for name, _ in raw.added if name not in (b".", b".."))


def test_readdir_pagination_covers_snapshot_without_duplicates(tmp_path: Path) -> None:
    """Chained small-buffer readdirs cover every entry exactly once, in order."""
    root = tmp_path / "tree"
    root.mkdir()
    expected = sorted(f"file-{index:03d}.dat" for index in range(40))
    for name in expected:
        (root / name).write_text("x", encoding="utf-8")
    descriptor = _open_dir(root)
    try:
        callbacks, state, raw = _callback_harness(descriptor)
        try:
            ino = cimount._FUSE_ROOT_ID
            callbacks._on_opendir(1, ino, 0)
            assert not raw.errors
            seen: list[str] = []
            offset = 0
            rounds = 0
            while True:
                raw.added.clear()
                callbacks._on_readdir(2, ino, 128, offset, 0)
                assert not raw.errors
                if not raw.added:
                    break
                for entry_name, _entry_off in raw.added:
                    if entry_name not in (b".", b".."):
                        seen.append(entry_name.decode("utf-8"))
                offset = raw.added[-1][1]
                rounds += 1
                assert rounds < 100
            assert rounds >= 3
            assert seen == expected
        finally:
            _close_state_fds(state)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def test_readdir_revalidates_by_mtime_mid_open_and_after_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listings rescan on directory-mtime change, even mid-open; release still drops."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    scans = [0]
    real_scan = cimount._scan_readdir_listing

    def counting_scan(dir_fd: int) -> list[tuple[str, os.stat_result]]:
        scans[0] += 1
        return real_scan(dir_fd)

    monkeypatch.setattr(cimount, "_scan_readdir_listing", counting_scan)
    descriptor = _open_dir(root)
    try:
        callbacks, state, raw = _callback_harness(descriptor)
        try:
            ino = cimount._FUSE_ROOT_ID
            callbacks._on_opendir(1, ino, 0)
            callbacks._on_readdir(2, ino, 4096, 0, 0)
            assert scans == [1]
            # No mutation → repeated readdirs never rescan.
            for req in (3, 4):
                raw.added.clear()
                callbacks._on_readdir(req, ino, 4096, 0, 0)
                assert scans == [1]
                assert _listed_names(raw) == ["a.txt"]
            # Backing mutation plus an explicit mtime bump rescans mid-open.
            (root / "b.txt").write_text("b", encoding="utf-8")
            _bump_dir_mtime(root)
            raw.added.clear()
            callbacks._on_readdir(5, ino, 4096, 0, 0)
            assert scans == [2]
            assert _listed_names(raw) == ["a.txt", "b.txt"]
            # Release still drops the snapshot: reopen rescans exactly once.
            callbacks._on_release(6, ino, 0)
            assert ino not in state.listings
            callbacks._on_opendir(7, ino, 0)
            raw.added.clear()
            callbacks._on_readdir(8, ino, 4096, 0, 0)
            assert scans == [3]
            assert _listed_names(raw) == ["a.txt", "b.txt"]
            raw.added.clear()
            callbacks._on_readdir(9, ino, 4096, 0, 0)
            assert scans == [3]
        finally:
            _close_state_fds(state)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def test_readdir_mtime_bump_without_content_change_rescans_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit mtime bump alone forces exactly one rescan, deterministically."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    scans = [0]
    real_scan = cimount._scan_readdir_listing

    def counting_scan(dir_fd: int) -> list[tuple[str, os.stat_result]]:
        scans[0] += 1
        return real_scan(dir_fd)

    monkeypatch.setattr(cimount, "_scan_readdir_listing", counting_scan)
    descriptor = _open_dir(root)
    try:
        callbacks, state, raw = _callback_harness(descriptor)
        try:
            ino = cimount._FUSE_ROOT_ID
            callbacks._on_opendir(1, ino, 0)
            callbacks._on_readdir(2, ino, 4096, 0, 0)
            assert scans == [1]
            _bump_dir_mtime(root)
            raw.added.clear()
            callbacks._on_readdir(3, ino, 4096, 0, 0)
            assert not raw.errors
            assert scans == [2]
            assert _listed_names(raw) == ["a.txt"]
            raw.added.clear()
            callbacks._on_readdir(4, ino, 4096, 0, 0)
            assert scans == [2]
        finally:
            _close_state_fds(state)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def test_forget_while_alive_keeps_inode_but_mtime_revalidates(tmp_path: Path) -> None:
    """A forget that leaves refs keeps the inode; mtime changes still rescan."""
    root = tmp_path / "tree"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (sub / "a.txt").write_text("a", encoding="utf-8")
    descriptor = _open_dir(root)
    try:
        callbacks, state, raw = _callback_harness(descriptor)
        try:
            before = set(state.inodes)
            callbacks._on_lookup(10, cimount._FUSE_ROOT_ID, b"sub")
            assert not raw.errors
            (child,) = set(state.inodes) - before
            callbacks._on_opendir(11, child, 0)
            assert not raw.errors
            callbacks._on_readdir(12, child, 4096, 0, 0)
            assert not raw.errors
            (sub / "b.txt").write_text("b", encoding="utf-8")
            _bump_dir_mtime(sub)
            # A forget that leaves refs keeps both the inode and its entry ...
            callbacks._on_forget(13, child, 0)
            assert child in state.inodes
            assert child in state.listings
            # ... but the next readdir revalidates by mtime and shows the mutation.
            raw.added.clear()
            callbacks._on_readdir(14, child, 4096, 0, 0)
            assert not raw.errors
            assert _listed_names(raw) == ["a.txt", "b.txt"]
            callbacks._on_forget(15, child, 1)
            assert child not in state.inodes
            assert child not in state.listings
            raw.errors.clear()
            callbacks._on_readdir(16, child, 4096, 0, 0)
            assert raw.errors == [(16, errno.ENOENT)]
        finally:
            _close_state_fds(state)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def test_debug_log_noop_without_env_and_logs_decisions_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The debug log stays disabled without the env var and records metadata with it."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("marker-asset-bytes-XYZ", encoding="utf-8")
    descriptor = _open_dir(root)
    try:
        callbacks, state, raw = _callback_harness(descriptor)
        try:
            monkeypatch.delenv("BOX_CIMOUNT_DEBUG_LOG", raising=False)
            cimount._debug_log(state, "silent")
            assert state.debug_fd == -1
            log = tmp_path / "cimount-debug.log"
            monkeypatch.setenv("BOX_CIMOUNT_DEBUG_LOG", str(log))
            cimount._debug_log(state, "hello")
            assert state.debug_fd >= 0
            opened = state.debug_fd
            cimount._debug_log(state, "again")
            assert state.debug_fd == opened
            ino = cimount._FUSE_ROOT_ID
            callbacks._on_opendir(21, ino, 0)
            callbacks._on_readdir(22, ino, 4096, 0, 0)
            callbacks._on_lookup(23, ino, b"a.txt")
            assert not raw.errors
            text = log.read_text(encoding="utf-8")
            assert "hello" in text
            assert "again" in text
            assert f"readdir ino={ino} miss" in text
            assert f"lookup parent={ino}" in text and "name_resolved='a.txt'" in text
            assert f"lookup parent={ino} name='a.txt' opened ino=" in text

            def failing_open(parent_fd: int, name: str) -> cimount._OpenChildResult:
                return cimount._OpenChildResult(step="open", errno_code=errno.ENOENT)

            real_open_child = cimount._open_child
            monkeypatch.setattr(cimount, "_open_child", failing_open)
            callbacks._on_lookup(24, ino, b"a.txt")
            assert raw.errors[-1] == (24, errno.ENOENT)
            failure_text = log.read_text(encoding="utf-8")
            assert (
                f"lookup parent={ino} name='a.txt' open_failed "
                f"step=open errno={errno.ENOENT}" in failure_text
            )
            monkeypatch.setattr(cimount, "_open_child", real_open_child)

            def overflowing_register(
                child_fd: int, metadata: os.stat_result, parent: int, name: str | None
            ) -> int:
                # File lookups arrive with fd == -1 (transient open already
                # closed by the caller); only close a retained directory fd.
                if child_fd >= 0:
                    os.close(child_fd)
                raise OverflowError("forced inode exhaustion")

            monkeypatch.setattr(callbacks, "_register", overflowing_register)
            callbacks._on_lookup(25, ino, b"a.txt")
            assert raw.errors[-1] == (25, errno.ENOSPC)
            overflow_text = log.read_text(encoding="utf-8")
            assert (
                f"lookup parent={ino} name='a.txt' open_failed "
                f"step=register errno={errno.ENOSPC}" in overflow_text
            )
            # Private log file, metadata only: file contents never logged, even
            # though the tree holds a distinctive token.
            assert stat.S_IMODE(os.stat(log).st_mode) & 0o077 == 0
            assert "marker-asset-bytes-XYZ" not in text
        finally:
            _close_state_fds(state)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def test_real_mount_many_files_survive_low_fd_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full walk of ~3000 files completes with zero ENOENT under soft RLIMIT 128.

    Incident regression: the daemon used to retain one backing descriptor
    per looked-up file, so a full-tree walk exhausted the standard 1024
    soft RLIMIT_NOFILE (~1021 usable fds) and every later lookup
    collapsed to ENOENT.  File inodes now retain no descriptor (only
    directories do), so the walk below holds a handful of descriptors.
    The low soft limit applies to the test process (restored in ``finally``,
    hard limit untouched so restoring needs no privilege); the daemon's own
    ceiling raise is neutralized for this test so it stays at 128 too —
    otherwise the raise alone would mask the old bug.
    """
    if not Path("/dev/fuse").exists():
        pytest.skip("no /dev/fuse device node")
    if not is_available():
        pytest.skip("no libfuse3 backend")
    monkeypatch.setattr(cimount, "_raise_daemon_nofile_limit", lambda: None)
    if not Path("/dev/fuse").exists():
        pytest.skip("no /dev/fuse device node")
    if not is_available():
        pytest.skip("no libfuse3 backend")
    root = tmp_path / "game"
    groups = 30
    per_group = 100
    for group in range(groups):
        pack = root / f"pack-{group:02d}"
        pack.mkdir(parents=True)
        for index in range(per_group):
            (pack / f"f-{group:02d}-{index:03d}.dat").write_bytes(
                f"bytes-{group:02d}-{index:03d}".encode()
            )
    mountpoint = tmp_path / CI_MOUNT_DIRNAME
    mountpoint.mkdir()
    drop_stale_ci_mount(mountpoint)
    game_fd = _open_dir(root)
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, hard))
        try:
            session = mount_ci_mount(game_fd, mountpoint)
            try:
                seen = 0
                for dirpath, _dirnames, filenames in os.walk(mountpoint):
                    for filename in filenames:
                        payload = (Path(dirpath) / filename).read_bytes()
                        stem = Path(filename).stem
                        group_text, index_text = stem.split("-")[1:3]
                        assert payload == f"bytes-{group_text}-{index_text}".encode()
                        seen += 1
                assert seen == groups * per_group
                descriptor = _open_dir(mountpoint)
                try:
                    validate_tree(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                session.disown()
                session.close()
                force_unmount(mountpoint)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    finally:
        os.close(game_fd)


def test_lookup_releases_file_descriptors_keeping_only_directories(tmp_path: Path) -> None:
    """File lookups retain no descriptor; only directories (and root) keep one."""
    root = tmp_path / "tree"
    sub = root / "sub"
    sub.mkdir(parents=True)
    names = [f"file-{index:03d}.dat" for index in range(50)]
    for name in names:
        (root / name).write_text("x", encoding="utf-8")
    (sub / "inner.txt").write_text("y", encoding="utf-8")
    descriptor = _open_dir(root)
    try:
        callbacks, state, raw = _callback_harness(descriptor)
        try:
            ino = cimount._FUSE_ROOT_ID
            callbacks._on_lookup(1, ino, b"sub")
            assert not raw.errors
            start_fds = len(os.listdir("/proc/self/fd"))
            for offset, name in enumerate(names):
                callbacks._on_lookup(10 + offset, ino, name.encode("utf-8"))
            assert not raw.errors
            # A repeat lookup dedups onto the same inode without retaining more.
            first = next(key for key, record in state.inodes.items() if record.name == names[0])
            callbacks._on_lookup(1000, ino, names[0].encode("utf-8"))
            assert not raw.errors
            assert next(key for key, r in state.inodes.items() if r.name == names[0]) == first
            retained = [record for record in state.inodes.values() if record.fd >= 0]
            assert len(retained) == 2
            files = [record for record in state.inodes.values() if record.fd == -1]
            assert len(files) == len(names)
            assert {record.name for record in files} == set(names)
            # OS-level proof, not just records: 51 file lookups grow the
            # descriptor table by zero (the subdirectory was retained
            # before the baseline snapshot).
            assert len(os.listdir("/proc/self/fd")) - start_fds == 0
        finally:
            _close_state_fds(state)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def test_file_getattr_reports_live_parent_relative_metadata(tmp_path: Path) -> None:
    """File getattr resolves per request off the parent and fails closed without it."""
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "live.dat"
    target.write_bytes(b"0123456789")
    descriptor = _open_dir(root)
    try:
        callbacks, state, raw = _callback_harness(descriptor)
        try:
            ino = cimount._FUSE_ROOT_ID
            callbacks._on_lookup(1, ino, b"live.dat")
            assert not raw.errors
            (child,) = set(state.inodes) - {ino}
            assert state.inodes[child].fd == -1
            callbacks._on_getattr(2, child, 0)
            assert not raw.errors
            live = os.stat(target)
            assert raw.attrs[-1] == (2, live.st_size, live.st_mode, 0.0)
            # A backing mutation is visible immediately: nothing is cached.
            target.write_bytes(b"01234567890123456789")
            grown = os.stat(target)
            callbacks._on_getattr(3, child, 0)
            assert not raw.errors
            assert raw.attrs[-1] == (3, grown.st_size, grown.st_mode, 0.0)
            assert grown.st_size == 20
            # A missing parent fails closed instead of raising.
            state.inodes.pop(ino)
            callbacks._on_getattr(4, child, 0)
            assert raw.errors[-1] == (4, errno.EIO)
        finally:
            _close_state_fds(state)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def test_file_open_and_read_serve_bytes_without_retained_descriptor(tmp_path: Path) -> None:
    """File open/read work off the parent descriptor and fail closed without it."""
    root = tmp_path / "tree"
    root.mkdir()
    payload = b"hello-ci-mount-bytes"
    (root / "hello.txt").write_bytes(payload)
    descriptor = _open_dir(root)
    try:
        callbacks, state, raw = _callback_harness(descriptor)
        try:
            ino = cimount._FUSE_ROOT_ID
            callbacks._on_lookup(1, ino, b"hello.txt")
            assert not raw.errors
            (child,) = set(state.inodes) - {ino}
            assert state.inodes[child].fd == -1
            callbacks._on_open(2, child, 0)
            assert not raw.errors
            write_flags = ctypes.c_int(os.O_WRONLY)
            callbacks._on_open(3, child, ctypes.addressof(write_flags))
            assert raw.errors[-1] == (3, errno.EROFS)
            raw.errors.clear()
            callbacks._on_read(4, child, 4096, 0, 0)
            assert not raw.errors
            assert raw.bufs[-1] == (4, payload, len(payload))
            callbacks._on_read(5, child, 4096, 6, 0)
            assert raw.bufs[-1] == (5, payload[6:], len(payload) - 6)
            callbacks._on_read(6, child, 4096, len(payload), 0)
            assert raw.bufs[-1] == (6, b"", 0)
            # A missing parent fails closed instead of raising.
            state.inodes.pop(ino)
            callbacks._on_open(7, child, 0)
            assert raw.errors[-1] == (7, errno.EIO)
            callbacks._on_read(8, child, 4096, 0, 0)
            assert raw.errors[-1] == (8, errno.EIO)
        finally:
            _close_state_fds(state)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def test_lookup_inside_file_parent_replies_enotdir(tmp_path: Path) -> None:
    """Lookups under a file inode, and dir opens of one, answer ENOTDIR."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "note.txt").write_text("x", encoding="utf-8")
    descriptor = _open_dir(root)
    try:
        callbacks, state, raw = _callback_harness(descriptor)
        try:
            ino = cimount._FUSE_ROOT_ID
            callbacks._on_lookup(1, ino, b"note.txt")
            assert not raw.errors
            (child,) = set(state.inodes) - {ino}
            callbacks._on_lookup(2, child, b"anything")
            assert raw.errors[-1] == (2, errno.ENOTDIR)
            callbacks._on_opendir(3, child, 0)
            assert raw.errors[-1] == (3, errno.ENOTDIR)
            callbacks._on_readdir(4, child, 4096, 0, 0)
            assert raw.errors[-1] == (4, errno.ENOTDIR)
        finally:
            _close_state_fds(state)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def test_close_state_fds_tolerates_unretained_file_descriptors(tmp_path: Path) -> None:
    """The test helper closes file records with fd == -1 without raising (EBADF)."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    descriptor = _open_dir(root)
    try:
        callbacks, state, raw = _callback_harness(descriptor)
        try:
            callbacks._on_lookup(1, cimount._FUSE_ROOT_ID, b"a.txt")
            assert not raw.errors
            assert any(record.fd == -1 for record in state.inodes.values())
            _close_state_fds(state)
        finally:
            _close_state_fds(state)
    finally:
        with suppress(OSError):
            os.close(descriptor)
