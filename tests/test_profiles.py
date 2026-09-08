from pathlib import Path

import pytest

import box.paths
from box.errors import ConfigurationError
from box.games.identity import game_id
from box.launch.profiles import ProfileCatalog
from box.models import EngineName, GameInfo
from box.paths import AppPaths


def _game(root: Path) -> GameInfo:
    return GameInfo(EngineName.RPG_MAKER_MZ, root, root / "index.html", root / "package.json")


def test_profile_catalog_creates_a_private_stable_profile_for_each_game(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    first_root = tmp_path / "games" / "first"
    second_root = tmp_path / "games" / "second"
    first_root.mkdir(parents=True)
    second_root.mkdir()
    catalog = ProfileCatalog(paths)

    first = catalog.create_for_game(_game(first_root))
    assert catalog.create_for_game(_game(first_root)) == first
    second = catalog.create_for_game(_game(second_root))

    assert first == paths.profiles_root / game_id(first_root)
    assert second == paths.profiles_root / game_id(second_root)
    assert first != second
    assert first.stat().st_mode & 0o777 == 0o700
    assert catalog.list() == tuple(sorted((first, second)))

    first.chmod(0o755)
    assert catalog.create_for_game(_game(first_root)) == first
    assert first.stat().st_mode & 0o777 == 0o700


def test_profile_catalog_refuses_to_remove_an_unmanaged_or_symlinked_profile(
    tmp_path: Path,
) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    catalog = ProfileCatalog(paths)
    outside = tmp_path / "outside"
    outside.mkdir()
    profile = paths.profiles_root / "0123456789abcdef"
    profile.symlink_to(outside, target_is_directory=True)

    assert catalog.list() == ()
    with pytest.raises(ConfigurationError, match="contains a symlink"):
        catalog.remove(profile)
    with pytest.raises(ConfigurationError, match="unexpected"):
        catalog.remove(tmp_path / "0123456789abcdef")


def test_profile_catalog_handles_a_concurrent_profile_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    game_root = tmp_path / "games" / "sample"
    game_root.mkdir(parents=True)
    catalog = ProfileCatalog(paths)
    original_mkdir = box.paths.os.mkdir

    def create_then_report_race(name: str, mode: int, *, dir_fd: int) -> None:
        original_mkdir(name, mode, dir_fd=dir_fd)
        raise FileExistsError

    monkeypatch.setattr("box.paths.os.mkdir", create_then_report_race)

    profile = catalog.create_for_game(_game(game_root))

    assert profile.is_dir()
