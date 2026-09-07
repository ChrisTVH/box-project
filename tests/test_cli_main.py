from pathlib import Path

import pytest

from box.cli.main import main
from box.cli.parser import build_parser


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

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("box.cli.main.launch_command.execute", fake_launch)

    assert main([]) == 0
    assert calls == [(Path("."), None, False)]


def test_launch_command_defaults_to_the_current_directory() -> None:
    arguments = build_parser().parse_args(["launch"])

    assert arguments.game == "."
