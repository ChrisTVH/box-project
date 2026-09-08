from pathlib import Path

import pytest

from box.cli.main import main
from box.cli.parser import build_parser
from box.engines.registry import EngineRegistry
from box.models import EngineName, GameInfo
from box.paths import AppPaths


def test_main_launches_the_current_directory_without_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, str | None, bool]] = []

    def fake_launch(
        paths: object,
        repository: object,
        game_path: Path,
        version: str | None,
        sdk: bool,
    ) -> int:
        calls.append((game_path, version, sdk))
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
    assert calls == [(Path("."), None, False)]


def test_main_without_arguments_shows_help_outside_a_game(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main([]) == 1

    captured = capsys.readouterr()
    assert "run box-rpg from the game directory" in captured.err
    assert "usage: box-rpg" in captured.out


def test_launch_command_defaults_to_the_current_directory() -> None:
    arguments = build_parser().parse_args(["launch"])

    assert arguments.game == "."


def test_cleanup_command_is_available() -> None:
    arguments = build_parser().parse_args(["cleanup"])

    assert arguments.command == "cleanup"


def test_cleanup_command_requires_an_interactive_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    assert main(["cleanup"]) == 1
    assert "cleanup requires an interactive terminal" in capsys.readouterr().err


def test_runtime_available_defaults_to_the_detected_architecture() -> None:
    arguments = build_parser().parse_args(["runtime", "available"])

    assert arguments.page == 1
    assert not arguments.interactive
    assert arguments.architecture is None
    assert not arguments.sdk


def test_runtime_available_uses_the_detected_architecture(
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

    assert main(["runtime", "available"]) == 0
    assert calls == [(1, False, "arm64", False)]


def test_runtime_available_rejects_an_unsupported_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    assert main(["runtime", "available", "--architecture", "invalid"]) == 1
    assert "unsupported NW.js architecture" in capsys.readouterr().err
