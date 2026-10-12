"""NW.js --allow-game-writes persistence layout.

Covers the overlay manifest payload, the API switch between the ephemeral
/session app directory and the persistent writable /game tree, the
ci-mount rejection, and real Bubblewrap probes proving screenshot-style
writes persist to the host game root only with the flag on.
"""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import cast

import pytest

from box.api.launch import launch
from box.config.repository import ConfigRepository
from box.engines.registry import EngineRegistry
from box.errors import GameValidationError, LaunchError
from box.launch.manifest import write_manifest
from box.launch.sandbox import Sandbox, clean_environment
from box.launch.session import create_session
from box.launch.supervisor import LaunchedSession
from box.models import EngineName, GameInfo
from box.paths import AppPaths, open_directory_without_symlinks
from box.runtime.platform import current_architecture


class FakeSandbox:
    """Record sandbox policy calls without touching Bubblewrap."""

    def __init__(self, *, allow_network: bool = False, allow_game_writes: bool = False) -> None:
        self.allow_network = allow_network
        self.allow_game_writes = allow_game_writes
        self.binds: list[tuple[str, bool]] = []
        self.calls: list[str] = []
        self.last_cwd = "/"
        self.last_arguments: list[str] = []
        self._kept: list[int] = []

    def __enter__(self) -> FakeSandbox:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
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
        return "/runtime/fake"

    def display_probe(self) -> str:
        return "wayland"

    def desktop(self) -> None:
        pass

    def x11(self) -> None:
        pass

    def devices(self) -> None:
        pass

    def audio(self) -> None:
        pass

    def persistence(self, paths: AppPaths, game: GameInfo, game_root: Path | None = None) -> None:
        pass

    def game_saves(self, game: GameInfo, descriptor: int) -> int:
        return descriptor

    def game_writable(self, descriptor: int) -> None:
        self.calls.append("game_writable")
        self._kept.append(descriptor)

    def nw_game(self, game: GameInfo, descriptor: int, saves: int) -> None:
        self.calls.append("nw_game")

    def bind(self, descriptor: int, destination: str, *, writable: bool = False) -> None:
        self.binds.append((destination, writable))

    def command(self, arguments: list[str], *, cwd: str = "/") -> list[str]:
        self.last_cwd = cwd
        self.last_arguments = list(arguments)
        return ["fake-bwrap", *arguments]


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


def _nwjs_game(tmp_path: Path) -> GameInfo:
    root = tmp_path / "game"
    root.mkdir()
    entry = root / "index.html"
    entry.write_text("fixture", encoding="utf-8")
    manifest = root / "package.json"
    manifest.write_text(json.dumps({"name": "fixture", "window": {"width": 640}}), encoding="utf-8")
    return GameInfo(EngineName.RPG_MAKER_MZ, root, entry, manifest)


def _install_nwjs(tmp_path: Path, version: str) -> None:
    arch = current_architecture()
    binary = (
        tmp_path
        / "cache"
        / "runtimes"
        / "nwjs"
        / f"linux-{arch}"
        / f"standard-{version.lower()}"
        / "nw"
    )
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)


def _patch_sandbox(monkeypatch: pytest.MonkeyPatch) -> list[FakeSandbox]:
    sandboxes: list[FakeSandbox] = []

    def factory(*, allow_network: bool = False, allow_game_writes: bool = False) -> FakeSandbox:
        sandbox = FakeSandbox(allow_network=allow_network, allow_game_writes=allow_game_writes)
        sandboxes.append(sandbox)
        return sandbox

    monkeypatch.setattr("box.api.launch.Sandbox", factory)
    return sandboxes


def _patch_spawn(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
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
        ci_mountpoint: Path | None = None,
    ) -> LaunchedSession:
        commands.append(list(command))
        return LaunchedSession(identifier, name, paths.sessions_root / identifier / name)

    monkeypatch.setattr("box.api.launch.spawn_detached", fake_spawn)
    return commands


def test_default_manifest_keeps_game_prefix(tmp_path: Path) -> None:
    game = _nwjs_game(tmp_path)
    session = tmp_path / "session"
    session.mkdir()
    payload = cast(dict[str, object], json.loads(write_manifest(session, game).read_text()))
    assert payload["main"] == "game/index.html"
    assert payload["window"] == {"width": 640}


def test_overlay_manifest_uses_game_relative_main(tmp_path: Path) -> None:
    game = _nwjs_game(tmp_path)
    session = tmp_path / "session"
    session.mkdir()
    other = tmp_path / "other-session"
    other.mkdir()
    overlay = write_manifest(session, game, filename="game-package.json", for_game_view=True)
    assert overlay.name == "game-package.json"
    payload = cast(dict[str, object], json.loads(overlay.read_text()))
    assert payload["main"] == "index.html"
    assert payload["window"] == {"width": 640}
    assert "chromium-args" not in payload
    # Default and overlay payloads agree except for the main prefix.
    default = cast(dict[str, object], json.loads(write_manifest(other, game).read_text()))
    assert default["main"] == "game/index.html"
    assert default["window"] == payload["window"]
    assert default["name"] == payload["name"]


def test_overlay_manifest_nested_entry_and_devtools(tmp_path: Path) -> None:
    root = tmp_path / "game"
    parent = root / "www"
    parent.mkdir(parents=True)
    (parent / "index.html").write_text("fixture", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps({"name": "nested", "chromium-args": "--no-sandbox"}), encoding="utf-8"
    )
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, parent / "index.html", root / "package.json")
    session = tmp_path / "session"
    session.mkdir()
    overlay = write_manifest(
        session, game, devtools=True, filename="game-package.json", for_game_view=True
    )
    payload = cast(dict[str, object], json.loads(overlay.read_text()))
    assert payload["main"] == "www/index.html"
    # Game-controlled switches stay filtered; only the fixed launcher flag lands.
    assert payload["chromium-args"] == "--auto-open-devtools-for-tabs"


@pytest.mark.parametrize("filename", ["../evil", "sub/dir", "", ".", "..", "/absolute"])
def test_overlay_manifest_rejects_unsafe_filename(tmp_path: Path, filename: str) -> None:
    game = _nwjs_game(tmp_path)
    session = tmp_path / "session"
    session.mkdir()
    with pytest.raises(LaunchError):
        write_manifest(session, game, filename=filename, for_game_view=True)


def test_launch_without_writes_keeps_session_appdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_nwjs(tmp_path, "v0.90.0")
    sandboxes = _patch_sandbox(monkeypatch)
    _patch_spawn(monkeypatch)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(paths, repository, game.root, "v0.90.0", False)
    assert handle.identifier
    assert len(sandboxes) == 1
    assert sandboxes[0].last_cwd == "/session/game"
    assert sandboxes[0].last_arguments[-1] == "/session"
    assert sandboxes[0].calls == ["nw_game"]
    assert ("/game/package.json", False) not in sandboxes[0].binds
    assert ("/session", False) in sandboxes[0].binds
    assert ("/session/save", True) in sandboxes[0].binds
    assert not (handle.root / "game-package.json").exists()
    session_payload = cast(
        dict[str, object], json.loads((handle.root / "package.json").read_text())
    )
    assert session_payload["main"] == "game/index.html"


def test_launch_with_writes_uses_game_appdir_and_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_nwjs(tmp_path, "v0.90.0")
    sandboxes = _patch_sandbox(monkeypatch)
    _patch_spawn(monkeypatch)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.delenv("DISPLAY", raising=False)

    handle = launch(paths, repository, game.root, "v0.90.0", False, allow_game_writes=True)
    assert handle.identifier
    assert len(sandboxes) == 1
    assert sandboxes[0].allow_game_writes is True
    assert sandboxes[0].last_cwd == "/game"
    assert sandboxes[0].last_arguments[-1] == "/game"
    # Whole-tree writable bind persists new files; the tmpfs view is skipped.
    assert sandboxes[0].calls == ["game_writable"]
    # The launcher overlay shadows the writable game manifest read-only.
    assert ("/game/package.json", False) in sandboxes[0].binds
    # The session stays bound read-only to protect lock/status files.
    assert ("/session", False) in sandboxes[0].binds
    assert ("/session/save", True) in sandboxes[0].binds
    overlay = cast(dict[str, object], json.loads((handle.root / "game-package.json").read_text()))
    assert overlay["main"] == "index.html"
    assert overlay["window"] == {"width": 640}
    session_payload = cast(
        dict[str, object], json.loads((handle.root / "package.json").read_text())
    )
    assert session_payload["main"] == "game/index.html"


def test_launch_rejects_ci_mount_with_game_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _nwjs_game(tmp_path)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game.root)
    _install_nwjs(tmp_path, "v0.90.0")

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def forbidden_spawn(*args: object, **kwargs: object) -> LaunchedSession:
        raise AssertionError("rejected ci-mount launch must not run")

    monkeypatch.setattr("box.api.launch.detect_game", fake_detect)
    monkeypatch.setattr("box.api.launch.spawn_detached", forbidden_spawn)
    with pytest.raises(GameValidationError, match=r"--ci-mount.*--allow-game-writes"):
        launch(
            paths, repository, game.root, "v0.90.0", False, ci_mount=True, allow_game_writes=True
        )


def _make_probe_game(game_root: Path) -> GameInfo:
    game_root.mkdir()
    (game_root / "index.html").write_text("fixture", encoding="utf-8")
    (game_root / "package.json").write_text('{"name": "screenshot-probe"}', encoding="utf-8")
    return GameInfo(
        EngineName.RPG_MAKER_MZ, game_root, game_root / "index.html", game_root / "package.json"
    )


def _make_probe_runtime(tmp_path: Path, script: str) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    probe = runtime / "probe"
    probe.write_text("#!/usr/bin/python3\n" + script)
    probe.chmod(0o700)
    return probe


def test_real_game_writes_screenshot_persists_to_host(tmp_path: Path) -> None:
    """Harmless probe: mkdir /game/screenshot persists with the flag on."""
    game_root = tmp_path / "game"
    game = _make_probe_game(game_root)
    original_package = (game_root / "package.json").read_text(encoding="utf-8")
    paths = _paths(tmp_path)
    probe = _make_probe_runtime(
        tmp_path,
        "import json\n"
        "from pathlib import Path\n"
        "# A stock screenshot plugin resolves against the app directory.\n"
        "Path('/game/screenshot').mkdir()\n"
        "Path('/game/screenshot/shot.txt').write_text('screenshot')\n"
        "assert Path('/game/screenshot/shot.txt').read_text() == 'screenshot'\n"
        "Path('/game/save/slot').write_text('saved')\n"
        "assert Path('/saves/slot').read_text() == 'saved'\n"
        "assert Path('/session/save/slot').read_text() == 'saved'\n"
        "overlay = json.loads(Path('/game/package.json').read_text())\n"
        "assert overlay['main'] == 'index.html', overlay\n"
        "for path in ('/game/package.json', '/session/package.json'):\n"
        "    try: Path(path).write_text('forbidden')\n"
        "    except OSError: pass\n"
        "    else: raise AssertionError('writable launcher manifest: ' + path)\n"
        "print('screenshot-ok')\n",
    )
    with create_session(paths, game) as session, Sandbox(allow_game_writes=True) as sandbox:
        executable = sandbox.runtime(probe)
        sandbox.persistence(paths, game)
        descriptor = sandbox.keep(open_directory_without_symlinks(game_root))
        saves = sandbox.game_saves(game, descriptor)
        sandbox.game_writable(os.dup(descriptor))
        sandbox.bind(sandbox.keep(os.dup(session.session_descriptor)), "/session")
        sandbox.bind(saves, "/session/save", writable=True)
        write_manifest(
            session.root,
            game,
            session_descriptor=session.session_descriptor,
            game_descriptor=descriptor,
            filename="game-package.json",
            for_game_view=True,
        )
        overlay = sandbox.keep(
            os.open(
                "game-package.json",
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=session.session_descriptor,
            )
        )
        sandbox.bind(overlay, "/game/package.json")
        result = subprocess.run(
            sandbox.command([executable], cwd="/game"),
            pass_fds=sandbox.pass_fds,
            env=clean_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "screenshot-ok\n"
    assert (game_root / "screenshot" / "shot.txt").read_text(encoding="utf-8") == "screenshot"
    assert (game_root / "save" / "slot").read_text(encoding="utf-8") == "saved"
    # The overlay shadowed the writable game manifest on the host.
    assert (game_root / "package.json").read_text(encoding="utf-8") == original_package


def test_real_game_writes_off_blocks_new_game_files(tmp_path: Path) -> None:
    """Harmless probe: mkdir /game/screenshot fails without the flag."""
    game_root = tmp_path / "game"
    game = _make_probe_game(game_root)
    paths = _paths(tmp_path)
    probe = _make_probe_runtime(
        tmp_path,
        "from pathlib import Path\n"
        "try: Path('/game/screenshot').mkdir()\n"
        "except OSError: pass\n"
        "else: raise AssertionError('writable game tree without the flag')\n"
        "Path('/game/save/slot').write_text('saved')\n"
        "print('read-only-ok')\n",
    )
    with create_session(paths, game) as session, Sandbox() as sandbox:
        executable = sandbox.runtime(probe)
        sandbox.persistence(paths, game)
        descriptor = sandbox.keep(open_directory_without_symlinks(game_root))
        saves = sandbox.game_saves(game, descriptor)
        sandbox.nw_game(game, descriptor, saves)
        sandbox.bind(sandbox.keep(os.dup(session.session_descriptor)), "/session")
        sandbox.bind(saves, "/session/save", writable=True)
        result = subprocess.run(
            sandbox.command([executable], cwd="/session/game"),
            pass_fds=sandbox.pass_fds,
            env=clean_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "read-only-ok\n"
    assert not (game_root / "screenshot").exists()
    assert (game_root / "save" / "slot").read_text(encoding="utf-8") == "saved"
