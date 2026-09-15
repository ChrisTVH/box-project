"""Pure game-icon discovery/extraction tests (no display needed)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

try:
    import gi

    gi.require_version("GdkPixbuf", "2.0")

    from box.models import EngineName, GameInfo
    from gi.repository import GdkPixbuf

    import box_gui.core.game_icon as game_icon_module
    from box_gui.core.game_icon import (
        extract_icon_png,
        find_game_executables,
        icon_path_for_game,
        install_image_as_icon,
    )

    _game_icon_available = True
except Exception:
    _game_icon_available = False

pytestmark = pytest.mark.skipif(not _game_icon_available, reason="gi/icoextract unavailable")


def _make_game(root: Path) -> GameInfo:
    """Build a minimal GameInfo pointing at root."""
    return GameInfo(engine=EngineName.RPG_MAKER_MV, root=root)


def _write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _ico_bytes(size: int = 16) -> bytes:
    """Build minimal `.ico` bytes through the same loader under test."""
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, size, size)
    pixbuf.fill(0x336699FF)
    ok, data = pixbuf.save_to_bufferv("ico", [], [])
    assert ok
    return bytes(data)


def test_find_executables_unreadable_root_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fail(_game: GameInfo) -> tuple[str, ...]:
        raise OSError("simulated unreadable root")

    monkeypatch.setattr(game_icon_module, "list_root_files", _fail)

    assert find_game_executables(_make_game(tmp_path)) == ()


def test_find_executables_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(game_icon_module, "list_root_files", lambda _game: ())

    assert find_game_executables(_make_game(tmp_path)) == ()


def test_find_executables_filters_exe_only_sorted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only `.exe` names survive a mixed list_root_files result, sorted and rooted."""
    monkeypatch.setattr(
        game_icon_module,
        "list_root_files",
        lambda _game: (
            "readme.txt",
            "RPG_RT.exe",
            "game.EXE",
            "lib.dll",
            "icon.png",
            "setup.msi",
        ),
    )

    assert find_game_executables(_make_game(tmp_path)) == (
        tmp_path / "game.EXE",
        tmp_path / "RPG_RT.exe",
    )


def test_icon_path_lives_in_cache_dir(tmp_path: Path) -> None:
    """The icon destination is app-owned cache, never the game root."""
    config_root = tmp_path / "config"
    game_root = tmp_path / "game"

    dest = icon_path_for_game(config_root, game_root)

    assert dest.parent == config_root / "icons"
    assert dest.suffix == ".png"
    assert game_root not in dest.parents
    assert dest.parent.is_dir()


def test_icon_path_is_stable_per_game(tmp_path: Path) -> None:
    """The same game path always maps to the same cache file."""
    config_root = tmp_path / "config"

    assert icon_path_for_game(config_root, tmp_path / "a") == icon_path_for_game(
        config_root, tmp_path / "a"
    )
    assert icon_path_for_game(config_root, tmp_path / "a") != icon_path_for_game(
        config_root, tmp_path / "b"
    )


def test_icon_cache_dir_uses_user_only_permissions(tmp_path: Path) -> None:
    """The icon cache directory is created with 0o700 like library.json's own."""
    import stat

    config_root = tmp_path / "config"

    dest = icon_path_for_game(config_root, tmp_path / "game")

    assert dest.parent.is_dir()
    assert stat.S_IMODE(dest.parent.stat().st_mode) == 0o700


def test_extract_icon_png_missing_exe(tmp_path: Path) -> None:
    dest = tmp_path / "dest.png"
    assert extract_icon_png(tmp_path / "missing.exe", dest) is False
    assert not dest.exists()


def test_extract_icon_png_non_pe(tmp_path: Path) -> None:
    exe = _write(tmp_path / "Game.exe", b"not a portable executable")
    dest = tmp_path / "dest.png"
    assert extract_icon_png(exe, dest) is False
    assert not dest.exists()


def test_extract_icon_png_composes_ico_decode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The icoextract bytes reach the PNG writer through the real decode path."""
    payload = _ico_bytes()

    class _FakeExtractor:
        def __init__(self, _path: str) -> None:
            pass

        def get_icon(self, *_args: object, **_kwargs: object) -> io.BytesIO:
            return io.BytesIO(payload)

    monkeypatch.setattr(game_icon_module, "IconExtractor", _FakeExtractor)
    exe = _write(tmp_path / "Game.exe", b"fake")
    dest = tmp_path / "dest.png"

    assert extract_icon_png(exe, dest) is True
    assert dest.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_install_image_as_icon_round_trip(tmp_path: Path) -> None:
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 300, 10)
    source = tmp_path / "art.png"
    pixbuf.savev(str(source), "png", [], [])
    dest_dir = tmp_path / "game"
    dest_dir.mkdir()
    dest = dest_dir / "dest.png"

    assert install_image_as_icon(source, dest) is True
    assert dest.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    stored = GdkPixbuf.Pixbuf.new_from_file(str(dest))
    assert (stored.get_width(), stored.get_height()) == (256, 8)


def test_install_image_as_icon_rejects_non_image(tmp_path: Path) -> None:
    source = _write(tmp_path / "notes.txt", b"plain text")
    dest = tmp_path / "dest.png"

    assert install_image_as_icon(source, dest) is False
    assert not dest.exists()


def test_install_image_as_icon_missing_source(tmp_path: Path) -> None:
    dest = tmp_path / "dest.png"

    assert install_image_as_icon(tmp_path / "missing.png", dest) is False
    assert not dest.exists()
