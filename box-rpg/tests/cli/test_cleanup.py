import json
import os
import shutil
from pathlib import Path

import pytest

# pyright: reportPrivateUsage=false
from box.api.cleanup import CleanupItem
from box.cli.cleanup import _render_cleanup_item, execute
from box.config.repository import ConfigRepository
from box.errors import RuntimeError
from box.paths import AppPaths
from box.utils.terminal import abbreviate_prompt_path


def test_cleanup_lists_stable_selectors_without_a_terminal(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    archive = paths.downloads_root / "standard-v0.90.0-linux-x64.tar.gz"
    archive.write_bytes(b"archive")
    output: list[str] = []

    assert (
        execute(
            paths,
            ConfigRepository(paths),
            command="list",
            category="downloads",
            has_tty=False,
            write=output.append,
        )
        == 0
    )
    assert [json.loads(line) for line in output] == [
        {
            "category": "downloads",
            "selector": "nwjs:standard-v0.90.0-linux-x64.tar.gz",
            "label": "standard-v0.90.0-linux-x64.tar.gz",
        }
    ]


def test_cleanup_lists_a_root_with_control_characters_as_json(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    root = tmp_path / "games\twith-newline\n"
    root.mkdir()
    repository = ConfigRepository(paths)
    repository.add_allowed_root(root)
    output: list[str] = []

    assert (
        execute(
            paths, repository, command="list", category="roots", has_tty=False, write=output.append
        )
        == 0
    )
    assert json.loads(output[0])["selector"] == str(root)


def test_cleanup_removes_one_profile_noninteractively_with_yes(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    profile = paths.profiles_root / "0123456789abcdef"
    profile.mkdir()

    assert (
        execute(
            paths,
            ConfigRepository(paths),
            command="remove",
            category="profiles",
            selector=profile.name,
            yes=True,
            has_tty=False,
        )
        == 0
    )
    assert not profile.exists()


def test_cleanup_requires_yes_for_noninteractive_removal(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    profile = paths.profiles_root / "0123456789abcdef"
    profile.mkdir()

    with pytest.raises(RuntimeError, match="requires --yes"):
        execute(
            paths,
            ConfigRepository(paths),
            command="remove",
            category="profiles",
            selector=profile.name,
            has_tty=False,
        )

    assert profile.is_dir()


def test_cleanup_removes_only_one_category_with_all(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    nwjs = paths.downloads_root / "standard-v0.90.0-linux-x64.tar.gz"
    easyrpg = paths.easyrpg_downloads_root / "easyrpg-player-0.8.1.1-linux.tar.gz"
    profile = paths.profiles_root / "0123456789abcdef"
    nwjs.write_bytes(b"archive")
    easyrpg.write_bytes(b"archive")
    profile.mkdir()

    assert (
        execute(
            paths,
            ConfigRepository(paths),
            command="remove",
            category="downloads",
            remove_all=True,
            yes=True,
            has_tty=False,
        )
        == 0
    )
    assert not nwjs.exists()
    assert not easyrpg.exists()
    assert profile.is_dir()


def test_cleanup_global_requires_confirmation_and_preserves_games_sessions_and_reports(
    tmp_path: Path,
) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    game_root = tmp_path / "games" / "sample"
    game_root.mkdir(parents=True)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game_root)
    paths.ensure()
    archive = paths.downloads_root / "standard-v0.90.0-linux-x64.tar.gz"
    profile = paths.profiles_root / "0123456789abcdef"
    session = paths.sessions_root / "active"
    report = paths.reports_root / "latest.txt"
    archive.write_bytes(b"archive")
    profile.mkdir()
    session.mkdir()
    report.write_text("report", encoding="utf-8")

    assert execute(paths, repository, command="all", has_tty=True, read=lambda _: "DELETE ALL") == 0
    assert repository.load().allowed_game_roots == ()
    assert not archive.exists()
    assert not profile.exists()
    assert game_root.is_dir()
    assert session.is_dir()
    assert report.read_text(encoding="utf-8") == "report"


def test_cleanup_interactive_mode_requires_a_terminal(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")

    with pytest.raises(RuntimeError, match="--interactive requires"):
        execute(paths, ConfigRepository(paths), interactive=True, has_tty=False)


def test_cleanup_requires_an_explicit_action(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")

    with pytest.raises(RuntimeError, match="cleanup --help"):
        execute(paths, ConfigRepository(paths), has_tty=True)


def test_cleanup_interactive_mode_can_remove_one_authorized_root(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    first = tmp_path / "games" / "first"
    second = tmp_path / "games" / "second"
    first.mkdir(parents=True)
    second.mkdir()
    repository = ConfigRepository(paths)
    repository.add_allowed_root(first)
    repository.add_allowed_root(second)
    choices = iter(("1", "1", "DELETE", "q"))

    assert (
        execute(
            paths,
            repository,
            interactive=True,
            has_tty=True,
            read=lambda _: next(choices),
        )
        == 0
    )
    assert repository.load().allowed_game_roots == (second,)


def test_cleanup_rejects_invalid_selector_and_ambiguous_remove_target(tmp_path: Path) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")

    with pytest.raises(RuntimeError, match="no profiles item"):
        execute(
            paths,
            ConfigRepository(paths),
            command="remove",
            category="profiles",
            selector="0123456789abcdef",
            yes=True,
            has_tty=False,
        )
    with pytest.raises(RuntimeError, match="exactly one"):
        execute(
            paths,
            ConfigRepository(paths),
            command="remove",
            category="profiles",
            yes=True,
            has_tty=False,
        )


def test_cleanup_interactive_root_label_abbreviates_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    root = home / "Documentos" / "Juegos" / "Linux" / "The Demon King's Reclusive Strategist"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((120, 24)))
    item = CleanupItem("roots", str(root), str(root), root)

    assert _render_cleanup_item(item) == abbreviate_prompt_path(root, 120 - len("  10. "), home)
    assert _render_cleanup_item(item).startswith("~/Documentos/")


def test_cleanup_interactive_root_label_uses_ellipsis_when_narrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    root = home / "Documentos" / "Juegos" / "Linux" / "The Demon King's Reclusive Strategist"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((40, 24)))
    item = CleanupItem("roots", str(root), str(root), root)

    rendered = _render_cleanup_item(item)
    assert rendered == abbreviate_prompt_path(root, 40 - len("  10. "), home)
    assert "Documentos" not in rendered


def test_cleanup_interactive_root_display_keeps_absolute_selector_and_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    root = home / "Documentos" / "Juegos" / "Linux" / "The Demon King's Reclusive Strategist"
    root.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((120, 24)))
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(root)
    output: list[str] = []
    choices = iter(("1", "q", "q"))

    assert (
        execute(
            paths,
            repository,
            interactive=True,
            has_tty=True,
            read=lambda _: next(choices),
            write=output.append,
        )
        == 0
    )
    assert any("~/Documentos/" in line for line in output)
    assert not any(str(root) in line for line in output if line.startswith("  1. "))
    assert repository.load().allowed_game_roots == (root,)

    listing: list[str] = []
    assert (
        execute(
            paths, repository, command="list", category="roots", has_tty=False, write=listing.append
        )
        == 0
    )
    assert json.loads(listing[0])["selector"] == str(root)


def test_cleanup_interactive_root_label_escapes_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "game\x1b[31m\n"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((120, 24)))
    item = CleanupItem("roots", str(root), str(root), root)

    rendered = _render_cleanup_item(item)
    assert "\x1b" not in rendered
    assert "\n" not in rendered
    assert r"\x1b" in rendered
