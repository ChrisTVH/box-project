import json
from pathlib import Path
from uuid import UUID

import pytest

from box.errors import ConfigurationError, LaunchError
from box.games.identity import game_id
from box.launch.session import create_session
from box.models import EngineName, GameInfo
from box.paths import AppPaths


def _fixed_uuid() -> UUID:
    return UUID("12345678-1234-5678-1234-567812345678")


def test_session_links_game_writes_wrapper_and_cleans_up_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "game"
    entrypoint = game_root / "index.html"
    manifest = game_root / "package.json"
    entrypoint.parent.mkdir()
    entrypoint.write_text("<html>game</html>", encoding="utf-8")
    manifest.write_text(
        '{"name": "Session Test", "window": {"width": 960}, "ignored": true}',
        encoding="utf-8",
    )
    auxiliary = game_root / "game_messages.csv"
    auxiliary.write_text("messages", encoding="utf-8")
    original_files = {
        path.relative_to(game_root): path.read_bytes()
        for path in game_root.rglob("*")
        if path.is_file()
    }
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root, entrypoint, manifest)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    monkeypatch.setattr("box.launch.session.uuid4", _fixed_uuid)

    with create_session(paths, game, ("game_messages.csv",)) as session:
        assert session.root == paths.sessions_root / game_id(game_root) / _fixed_uuid().hex
        assert (session.root / "game").is_symlink()
        assert (session.root / "game").resolve() == game_root
        copied_auxiliary = session.root / "game_messages.csv"
        assert copied_auxiliary.is_file()
        assert not copied_auxiliary.is_symlink()
        assert copied_auxiliary.read_text(encoding="utf-8") == auxiliary.read_text(encoding="utf-8")
        assert json.loads((session.root / "package.json").read_text(encoding="utf-8")) == {
            "name": "Session Test",
            "main": "game/index.html",
            "window": {"width": 960},
        }

    assert not session.root.exists()
    assert {
        path.relative_to(game_root): path.read_bytes()
        for path in game_root.rglob("*")
        if path.is_file()
    } == original_files


def test_session_rejects_symlinked_game_root_files(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    entrypoint = game_root / "index.html"
    manifest = game_root / "package.json"
    entrypoint.parent.mkdir()
    entrypoint.write_text("<html>game</html>", encoding="utf-8")
    manifest.write_text('{"name": "Session Test"}', encoding="utf-8")
    outside = tmp_path / "outside.csv"
    outside.write_text("messages", encoding="utf-8")
    (game_root / "game_messages.csv").symlink_to(outside)
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root, entrypoint, manifest)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")

    with pytest.raises(LaunchError, match="missing or unsafe"):
        create_session(paths, game, ("game_messages.csv",))


def test_session_rejects_reserved_game_root_filenames(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    entrypoint = game_root / "index.html"
    manifest = game_root / "package.json"
    entrypoint.parent.mkdir()
    entrypoint.write_text("<html>game</html>", encoding="utf-8")
    manifest.write_text('{"name": "Session Test"}', encoding="utf-8")
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root, entrypoint, manifest)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")

    with pytest.raises(LaunchError, match="invalid game-root filename"):
        create_session(paths, game, ("package.json",))

    assert manifest.read_text(encoding="utf-8") == '{"name": "Session Test"}'


def test_session_uses_the_game_directory_name_when_manifest_name_is_empty(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "Roseliam-1.08"
    entrypoint = game_root / "index.html"
    manifest = game_root / "package.json"
    entrypoint.parent.mkdir()
    entrypoint.write_text("<html>game</html>", encoding="utf-8")
    manifest.write_text('{"name": ""}', encoding="utf-8")
    game = GameInfo(EngineName.RPG_MAKER_MV, game_root, entrypoint, manifest)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")

    with create_session(paths, game) as session:
        wrapper = json.loads((session.root / "package.json").read_text(encoding="utf-8"))

    assert wrapper["name"] == "Roseliam-1.08"


def test_session_creation_rejects_a_symlinked_cache_root(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    entrypoint = game_root / "index.html"
    manifest = game_root / "package.json"
    entrypoint.parent.mkdir()
    entrypoint.write_text("<html></html>", encoding="utf-8")
    manifest.write_text('{"name": "Test"}', encoding="utf-8")
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.cache_root.symlink_to(outside, target_is_directory=True)
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root, entrypoint, manifest)

    with pytest.raises(ConfigurationError, match="managed directory must not be a symlink"):
        create_session(paths, game)


def test_session_creation_rejects_a_symlinked_game_session_directory(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    entrypoint = game_root / "index.html"
    manifest = game_root / "package.json"
    entrypoint.parent.mkdir()
    entrypoint.write_text("<html></html>", encoding="utf-8")
    manifest.write_text('{"name": "Test"}', encoding="utf-8")
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    outside = tmp_path / "outside"
    outside.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root, entrypoint, manifest)
    (paths.sessions_root / game_id(game_root)).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="contains a symlink"):
        create_session(paths, game)
