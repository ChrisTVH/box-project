from pathlib import Path

import pytest

from box.errors import ConfigurationError
from box.paths import AppPaths


@pytest.mark.parametrize("value", ["", ".", "relative", "~/cache"])
def test_invalid_xdg_values_use_absolute_home_fallback(tmp_path: Path, value: str) -> None:
    paths = AppPaths.from_environment(
        {"HOME": str(tmp_path), "XDG_CONFIG_HOME": value, "XDG_CACHE_HOME": value}
    )
    assert paths.config_root == tmp_path / ".config" / "box-rpg"
    assert paths.cache_root == tmp_path / ".cache" / "box-rpg"


@pytest.mark.parametrize("value", [".", "relative", "~/home"])
def test_relative_home_is_rejected(value: str) -> None:
    with pytest.raises(ConfigurationError, match="HOME must be an absolute path"):
        AppPaths.from_environment({"HOME": value})


def test_app_paths_use_xdg_environment_and_create_managed_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    cache_home = tmp_path / "xdg-cache"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))

    paths = AppPaths.from_environment()

    assert paths.config_root == config_home / "box-rpg"
    assert paths.cache_root == cache_home / "box-rpg"
    assert paths.config_file == config_home / "box-rpg" / "config.toml"
    assert paths.downloads_root == cache_home / "box-rpg" / "downloads" / "nwjs"
    assert paths.runtimes_root == cache_home / "box-rpg" / "runtimes" / "nwjs"
    assert paths.sessions_root == cache_home / "box-rpg" / "sessions"
    assert paths.reports_root == cache_home / "box-rpg" / "reports"
    assert paths.profiles_root == cache_home / "box-rpg" / "profiles"

    paths.ensure()

    assert all(
        directory.is_dir()
        for directory in (
            paths.config_root,
            paths.downloads_root,
            paths.runtimes_root,
            paths.sessions_root,
            paths.reports_root,
            paths.profiles_root,
        )
    )


def test_app_paths_fall_back_to_home_and_require_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    paths = AppPaths.from_environment()

    assert paths.config_root == home / ".config" / "box-rpg"
    assert paths.cache_root == home / ".cache" / "box-rpg"

    monkeypatch.delenv("HOME")

    with pytest.raises(ConfigurationError, match="HOME is required"):
        AppPaths.from_environment()


def test_managed_runtime_path_rejects_a_cache_symlink_escape(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    outside = tmp_path / "outside"
    outside.mkdir()
    runtimes_parent = paths.cache_root / "runtimes"
    runtimes_parent.parent.mkdir(parents=True)
    runtimes_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="must not be a symlink"):
        paths.ensure_managed_runtime_path(paths.runtimes_root / "linux-x64" / "standard-v0.90.0")


def test_app_paths_reject_a_symlinked_xdg_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    cache_home = tmp_path / "xdg-cache"
    cache_home.symlink_to(target, target_is_directory=True)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=cache_home / "box-rpg")

    with pytest.raises(ConfigurationError, match="must not be a symlink"):
        paths.ensure()


def test_app_paths_reject_existing_managed_directories_with_unsafe_permissions(
    tmp_path: Path,
) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    paths.sessions_root.chmod(0o777)

    with pytest.raises(ConfigurationError, match="unsafe permissions"):
        paths.ensure()
