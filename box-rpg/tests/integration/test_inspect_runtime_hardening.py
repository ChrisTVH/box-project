"""Focused regressions for untrusted presentation, metadata and runtime probes."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import os
import sys
from argparse import Namespace
from pathlib import Path
from typing import cast

import pytest

from box.cli import inspect, main
from box.diagnostics import versions
from box.errors import LaunchError
from box.games.inspector import Inspection, inspect_game
from box.launch import manifest
from box.launch.process import run_process, runtime_environment
from box.models import EngineName, GameInfo
from box.utils.terminal import safe_terminal_text


def test_terminal_controls_are_visible_without_changing_unicode() -> None:
    original = "Café 日本語\x1b]0;fake\x07\r\n\x85\u202e\u2066\u2028"
    assert safe_terminal_text(original) == (
        "Café 日本語\\x1b]0;fake\\x07\\x0d\\x0a\\x85\\u202e\\u2066\\u2028"
    )
    assert "\x1b" in original
    for code in (*range(32), *range(127, 160)):
        assert chr(code) not in safe_terminal_text(chr(code))


def test_inspect_escapes_only_presentation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "game\n\x1b[2J"
    game = GameInfo(EngineName.RPG_MAKER_MV, root, root / "index.html")
    inspection = Inspection(game, "title\r\u202e", 1)

    def inspect_stub(_path: Path, _registry: object = None) -> Inspection:
        return inspection

    monkeypatch.setattr("box.api.inspect.inspect_game", inspect_stub)
    assert inspect.execute(root) == 0
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 5
    assert "\\x0a\\x1b[2J" in output
    assert "title\\x0d\\u202e" in output
    assert game.root == root


def test_main_escapes_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_arguments: Namespace) -> int:
        raise LaunchError("bad\n\x1b[2J\u202e")

    monkeypatch.setattr(main, "_dispatch", fail)
    assert main.main(["inspect", "."]) == 1
    assert "bad\\x0a\\x1b[2J\\u202e" in capsys.readouterr().err


def test_metadata_limit_and_special_files(tmp_path: Path) -> None:
    path = tmp_path / "metadata"
    path.write_bytes(b"1234")
    assert manifest.read_regular_metadata(path, 4) == b"1234"
    with pytest.raises(ValueError, match="size limit"):
        manifest.read_regular_metadata(path, 3)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        manifest.read_regular_metadata(fifo, 4)
    with pytest.raises((OSError, ValueError)):
        manifest.read_regular_metadata(tmp_path, 4)


def test_metadata_rejects_symlinks_and_traversal(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "core").write_text("hello")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        manifest.read_regular_metadata(link / "core", 20)
    (real / "alias").symlink_to(real / "core")
    with pytest.raises(OSError):
        manifest.read_regular_metadata(real / "alias", 20)
    descriptor = os.open(real, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert (
            manifest.read_regular_metadata(Path("core"), 20, root_descriptor=descriptor) == b"hello"
        )
        with pytest.raises(ValueError):
            manifest.read_regular_metadata(Path("../real/core"), 20, root_descriptor=descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("window", ["fullscreen", {"width": True}, {"title": {}}, []])
def test_window_rejects_wrong_types(window: object) -> None:
    assert manifest._safe_window(window) == {}


def test_manifest_forwards_only_typed_presentation(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    session = tmp_path / "session"
    session.mkdir()
    package = game_root / "package.json"
    package.write_text(
        json.dumps(
            {
                "name": "Real\nTitle",
                "chromium-args": "--no-sandbox --load-extension=/tmp/code",
                "js-flags": "--expose-gc",
                "node-remote": "*",
                "window": {
                    "width": 816,
                    "height": 624,
                    "fullscreen": False,
                    "title": "Real\nTitle",
                    "icon": "/outside/icon",
                    "inject_js_start": "evil.js",
                    "max_width": -1,
                },
            }
        )
    )
    game = GameInfo(EngineName.RPG_MAKER_MV, game_root, game_root / "index.html", package)
    result = manifest.write_manifest(session, game)
    payload = cast(dict[str, object], json.loads(result.read_text()))
    assert payload == {
        "name": "Real\nTitle",
        "main": "game/index.html",
        "window": {"width": 816, "height": 624, "fullscreen": False, "title": "Real\nTitle"},
    }


def test_manifest_with_utf8_bom_is_accepted(tmp_path: Path) -> None:
    package = tmp_path / "package.json"
    package.write_bytes(b'\xef\xbb\xbf{"name": "BOM Game"}')
    game = GameInfo(EngineName.RPG_MAKER_MV, tmp_path, tmp_path / "index.html", package)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert manifest._read_game_manifest(game, descriptor) == {"name": "BOM Game"}
    finally:
        os.close(descriptor)


def test_inspect_accepts_manifest_with_utf8_bom(tmp_path: Path) -> None:
    root = tmp_path / "game"
    (root / "www" / "js").mkdir(parents=True)
    (root / "www" / "index.html").write_text("<html></html>")
    (root / "www" / "js" / "plugins.js").write_text("var $plugins = [];")
    (root / "package.json").write_bytes(b'\xef\xbb\xbf{"name": "BOM Game"}')
    assert inspect_game(root).title == "BOM Game"


@pytest.mark.parametrize("requested", [True, False])
def test_session_manifest_passes_fullscreen_through(tmp_path: Path, requested: bool) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    session = tmp_path / "session"
    session.mkdir()
    (game_root / "package.json").write_text(
        json.dumps({"name": "Plain", "window": {"width": 640, "fullscreen": requested}})
    )
    game = GameInfo(
        EngineName.RPG_MAKER_MV, game_root, game_root / "index.html", game_root / "package.json"
    )
    payload = cast(
        dict[str, object], json.loads(manifest.write_manifest(session, game).read_text())
    )
    assert payload["window"] == {"width": 640, "fullscreen": requested}


def test_session_manifest_omits_fullscreen_when_unrequested(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    session = tmp_path / "session"
    session.mkdir()
    (game_root / "package.json").write_text(json.dumps({"name": "Plain"}))
    game = GameInfo(
        EngineName.RPG_MAKER_MV, game_root, game_root / "index.html", game_root / "package.json"
    )
    payload = cast(
        dict[str, object], json.loads(manifest.write_manifest(session, game).read_text())
    )
    assert payload["window"] == {}


@pytest.mark.parametrize("content", [b"x" * (1024 * 1024 + 1), b"\xff", b"[", b"[" * 2000])
def test_bad_manifest_is_a_launch_error(tmp_path: Path, content: bytes) -> None:
    package = tmp_path / "package.json"
    package.write_bytes(content)
    game = GameInfo(EngineName.RPG_MAKER_MV, tmp_path, tmp_path / "index.html", package)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(LaunchError):
            manifest._read_game_manifest(game, descriptor)
    finally:
        os.close(descriptor)


def test_core_is_bounded_and_escaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    core = tmp_path / "core.js"
    core.write_text("RPGMAKER_VERSION = '1.2\x1b[2J';")
    assert versions._read_core_version(core) == "1.2\\x1b[2J"
    monkeypatch.setattr(versions, "_CORE_LIMIT", 8)
    assert versions._read_core_version(core) is None


def test_core_rejects_symlinked_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "core.js").write_text("RPGMAKER_VERSION = '1.2';")
    link = tmp_path / "js"
    link.symlink_to(real, target_is_directory=True)
    assert versions._read_core_version(link / "core.js") is None


def test_manifest_rejects_symlinked_root_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "game").mkdir()
    (real / "game" / "package.json").write_text("{}")
    link = tmp_path / "alias"
    link.symlink_to(real, target_is_directory=True)
    root = link / "game"
    session = tmp_path / "session"
    session.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_MV, root, root / "index.html", root / "package.json")
    with pytest.raises(LaunchError):
        manifest.write_manifest(session, game)
    assert not (session / "package.json").exists()


def test_environment_uses_constants_without_host_settings() -> None:
    source = {
        "PATH": "/bin",
        "HOME": "/home/player",
        "DISPLAY": ":1",
        "LANG": "en_US.UTF-8",
        "LD_PRELOAD": "evil.so",
        "LD_LIBRARY_PATH": "/evil",
        "NODE_OPTIONS": "--require=evil",
        "NW_PRE_ARGS": "--no-sandbox",
        "GCONV_PATH": "/evil",
        "DYLD_INSERT_LIBRARIES": "evil",
    }
    environment = runtime_environment(source)
    assert environment["HOME"] == "/home/sandbox"
    assert environment["PATH"] == "/usr/bin:/bin"
    assert "DISPLAY" not in environment
    assert all(key not in environment for key in source if key not in {"HOME", "PATH", "LANG"})
    assert source["LD_PRELOAD"] == "evil.so"


def test_launch_uses_filtered_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODE_OPTIONS", "--require=evil")
    with pytest.raises(LaunchError, match="without Bubblewrap"):
        run_process(
            [sys.executable, "-c", "import os; assert 'NODE_OPTIONS' not in os.environ"], tmp_path
        )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("print('v1\\x1b[2J')", "v1\\x1b[2J"),
        ("import sys; sys.stderr.write('v2')", "v2"),
        ("import os; os.write(1, b'v3\\xff')", "v3\ufffd"),
        ("print('x' * 70000)", "fallback"),
        ("import sys; sys.stderr.write('x' * 70000)", "fallback"),
        ("import time; time.sleep(5)", "fallback"),
        ("import os; assert 'NODE_OPTIONS' not in os.environ; print('clean')", "clean"),
        ("import os; os.write(1, b'x' * 40000); os.write(2, b'x' * 40000)", "fallback"),
        ("import os; os.write(1, b'x' * 65536)", "x" * 65536),
        ("import os, time; os.fork(); time.sleep(5)", "fallback"),
        ("import os, time; os.close(1); os.close(2); time.sleep(5)", "fallback"),
    ],
)
def test_binary_probe_limits_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, expected: str
) -> None:
    binary = tmp_path / "runtime"
    binary.write_text(f"#!/usr/bin/python3\n{code}\n")
    binary.chmod(0o700)
    monkeypatch.setenv("NODE_OPTIONS", "--require=evil")
    monkeypatch.setattr(versions, "_VERSION_TIMEOUT", 0.5)
    assert versions._binary_version(binary, "fallback") == expected
