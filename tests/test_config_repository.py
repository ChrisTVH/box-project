from pathlib import Path

import pytest

from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.config.validator import decode_config
from box.errors import ConfigurationError
from box.paths import AppPaths


def test_config_repository_persists_toml_configuration(tmp_path: Path) -> None:
    game_root = tmp_path / "games" / "example"
    game_root.mkdir(parents=True)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    expected = AppConfig(
        allowed_game_roots=(game_root,),
        preferred_runtime="v0.90.0",
        prefer_sdk=True,
    )

    repository.save(expected)

    assert paths.config_file.is_file()
    assert paths.config_file.read_text(encoding="utf-8") == (
        "schema_version = 1\n"
        f'allowed_game_roots = ["{game_root}"]\n'
        "prefer_sdk = true\n"
        'preferred_runtime = "v0.90.0"\n'
    )
    assert ConfigRepository(paths).load() == expected


def test_config_repository_adds_each_allowed_root_once(tmp_path: Path) -> None:
    game_root = tmp_path / "games" / "example"
    game_root.mkdir(parents=True)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)

    first = repository.add_allowed_root(game_root)
    second = repository.add_allowed_root(game_root)

    assert first.allowed_game_roots == (game_root,)
    assert second == first
    assert repository.load() == first


def test_config_repository_prunes_missing_allowed_roots_without_deleting_data(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "games" / "missing"
    dangling_root = tmp_path / "games" / "dangling"
    existing_root = tmp_path / "games" / "existing"
    existing_root.mkdir(parents=True)
    dangling_root.symlink_to(missing_root, target_is_directory=True)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.save(AppConfig(allowed_game_roots=(missing_root, dangling_root, existing_root)))

    updated = repository.prune_missing_allowed_roots()

    assert updated.allowed_game_roots == (existing_root,)
    assert repository.load() == updated
    assert existing_root.is_dir()


def test_config_repository_add_prunes_missing_allowed_roots(tmp_path: Path) -> None:
    missing_root = tmp_path / "games" / "missing"
    new_root = tmp_path / "games" / "new"
    new_root.mkdir(parents=True)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.save(AppConfig(allowed_game_roots=(missing_root,)))

    updated = repository.add_allowed_root(new_root)

    assert updated.allowed_game_roots == (new_root,)


def test_config_repository_removes_an_exact_allowed_root_without_deleting_it(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "games" / "example"
    other_root = game_root / "other"
    game_root.mkdir(parents=True)
    other_root.mkdir()
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.save(
        AppConfig(
            allowed_game_roots=(game_root, other_root),
            preferred_runtime="v0.90.0",
            prefer_sdk=True,
        )
    )

    updated = repository.remove_allowed_root(game_root)

    assert updated == AppConfig(
        allowed_game_roots=(other_root,),
        preferred_runtime="v0.90.0",
        prefer_sdk=True,
    )
    assert repository.load() == updated
    assert game_root.is_dir()


def test_config_repository_removes_missing_or_symlinked_allowed_roots(tmp_path: Path) -> None:
    missing_root = tmp_path / "games" / "missing"
    symlink_root = tmp_path / "games" / "symlink"
    outside = tmp_path / "outside"
    missing_root.mkdir(parents=True)
    symlink_root.mkdir()
    outside.mkdir()
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.save(AppConfig(allowed_game_roots=(missing_root, symlink_root)))
    missing_root.rmdir()
    symlink_root.rmdir()
    symlink_root.symlink_to(outside, target_is_directory=True)

    updated = repository.remove_allowed_root(missing_root)
    updated = repository.remove_allowed_root(symlink_root)

    assert updated.allowed_game_roots == ()
    assert repository.load() == updated
    assert not missing_root.exists()
    assert symlink_root.is_symlink()
    assert outside.is_dir()


def test_config_repository_clears_allowed_roots_without_deleting_directories(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "games" / "example"
    other_root = tmp_path / "games" / "other"
    game_root.mkdir(parents=True)
    other_root.mkdir()
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.save(
        AppConfig(
            allowed_game_roots=(game_root, other_root),
            preferred_runtime="v0.90.0",
            prefer_sdk=True,
        )
    )

    updated = repository.clear_allowed_roots()

    assert updated == AppConfig(preferred_runtime="v0.90.0", prefer_sdk=True)
    assert repository.load() == updated
    assert game_root.is_dir()
    assert other_root.is_dir()


@pytest.mark.parametrize("version", [True, 1.0])
def test_config_rejects_non_integer_schema_versions(version: object) -> None:
    with pytest.raises(ConfigurationError):
        decode_config({"schema_version": version})


def test_config_rejects_relative_game_roots() -> None:
    with pytest.raises(ConfigurationError, match="absolute paths"):
        decode_config({"allowed_game_roots": ["games"]})
