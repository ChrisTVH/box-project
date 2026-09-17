"""Source-folder EasyRPG saves with first-run migration from the unpacked copy."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
import stat
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from box.api import launch as launch_module
from box.api.launch import launch
from box.config.repository import ConfigRepository
from box.errors import LaunchError
from box.games.identity import game_id
from box.launch.sandbox import Sandbox
from box.launch.supervisor import LaunchedSession
from box.models import EngineName, GameInfo
from box.paths import AppPaths, open_directory_without_symlinks
from box.runtime import evb as evb_module


class FakeInteraction:
    """Record GUI callbacks with scripted answers."""

    def __init__(self, *, confirm_add_root: bool = True) -> None:
        self._confirm_add_root = confirm_add_root
        self.add_root_calls: list[Path] = []

    def confirm_x11(self, display: str) -> bool:
        del display
        return True

    def confirm_add_root(self, path: Path) -> bool:
        self.add_root_calls.append(path)
        return self._confirm_add_root

    def choose_runtime(self, kind: str, candidates: tuple[str, ...], title: str) -> int | None:
        del kind, candidates, title
        return 0


class FakeSandbox:
    """Record sandbox policy calls without touching Bubblewrap."""

    def __init__(self, *, allow_network: bool = False, allow_game_writes: bool = False) -> None:
        self.allow_network = allow_network
        self.allow_game_writes = allow_game_writes
        self.calls: list[str] = []
        self.persistence_roots: list[Path | None] = []
        self.source_roots: list[Path] = []
        self.binds: list[tuple[str, bool]] = []
        self.probe = "wayland"
        self.last_cwd = "/"
        self.last_arguments: list[str] = []
        self._kept: list[int] = []

    def __enter__(self) -> FakeSandbox:
        self.calls.append("enter")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.calls.append("exit")
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
        self.calls.append("runtime")
        return "/runtime/fake"

    def display_probe(self) -> str:
        return self.probe

    def desktop(self) -> None:
        self.calls.append("desktop")

    def x11(self) -> None:
        self.calls.append("x11")

    def devices(self) -> None:
        self.calls.append("devices")

    def audio(self) -> None:
        self.calls.append("audio")

    def gamemode(self, socket_path: Path) -> None:
        del socket_path
        self.calls.append("gamemode")

    def persistence(self, paths: AppPaths, game: GameInfo, game_root: Path | None = None) -> None:
        del paths, game
        self.calls.append("persistence")
        self.persistence_roots.append(game_root)

    def game_saves(self, game: GameInfo, descriptor: int) -> int:
        del game
        self.calls.append("game_saves")
        duplicated = os.dup(descriptor)
        self._kept.append(duplicated)
        return duplicated

    def game_source_saves(self, source: GameInfo, descriptor: int) -> int:
        del source
        self.calls.append("game_source_saves")
        self.source_roots.append(Path(os.readlink(f"/proc/self/fd/{descriptor}")))
        duplicated = os.dup(descriptor)
        self._kept.append(duplicated)
        return duplicated

    def game_writable(self, descriptor: int) -> None:
        self.calls.append("game_writable")
        self._kept.append(descriptor)

    def nw_game(self, game: GameInfo, descriptor: int, saves: int) -> None:
        del game, descriptor, saves
        self.calls.append("nw_game")

    def bind(self, descriptor: int, destination: str, *, writable: bool = False) -> None:
        self.calls.append(f"bind:{destination}")
        self.binds.append((destination, writable))
        self._kept.append(descriptor)

    def command(self, arguments: list[str], *, cwd: str = "/") -> list[str]:
        self.calls.append(f"command:{cwd}")
        self.last_cwd = cwd
        self.last_arguments = list(arguments)
        return ["fake-bwrap", *arguments]


def _paths(tmp_path: Path) -> AppPaths:
    """Return isolated launcher paths below the test directory."""
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


def _packed_source(tmp_path: Path) -> tuple[Path, Path]:
    """Create a packed candidate directory with one custom-named executable."""
    source = tmp_path / "source-game"
    source.mkdir()
    executable = source / "My Custom Game.exe"
    executable.write_bytes(b"fake packed executable")
    return (source, executable)


def _rpg_rt_tree(root: Path) -> Path:
    """Create a detectable 2000/2003 tree at the given root."""
    root.mkdir(parents=True, exist_ok=True)
    for filename in ("RPG_RT.ini", "RPG_RT.ldb", "RPG_RT.lmt"):
        (root / filename).write_text("fixture", encoding="utf-8")
    return root


def _install_easyrpg(tmp_path: Path, version: str) -> None:
    """Install one fake EasyRPG runtime into the isolated cache."""
    binary = tmp_path / "cache" / "runtimes" / "easyrpg" / version / "easyrpg-player"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)


def _patch_sandbox(monkeypatch: pytest.MonkeyPatch) -> list[FakeSandbox]:
    """Replace Bubblewrap sandboxing with recording doubles."""
    sandboxes: list[FakeSandbox] = []

    def factory(*, allow_network: bool = False, allow_game_writes: bool = False) -> FakeSandbox:
        sandbox = FakeSandbox(allow_network=allow_network, allow_game_writes=allow_game_writes)
        sandboxes.append(sandbox)
        return sandbox

    monkeypatch.setattr("box.api.launch.Sandbox", factory)
    return sandboxes


def _patch_spawn(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Mock detached spawn; capture commands and return a fake handle."""
    commands: list[list[str]] = []

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
    ) -> LaunchedSession:
        commands.append(list(command))
        return LaunchedSession(
            identifier=identifier, name=name, root=paths.sessions_root / identifier / name
        )

    monkeypatch.setattr("box.api.launch.spawn_detached", fake_spawn)
    return commands


def test_game_source_saves_uses_source_root(tmp_path: Path) -> None:
    """The saves descriptor points at the consented source save/ directory."""
    source = _rpg_rt_tree(tmp_path / "source")
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    source_game = GameInfo(EngineName.RPG_MAKER_2000_2003, source)
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(source))
        saves = sandbox.game_source_saves(source_game, descriptor)
        assert Path(os.readlink(f"/proc/self/fd/{saves}")) == source / "save"
        assert stat.S_IMODE(os.fstat(saves).st_mode) == 0o700
    assert (source / "save").is_dir()
    assert not (unpacked / "save").exists()


def test_game_source_saves_rejects_non_easyrpg(tmp_path: Path) -> None:
    """Only RPG Maker 2000/2003 games qualify for source-backed saves."""
    root = tmp_path / "game"
    root.mkdir()
    entry = root / "index.html"
    entry.write_text("fixture", encoding="utf-8")
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, entry)
    with pytest.raises(LaunchError), Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        sandbox.game_source_saves(game, descriptor)


def test_game_source_saves_rejects_symlinked_save(tmp_path: Path) -> None:
    """A symlinked source save/ fails closed instead of redirecting the bind."""
    source = _rpg_rt_tree(tmp_path / "source")
    outside = tmp_path / "outside"
    outside.mkdir()
    (source / "save").symlink_to(outside)
    source_game = GameInfo(EngineName.RPG_MAKER_2000_2003, source)
    with pytest.raises(LaunchError), Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(source))
        sandbox.game_source_saves(source_game, descriptor)
    assert list(outside.iterdir()) == []


def test_migrate_moves_unpacked_saves_to_missing_source(tmp_path: Path) -> None:
    """First-run migration carries regular files and dirs into the source."""
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    (unpacked / "save").mkdir()
    (unpacked / "save" / "Save01.lsd").write_bytes(b"player progress")
    nested = unpacked / "save" / "nested"
    nested.mkdir()
    (nested / "Save02.lsd").write_bytes(b"nested progress")
    source = _rpg_rt_tree(tmp_path / "source")

    launch_module._migrate_easyrpg_saves_to_source(unpacked, source)

    assert (source / "save" / "Save01.lsd").read_bytes() == b"player progress"
    assert (source / "save" / "nested" / "Save02.lsd").read_bytes() == b"nested progress"
    assert stat.S_IMODE((source / "save").stat().st_mode) == 0o700
    assert not (unpacked / "save" / "Save01.lsd").exists()
    assert not (unpacked / "save" / "nested" / "Save02.lsd").exists()
    assert (unpacked / "save").is_dir()


def test_migrate_skips_when_source_already_has_saves(tmp_path: Path) -> None:
    """A non-empty source save/ is never overwritten by stale unpacked saves."""
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    (unpacked / "save").mkdir()
    (unpacked / "save" / "Save01.lsd").write_bytes(b"stale progress")
    source = _rpg_rt_tree(tmp_path / "source")
    (source / "save").mkdir()
    (source / "save" / "Save01.lsd").write_bytes(b"current progress")

    launch_module._migrate_easyrpg_saves_to_source(unpacked, source)

    assert (source / "save" / "Save01.lsd").read_bytes() == b"current progress"
    assert (unpacked / "save" / "Save01.lsd").read_bytes() == b"stale progress"


def test_migrate_skips_symlinks_and_specials(tmp_path: Path) -> None:
    """Only regular files and directories travel; links and FIFOs stay behind."""
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    (unpacked / "save").mkdir()
    (unpacked / "save" / "Save01.lsd").write_bytes(b"player progress")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("host secret", encoding="utf-8")
    (unpacked / "save" / "sneaky").symlink_to(secret)
    fifo = unpacked / "save" / "pipe"
    os.mkfifo(fifo)
    source = _rpg_rt_tree(tmp_path / "source")

    launch_module._migrate_easyrpg_saves_to_source(unpacked, source)

    assert (source / "save" / "Save01.lsd").read_bytes() == b"player progress"
    assert not os.path.lexists(source / "save" / "sneaky")
    assert not os.path.lexists(source / "save" / "pipe")
    assert os.path.islink(unpacked / "save" / "sneaky")
    assert secret.read_text(encoding="utf-8") == "host secret"


def test_easyrpg_packed_launch_uses_source_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Packed EasyRPG launches bind saves from the source, keeping /game read-only."""
    source, _executable = _packed_source(tmp_path)
    cache = _rpg_rt_tree(tmp_path / "cache-entry")
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_easyrpg(tmp_path, "0.8.1.1")
    interaction = FakeInteraction()
    sandboxes = _patch_sandbox(monkeypatch)
    _patch_spawn(monkeypatch)

    def fake_unpack(paths_arg: AppPaths, exe: Path) -> Path:
        del paths_arg, exe
        return cache

    monkeypatch.setattr(evb_module, "ensure_unpacked", fake_unpack)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(paths, repository, source, "0.8.1.1", False, interaction=interaction)

    assert interaction.add_root_calls == [source]
    assert handle.identifier == game_id(source)
    assert sandboxes[0].persistence_roots == [source]
    assert "game_source_saves" in sandboxes[0].calls
    assert "game_saves" not in sandboxes[0].calls
    assert sandboxes[0].source_roots == [source]
    assert sandboxes[0].last_cwd == "/game"
    assert sandboxes[0].last_arguments[-6:] == [
        "/runtime/fake",
        "--project-path",
        "/game",
        "--fullscreen",
        "--save-path",
        "/game/save",
    ]
    assert ("/game", False) in sandboxes[0].binds
    assert ("/game/save", True) in sandboxes[0].binds


def test_easyrpg_packed_launch_creates_unpacked_save_mountpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /game/save bind needs a mountpoint even without an unpacked save/.

    Regression: Bubblewrap cannot create /game/save when /game is mounted
    read-only, so launching a packed game whose unpacked tree lost its
    save/ (first-run migration moved it to the source) failed with
    'Can't create file /game/save'. The launch now ensures the mountpoint
    on the unpacked tree before sandboxing.
    """
    source, _executable = _packed_source(tmp_path)
    cache = _rpg_rt_tree(tmp_path / "cache-entry")
    assert not (cache / "save").exists()
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_easyrpg(tmp_path, "0.8.1.1")
    interaction = FakeInteraction()
    sandboxes = _patch_sandbox(monkeypatch)
    _patch_spawn(monkeypatch)

    def fake_unpack(paths_arg: AppPaths, exe: Path) -> Path:
        del paths_arg, exe
        return cache

    monkeypatch.setattr(evb_module, "ensure_unpacked", fake_unpack)
    monkeypatch.delenv("DISPLAY", raising=False)

    launch(paths, repository, source, "0.8.1.1", False, interaction=interaction)

    assert (cache / "save").is_dir()
    assert not (cache / "save").is_symlink()
    assert ("/game/save", True) in sandboxes[0].binds


def test_ensure_save_mountpoint_rejects_non_directory(
    tmp_path: Path,
) -> None:
    """A non-directory save/ under the game tree fails closed."""
    from box.api.launch import _ensure_save_mountpoint

    root = _rpg_rt_tree(tmp_path / "game")
    (root / "save").write_text("not a directory", encoding="utf-8")
    descriptor = os.open(root, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with pytest.raises(LaunchError):
            _ensure_save_mountpoint(descriptor)
    finally:
        os.close(descriptor)


def test_easyrpg_packed_launch_migrates_first_run_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old unpacked saves move into the source once, then the cache goes stale."""
    source, _executable = _packed_source(tmp_path)
    cache = _rpg_rt_tree(tmp_path / "cache-entry")
    (cache / "save").mkdir()
    (cache / "save" / "Save01.lsd").write_bytes(b"old progress")
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_easyrpg(tmp_path, "0.8.1.1")
    _patch_sandbox(monkeypatch)
    _patch_spawn(monkeypatch)

    def fake_unpack(paths_arg: AppPaths, exe: Path) -> Path:
        del paths_arg, exe
        return cache

    monkeypatch.setattr(evb_module, "ensure_unpacked", fake_unpack)
    monkeypatch.delenv("DISPLAY", raising=False)

    launch(paths, repository, source, "0.8.1.1", False, interaction=FakeInteraction())

    assert (source / "save" / "Save01.lsd").read_bytes() == b"old progress"
    assert not (cache / "save" / "Save01.lsd").exists()


def test_easyrpg_non_packed_launch_uses_game_root_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-packed EasyRPG consents at the game root with ro /game plus save overlay."""
    game_root = _rpg_rt_tree(tmp_path / "game")
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_easyrpg(tmp_path, "0.8.1.1")
    interaction = FakeInteraction()
    sandboxes = _patch_sandbox(monkeypatch)
    _patch_spawn(monkeypatch)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(paths, repository, game_root, "0.8.1.1", False, interaction=interaction)

    assert interaction.add_root_calls == [game_root]
    assert handle.identifier == game_id(game_root)
    assert sandboxes[0].persistence_roots == [None]
    assert "game_source_saves" in sandboxes[0].calls
    assert "game_saves" not in sandboxes[0].calls
    assert sandboxes[0].source_roots == [game_root]
    assert ("/game", False) in sandboxes[0].binds
    assert ("/game/save", True) in sandboxes[0].binds
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(game_root))
        saves = sandbox.game_source_saves(game, descriptor)
        sandbox.bind(sandbox.keep(os.dup(descriptor)), "/game")
        sandbox.bind(saves, "/game/save", writable=True)
        options = list(sandbox.options)
    game_index = options.index("/game")
    assert options[game_index - 2] == "--ro-bind"
    save_index = options.index("/game/save")
    assert options[save_index - 2] == "--bind"


def test_easyrpg_packed_launch_allow_game_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Packed EasyRPG with game writes mounts unpacked /game writable plus source saves."""
    source, _executable = _packed_source(tmp_path)
    cache = _rpg_rt_tree(tmp_path / "cache-entry")
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_easyrpg(tmp_path, "0.8.1.1")
    sandboxes = _patch_sandbox(monkeypatch)
    _patch_spawn(monkeypatch)

    def fake_unpack(paths_arg: AppPaths, exe: Path) -> Path:
        del paths_arg, exe
        return cache

    monkeypatch.setattr(evb_module, "ensure_unpacked", fake_unpack)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(
        paths,
        repository,
        source,
        "0.8.1.1",
        False,
        allow_game_writes=True,
        interaction=FakeInteraction(),
    )

    assert handle.identifier == game_id(source)
    assert sandboxes[0].allow_game_writes is True
    assert "game_writable" in sandboxes[0].calls
    assert "game_source_saves" in sandboxes[0].calls
    assert "bind:/game" not in sandboxes[0].calls
    assert "bind:/game/save" in sandboxes[0].calls
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(cache))
        sandbox.game_writable(os.dup(descriptor))
        options = list(sandbox.options)
    game_index = options.index("/game")
    assert options[game_index - 2] == "--bind"


def test_easyrpg_packed_launch_survives_migration_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration failures never fail the launch; stale saves stay behind."""
    source, _executable = _packed_source(tmp_path)
    cache = _rpg_rt_tree(tmp_path / "cache-entry")
    (cache / "save").mkdir()
    (cache / "save" / "Save01.lsd").write_bytes(b"old progress")
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    _install_easyrpg(tmp_path, "0.8.1.1")
    _patch_sandbox(monkeypatch)
    _patch_spawn(monkeypatch)

    def failing_move(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("injected migration failure")

    monkeypatch.setattr(launch_module, "_move_save_file", failing_move)
    monkeypatch.setattr(launch_module, "_move_save_directory", failing_move)

    def fake_unpack(paths_arg: AppPaths, exe: Path) -> Path:
        del paths_arg, exe
        return cache

    monkeypatch.setattr(evb_module, "ensure_unpacked", fake_unpack)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(paths, repository, source, "0.8.1.1", False, interaction=FakeInteraction())

    assert handle.identifier == game_id(source)
    assert (cache / "save" / "Save01.lsd").read_bytes() == b"old progress"


def test_migrate_leaves_disjoint_non_empty_source_untouched(tmp_path: Path) -> None:
    """A non-empty source save/ with disjoint names blocks migration entirely."""
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    (unpacked / "save").mkdir()
    (unpacked / "save" / "Save02.lsd").write_bytes(b"stale progress")
    source = _rpg_rt_tree(tmp_path / "source")
    (source / "save").mkdir()
    (source / "save" / "Save01.lsd").write_bytes(b"current progress")

    launch_module._migrate_easyrpg_saves_to_source(unpacked, source)

    assert (source / "save" / "Save01.lsd").read_bytes() == b"current progress"
    assert not os.path.lexists(source / "save" / "Save02.lsd")
    assert (unpacked / "save" / "Save02.lsd").read_bytes() == b"stale progress"


def test_migrate_skips_non_directory_source_save(tmp_path: Path) -> None:
    """A source save/ that is a symlink or file triggers an early return."""
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    (unpacked / "save").mkdir()
    (unpacked / "save" / "Save01.lsd").write_bytes(b"player progress")
    source = _rpg_rt_tree(tmp_path / "source")
    target = tmp_path / "target"
    target.mkdir()

    (source / "save").symlink_to(target)
    launch_module._migrate_easyrpg_saves_to_source(unpacked, source)
    assert (unpacked / "save" / "Save01.lsd").read_bytes() == b"player progress"
    assert list(target.iterdir()) == []

    (source / "save").unlink()
    (source / "save").write_bytes(b"not a directory")
    launch_module._migrate_easyrpg_saves_to_source(unpacked, source)
    assert (unpacked / "save" / "Save01.lsd").read_bytes() == b"player progress"
    assert (source / "save").read_bytes() == b"not a directory"


def test_migrate_skips_unsafe_source_save_permissions(tmp_path: Path) -> None:
    """An empty group/other-writable source save/ never receives migrated files."""
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    (unpacked / "save").mkdir()
    (unpacked / "save" / "Save01.lsd").write_bytes(b"player progress")
    source = _rpg_rt_tree(tmp_path / "source")
    (source / "save").mkdir()
    (source / "save").chmod(0o777)

    launch_module._migrate_easyrpg_saves_to_source(unpacked, source)

    assert (unpacked / "save" / "Save01.lsd").read_bytes() == b"player progress"
    assert list((source / "save").iterdir()) == []


def test_migrate_copy_failures_leave_sources_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable saves stay behind without failing the migration."""
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    (unpacked / "save").mkdir()
    (unpacked / "save" / "Save01.lsd").write_bytes(b"player progress")
    source = _rpg_rt_tree(tmp_path / "source")
    real_fdopen = os.fdopen

    class _FailingReadBinary:
        """Wrap a binary file whose reads always fail."""

        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> _FailingReadBinary:
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()

        def read(self, size: int = -1) -> bytes:
            raise OSError("injected read failure")

        def write(self, data: bytes) -> int:
            return self._handle.write(data)

    def failing_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        return _FailingReadBinary(real_fdopen(fd, *args, **kwargs))

    monkeypatch.setattr(os, "fdopen", failing_fdopen)
    try:
        launch_module._migrate_easyrpg_saves_to_source(unpacked, source)
    finally:
        monkeypatch.undo()

    assert (unpacked / "save" / "Save01.lsd").read_bytes() == b"player progress"
    assert not os.path.lexists(source / "save" / "Save01.lsd")


def test_migrate_write_failures_remove_partial_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unwritable destinations leave no partial file and keep the source."""
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    (unpacked / "save").mkdir()
    (unpacked / "save" / "Save01.lsd").write_bytes(b"player progress")
    source = _rpg_rt_tree(tmp_path / "source")
    real_fdopen = os.fdopen

    class _FailingWriteBinary:
        """Wrap a binary file whose writes always fail."""

        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> _FailingWriteBinary:
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()

        def read(self, size: int = -1) -> bytes:
            return self._handle.read(size)

        def write(self, data: bytes) -> int:
            raise OSError("injected write failure")

    def failing_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        return _FailingWriteBinary(real_fdopen(fd, *args, **kwargs))

    monkeypatch.setattr(os, "fdopen", failing_fdopen)
    try:
        launch_module._migrate_easyrpg_saves_to_source(unpacked, source)
    finally:
        monkeypatch.undo()

    assert (unpacked / "save" / "Save01.lsd").read_bytes() == b"player progress"
    assert not os.path.lexists(source / "save" / "Save01.lsd")


def test_move_save_file_skips_existing_destination(tmp_path: Path) -> None:
    """Pre-existing destinations are never truncated; the source stays behind."""
    origin = tmp_path / "Save01.lsd"
    origin.write_bytes(b"stale progress")
    target_dir = tmp_path / "save"
    target_dir.mkdir()
    target = target_dir / "Save01.lsd"
    target.write_bytes(b"current progress")

    launch_module._move_save_file(origin, target)

    assert target.read_bytes() == b"current progress"
    assert origin.read_bytes() == b"stale progress"


def test_migrate_skips_destination_created_after_emptiness_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file racing the emptiness check wins; migration skips instead of truncating."""
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    (unpacked / "save").mkdir()
    (unpacked / "save" / "Save01.lsd").write_bytes(b"stale progress")
    source = _rpg_rt_tree(tmp_path / "source")
    (source / "save").mkdir()
    real_open = os.open

    def racing_open(path: str | bytes | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        if flags & os.O_EXCL and str(path).endswith("Save01.lsd"):
            racing_descriptor = real_open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(racing_descriptor, b"concurrent progress")
            finally:
                os.close(racing_descriptor)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", racing_open)

    launch_module._migrate_easyrpg_saves_to_source(unpacked, source)

    assert (source / "save" / "Save01.lsd").read_bytes() == b"concurrent progress"
    assert (unpacked / "save" / "Save01.lsd").read_bytes() == b"stale progress"


def test_migrate_caps_wide_top_level_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Top-level entries beyond the scan cap stay behind without failing."""
    monkeypatch.setattr(launch_module, "_MAX_MIGRATION_SCAN_ENTRIES", 5)
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    (unpacked / "save").mkdir()
    for index in range(8):
        (unpacked / "save" / f"Save{index:02d}.lsd").write_bytes(f"progress {index}".encode())
    source = _rpg_rt_tree(tmp_path / "source")

    launch_module._migrate_easyrpg_saves_to_source(unpacked, source)

    assert len(list((source / "save").iterdir())) == 5
    assert len(list((unpacked / "save").iterdir())) == 3


def test_migrate_caps_nested_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nested entries share the same scan budget as top-level entries."""
    monkeypatch.setattr(launch_module, "_MAX_MIGRATION_SCAN_ENTRIES", 4)
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    nested = unpacked / "save" / "nested"
    nested.mkdir(parents=True)
    for index in range(6):
        (nested / f"Save{index:02d}.lsd").write_bytes(f"progress {index}".encode())
    source = _rpg_rt_tree(tmp_path / "source")

    launch_module._migrate_easyrpg_saves_to_source(unpacked, source)

    assert len(list((source / "save" / "nested").iterdir())) == 3
    assert len(list((unpacked / "save" / "nested").iterdir())) == 3


def test_migrate_skips_over_budget_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files over the per-file byte cap stay behind while smaller files travel."""
    monkeypatch.setattr(launch_module, "_MAX_MIGRATION_FILE_BYTES", 16)
    unpacked = _rpg_rt_tree(tmp_path / "unpacked")
    (unpacked / "save").mkdir()
    (unpacked / "save" / "Save01.lsd").write_bytes(b"small")
    (unpacked / "save" / "Save02.lsd").write_bytes(b"x" * 17)
    source = _rpg_rt_tree(tmp_path / "source")

    launch_module._migrate_easyrpg_saves_to_source(unpacked, source)

    assert (source / "save" / "Save01.lsd").read_bytes() == b"small"
    assert not os.path.lexists(source / "save" / "Save02.lsd")
    assert (unpacked / "save" / "Save02.lsd").read_bytes() == b"x" * 17
