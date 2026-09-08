from pathlib import Path

import pytest

from box.cli.main import main
from box.cli.parser import build_parser
from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.diagnostics.versions import VersionReport
from box.engines.registry import EngineRegistry
from box.models import EngineName, GameInfo
from box.paths import AppPaths


def test_main_launches_the_current_directory_without_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, str | None, bool, tuple[str, ...]]] = []

    def fake_launch(
        paths: object,
        repository: object,
        game_path: Path,
        version: str | None,
        sdk: bool,
        copy_root_files: tuple[str, ...],
    ) -> int:
        calls.append((game_path, version, sdk, copy_root_files))
        return 0

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return GameInfo(
            EngineName.RPG_MAKER_MZ, tmp_path, tmp_path / "index.html", tmp_path / "package.json"
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("box.cli.main.detect_game", detect_game)
    monkeypatch.setattr("box.cli.main.launch_command.execute", fake_launch)

    assert main([]) == 0
    assert calls == [(Path("."), None, False, ())]


def test_main_without_arguments_shows_help_outside_a_game(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main([]) == 1

    captured = capsys.readouterr()
    assert "run box-rpg from the game directory" in captured.err
    assert "usage: box-rpg" in captured.out


def test_inspect_does_not_create_xdg_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game = tmp_path / "game"
    config_home = tmp_path / "config"
    cache_home = tmp_path / "cache"
    calls: list[Path] = []

    def inspect_game(game_path: Path) -> int:
        calls.append(game_path)
        return 0

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setattr("box.cli.main.inspect_command.execute", inspect_game)

    assert main(["inspect", str(game)]) == 0
    assert calls == [game]
    assert not config_home.exists()
    assert not cache_home.exists()


def test_diagnose_uses_configured_runtime_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    cache_home = tmp_path / "cache"
    paths = AppPaths(config_root=config_home / "box-rpg", cache_root=cache_home / "box-rpg")
    ConfigRepository(paths).save(AppConfig(preferred_runtime="v0.90.0", prefer_sdk=True))
    selected: list[tuple[str | None, bool]] = []

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return GameInfo(
            EngineName.RPG_MAKER_MZ, tmp_path, tmp_path / "index.html", tmp_path / "package.json"
        )

    def select_runtime(_: object, __: str, version: str | None, sdk: bool) -> object:
        selected.append((version, sdk))
        return object()

    def collect_versions(_: GameInfo, __: object) -> VersionReport:
        return VersionReport("rpg-maker-mz", None, "v0.90.0")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setattr("box.cli.diagnose.detect_game", detect_game)
    monkeypatch.setattr("box.cli.diagnose.select_runtime", select_runtime)
    monkeypatch.setattr("box.cli.diagnose.collect_versions", collect_versions)

    assert main(["diagnose", str(tmp_path)]) == 0
    assert selected == [("v0.90.0", True)]


def test_launch_command_defaults_to_the_current_directory() -> None:
    arguments = build_parser().parse_args(["launch"])

    assert arguments.game == "."
    assert arguments.copy_root_file == []


def test_launch_command_accepts_direct_game_root_file_copies() -> None:
    arguments = build_parser().parse_args(
        ["launch", "--copy-root-file", "game_messages.csv", "--copy-root-file", "mod.ini"]
    )

    assert arguments.copy_root_file == ["game_messages.csv", "mod.ini"]


def test_cleanup_command_is_available() -> None:
    arguments = build_parser().parse_args(["cleanup"])

    assert arguments.command == "cleanup"
    assert arguments.cleanup_command is None
    assert not arguments.interactive
    assert not arguments.yes


def test_cleanup_command_without_an_action_points_to_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    assert main(["cleanup"]) == 1
    assert "box-rpg cleanup --help" in capsys.readouterr().err


def test_cleanup_parser_accepts_list_remove_and_all_commands() -> None:
    listed = build_parser().parse_args(["cleanup", "list", "profiles"])
    removed = build_parser().parse_args(
        ["cleanup", "remove", "profiles", "0123456789abcdef", "--yes"]
    )
    cleared = build_parser().parse_args(["cleanup", "all", "--yes"])
    parent_yes = build_parser().parse_args(["cleanup", "--yes", "all"])

    assert (listed.cleanup_command, listed.category) == ("list", "profiles")
    assert (removed.cleanup_command, removed.category, removed.selector, removed.yes) == (
        "remove",
        "profiles",
        "0123456789abcdef",
        True,
    )
    assert (cleared.cleanup_command, cleared.yes) == ("all", True)
    assert (parent_yes.cleanup_command, parent_yes.yes) == ("all", True)


def test_cleanup_parser_rejects_conflicting_global_modes() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["cleanup", "--interactive", "--yes"])


def test_easyrpg_runtime_parser_accepts_interactive_version_selection() -> None:
    arguments = build_parser().parse_args(["runtime", "easyrpg", "available", "--interactive"])

    assert arguments.runtime_command == "easyrpg"
    assert arguments.easyrpg_command == "available"
    assert arguments.interactive


def test_nwjs_runtime_parser_accepts_all_actions() -> None:
    listed = build_parser().parse_args(["runtime", "nwjs", "list"])
    available = build_parser().parse_args(["runtime", "nwjs", "available"])
    installed = build_parser().parse_args(["runtime", "nwjs", "install", "v0.115.0"])
    removed = build_parser().parse_args(["runtime", "nwjs", "remove", "v0.115.0"])

    assert (listed.runtime_command, listed.nwjs_command) == ("nwjs", "list")
    assert (available.runtime_command, available.nwjs_command) == ("nwjs", "available")
    assert (installed.nwjs_command, installed.version) == ("install", "v0.115.0")
    assert (removed.nwjs_command, removed.version) == ("remove", "v0.115.0")


def test_nwjs_runtime_parser_rejects_legacy_direct_actions() -> None:
    for action in ("list", "available", "install", "remove"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["runtime", action])


def test_nwjs_runtime_available_defaults_to_the_detected_architecture() -> None:
    arguments = build_parser().parse_args(["runtime", "nwjs", "available"])

    assert arguments.runtime_command == "nwjs"
    assert arguments.nwjs_command == "available"
    assert arguments.page == 1
    assert not arguments.interactive
    assert arguments.architecture is None
    assert not arguments.sdk


def test_nwjs_runtime_available_uses_the_detected_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, bool, str, bool]] = []

    def list_available(
        _: AppPaths,
        page: int,
        interactive: bool,
        architecture: str,
        sdk: bool,
    ) -> int:
        calls.append((page, interactive, architecture, sdk))
        return 0

    def detect_architecture() -> str:
        return "arm64"

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("box.cli.main.current_architecture", detect_architecture)
    monkeypatch.setattr("box.cli.main.runtime_command.available", list_available)

    assert main(["runtime", "nwjs", "available"]) == 0
    assert calls == [(1, False, "arm64", False)]


def test_nwjs_runtime_available_rejects_an_unsupported_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    assert main(["runtime", "nwjs", "available", "--architecture", "invalid"]) == 1
    assert "unsupported NW.js architecture" in capsys.readouterr().err
