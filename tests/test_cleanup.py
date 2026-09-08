from pathlib import Path

import pytest

from box.cli.cleanup import execute
from box.config.repository import ConfigRepository
from box.errors import RuntimeError
from box.paths import AppPaths


def test_cleanup_removes_one_authorized_game_root(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    first = tmp_path / "games" / "first"
    second = tmp_path / "games" / "second"
    first.mkdir(parents=True)
    second.mkdir()
    repository = ConfigRepository(paths)
    repository.add_allowed_root(first)
    repository.add_allowed_root(second)
    choices = iter(("1", "1", "yes", "q"))

    assert execute(paths, repository, interactive=True, read=lambda _: next(choices)) == 0
    assert repository.load().allowed_game_roots == (second,)
    assert first.is_dir()


def test_cleanup_removes_all_download_archives_from_their_menu(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    first = paths.downloads_root / "standard-v0.90.0-linux-x64.tar.gz"
    second = paths.downloads_root / "sdk-v0.91.0-linux-x64.tar.gz.part"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    choices = iter(("3", "a", "yes", "q"))

    assert (
        execute(paths, ConfigRepository(paths), interactive=True, read=lambda _: next(choices)) == 0
    )
    assert not first.exists()
    assert not second.exists()


def test_cleanup_removes_easyrpg_managed_runtime_and_archive(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    runtime = paths.easyrpg_runtimes_root / "0.8.1.1"
    runtime.mkdir()
    player = runtime / "easyrpg-player"
    player.write_text("#!/bin/sh\n", encoding="utf-8")
    player.chmod(0o700)
    archive = paths.easyrpg_downloads_root / "easyrpg-player-0.8.1.1-linux.tar.gz"
    archive.write_bytes(b"archive")
    choices = iter(("2", "1", "yes", "3", "1", "yes", "q"))

    assert (
        execute(paths, ConfigRepository(paths), interactive=True, read=lambda _: next(choices)) == 0
    )
    assert not runtime.exists()
    assert not archive.exists()


def test_cleanup_global_all_removes_only_the_three_managed_categories(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    game_root = tmp_path / "games" / "sample"
    game_root.mkdir(parents=True)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game_root)
    paths.ensure()
    runtime = paths.runtimes_root / "linux-x64" / "standard-v0.90.0"
    runtime.mkdir(parents=True)
    archive = paths.downloads_root / "standard-v0.90.0-linux-x64.tar.gz"
    archive.write_bytes(b"archive")
    session = paths.sessions_root / "active"
    report = paths.reports_root / "latest.txt"
    session.mkdir()
    report.write_text("report", encoding="utf-8")
    choices = iter(("a", "DELETE ALL", "q"))

    assert execute(paths, repository, interactive=True, read=lambda _: next(choices)) == 0
    assert repository.load().allowed_game_roots == ()
    assert not runtime.exists()
    assert not archive.exists()
    assert game_root.is_dir()
    assert session.is_dir()
    assert report.read_text(encoding="utf-8") == "report"


def test_cleanup_rejects_non_interactive_execution(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")

    with pytest.raises(RuntimeError, match="interactive terminal"):
        execute(paths, ConfigRepository(paths), interactive=False)
