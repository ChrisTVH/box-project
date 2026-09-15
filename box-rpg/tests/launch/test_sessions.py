import json
import os
from pathlib import Path
from uuid import UUID

import pytest

from box.errors import ConfigurationError, LaunchError
from box.games.identity import game_id
from box.launch.links import open_game_root
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
        assert session.profile_root == paths.profiles_root / game_id(game_root)
        assert session.profile_root.stat().st_mode & 0o777 == 0o700
        assert (session.root / "game").is_symlink()
        assert (session.root / "game").readlink() == Path("/game")
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
    assert session.profile_root.is_dir()
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
    game_root = tmp_path / "sample-game"
    entrypoint = game_root / "index.html"
    manifest = game_root / "package.json"
    entrypoint.parent.mkdir()
    entrypoint.write_text("<html>game</html>", encoding="utf-8")
    manifest.write_text('{"name": ""}', encoding="utf-8")
    game = GameInfo(EngineName.RPG_MAKER_MV, game_root, entrypoint, manifest)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")

    with create_session(paths, game) as session:
        wrapper = json.loads((session.root / "package.json").read_text(encoding="utf-8"))

    assert wrapper["name"] == "sample-game"


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


def test_session_creation_closes_its_game_descriptor_when_profile_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    (paths.profiles_root / game_id(game_root)).symlink_to(outside, target_is_directory=True)
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root, entrypoint, manifest)
    descriptors: list[int] = []

    def open_descriptor(root: Path) -> int:
        descriptor = open_game_root(root)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr("box.launch.session.open_game_root", open_descriptor)

    with pytest.raises(ConfigurationError, match="unsafe"):
        create_session(paths, game)
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_session_creation_restricts_permissions_of_an_existing_game_session_directory(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    entrypoint = game_root / "index.html"
    manifest = game_root / "package.json"
    entrypoint.parent.mkdir()
    entrypoint.write_text("<html></html>", encoding="utf-8")
    manifest.write_text('{"name": "Test"}', encoding="utf-8")
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root, entrypoint, manifest)
    directory = paths.sessions_root / game_id(game_root)
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)

    with create_session(paths, game):
        assert directory.stat().st_mode & 0o777 == 0o700


def test_session_links_the_validated_game_directory(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    entrypoint = game_root / "index.html"
    manifest = game_root / "package.json"
    entrypoint.parent.mkdir()
    entrypoint.write_text("original", encoding="utf-8")
    manifest.write_text('{"name": "Original"}', encoding="utf-8")
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root, entrypoint, manifest)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")

    with create_session(paths, game) as session:
        assert (session.reference / "game").readlink() == Path("/game")


def test_session_cleanup_does_not_remove_a_replacement_directory(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    entrypoint = game_root / "index.html"
    manifest = game_root / "package.json"
    entrypoint.parent.mkdir()
    entrypoint.write_text("original", encoding="utf-8")
    manifest.write_text('{"name": "Original"}', encoding="utf-8")
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root, entrypoint, manifest)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    session = create_session(paths, game)
    original = tmp_path / "original-session"
    session.root.rename(original)
    session.root.mkdir()

    with pytest.raises(LaunchError, match="changed before cleanup"):
        session.cleanup()

    assert session.root.is_dir()
