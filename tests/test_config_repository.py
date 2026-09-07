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


@pytest.mark.parametrize("version", [True, 1.0])
def test_config_rejects_non_integer_schema_versions(version: object) -> None:
    with pytest.raises(ConfigurationError):
        decode_config({"schema_version": version})


def test_config_rejects_relative_game_roots() -> None:
    with pytest.raises(ConfigurationError, match="absolute paths"):
        decode_config({"allowed_game_roots": ["games"]})
