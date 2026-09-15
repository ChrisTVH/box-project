"""Tests for the interaction, inspect, and diagnose APIs."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from box.api import diagnose as api_diagnose
from box.api import inspect as api_inspect
from box.api import interaction as api_interaction
from box.api.diagnose import DiagnoseResult
from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.diagnostics.environment import Environment
from box.diagnostics.versions import VersionReport
from box.engines.registry import EngineRegistry
from box.errors import GameValidationError
from box.games.inspector import Inspection
from box.models import EngineName, GameInfo, RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime.easyrpg import EasyRPGRuntime


def _paths(tmp_path: Path) -> AppPaths:
    """Build isolated launcher paths inside a temporary directory."""
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


def test_inspect_returns_delegated_result_without_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root)
    expected = Inspection(game=game, title="Sample", plugin_count=3)
    seen: dict[str, object] = {}

    def fake_inspect(path: Path, registry: EngineRegistry | None = None) -> Inspection:
        seen["path"] = path
        seen["registry"] = registry
        return expected

    def forbidden_print(*args: object, **kwargs: object) -> None:
        raise AssertionError("inspect API must not print")

    def forbidden_input(*args: object, **kwargs: object) -> str:
        raise AssertionError("inspect API must not read")

    monkeypatch.setattr(api_inspect, "inspect_game", fake_inspect)
    monkeypatch.setattr("builtins.print", forbidden_print)
    monkeypatch.setattr("builtins.input", forbidden_input)

    result = api_inspect.inspect(game_root)

    assert result == expected
    assert seen == {"path": game_root, "registry": None}
    assert capsys.readouterr() == ("", "")


def test_inspect_forwards_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from box.engines.registry import default_registry

    registry = default_registry()
    game = GameInfo(EngineName.RPG_MAKER_MZ, tmp_path)
    expected = Inspection(game=game, title=None, plugin_count=0)
    seen: dict[str, object] = {}

    def fake_inspect(path: Path, registry: EngineRegistry | None = None) -> Inspection:
        seen["registry"] = registry
        return expected

    monkeypatch.setattr(api_inspect, "inspect_game", fake_inspect)

    assert api_inspect.inspect(tmp_path, registry) == expected
    assert seen["registry"] is registry


def test_diagnose_easyrpg_explicit_version_without_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    seen: dict[str, str] = {}
    environment = Environment("Linux", "1.0", "x86_64")

    class FakeCatalog:
        def __init__(self, actual_paths: AppPaths) -> None:
            assert actual_paths == paths

        def get(self, version: str) -> EasyRPGRuntime:
            seen["version"] = version
            return EasyRPGRuntime(version, tmp_path)

        def latest(self) -> EasyRPGRuntime:
            raise AssertionError("explicit version must be used")

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        assert path == game_root
        return game

    def fake_environment() -> Environment:
        return environment

    def fake_versions(actual_game: GameInfo, runtime: EasyRPGRuntime) -> VersionReport:
        assert actual_game == game
        return VersionReport("rpg-maker-2000-2003", None, None, runtime.version)

    def forbidden_print(*args: object, **kwargs: object) -> None:
        raise AssertionError("diagnose API must not print")

    def forbidden_input(*args: object, **kwargs: object) -> str:
        raise AssertionError("diagnose API must not read")

    monkeypatch.setattr(api_diagnose, "detect_game", fake_detect)
    monkeypatch.setattr(api_diagnose, "EasyRPGCatalog", FakeCatalog)
    monkeypatch.setattr(api_diagnose, "collect_environment", fake_environment)
    monkeypatch.setattr(api_diagnose, "collect_easyrpg_versions", fake_versions)
    monkeypatch.setattr("builtins.print", forbidden_print)
    monkeypatch.setattr("builtins.input", forbidden_input)

    result = api_diagnose.diagnose(paths, repository, game_root, "0.8", False)

    assert isinstance(result, DiagnoseResult)
    assert result.environment == environment
    assert result.versions == VersionReport("rpg-maker-2000-2003", None, None, "0.8")
    assert seen == {"version": "0.8"}
    assert capsys.readouterr() == ("", "")


def test_diagnose_easyrpg_latest_without_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    latest = EasyRPGRuntime("0.8.1", tmp_path)

    class FakeCatalog:
        def __init__(self, actual_paths: AppPaths) -> None:
            pass

        def get(self, version: str) -> EasyRPGRuntime:
            raise AssertionError("latest must be used without a version")

        def latest(self) -> EasyRPGRuntime:
            return latest

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    monkeypatch.setattr(api_diagnose, "detect_game", fake_detect)
    monkeypatch.setattr(api_diagnose, "EasyRPGCatalog", FakeCatalog)
    monkeypatch.setattr(
        api_diagnose, "collect_environment", lambda: Environment("Linux", "1.0", "x86_64")
    )

    def fake_easyrpg_versions(actual_game: GameInfo, runtime: EasyRPGRuntime) -> VersionReport:
        assert actual_game == game
        return VersionReport("rpg-maker-2000-2003", None, None, runtime.version)

    monkeypatch.setattr(api_diagnose, "collect_easyrpg_versions", fake_easyrpg_versions)

    result = api_diagnose.diagnose(paths, repository, game_root, None, False)

    assert result.versions.easyrpg_player == "0.8.1"


def test_diagnose_easyrpg_rejects_sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    monkeypatch.setattr(api_diagnose, "detect_game", fake_detect)

    with pytest.raises(GameValidationError, match="--sdk"):
        api_diagnose.diagnose(paths, repository, game_root, None, True)


def test_diagnose_nwjs_uses_config_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.save(AppConfig(preferred_runtime="v0.90.0", prefer_sdk=True))
    runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), tmp_path, tmp_path / "nw")
    environment = Environment("Linux", "1.0", "x86_64")
    selected: dict[str, object] = {}

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def fake_architecture() -> str:
        return "x64"

    def fake_select(
        catalog: object, architecture: str, version: str | None, sdk: bool
    ) -> RuntimeInfo:
        selected["architecture"] = architecture
        selected["version"] = version
        selected["sdk"] = sdk
        return runtime

    def fake_environment() -> Environment:
        return environment

    def fake_versions(actual_game: GameInfo, actual_runtime: RuntimeInfo) -> VersionReport:
        assert actual_game == game
        assert actual_runtime == runtime
        return VersionReport("rpg-maker-mz", "1.7.0", "v0.90.0")

    def forbidden_print(*args: object, **kwargs: object) -> None:
        raise AssertionError("diagnose API must not print")

    monkeypatch.setattr(api_diagnose, "detect_game", fake_detect)
    monkeypatch.setattr(api_diagnose, "current_architecture", fake_architecture)
    monkeypatch.setattr(api_diagnose, "select_runtime", fake_select)
    monkeypatch.setattr(api_diagnose, "collect_environment", fake_environment)
    monkeypatch.setattr(api_diagnose, "collect_versions", fake_versions)
    monkeypatch.setattr("builtins.print", forbidden_print)

    result = api_diagnose.diagnose(paths, repository, game_root, None, False)

    assert selected == {"architecture": "x64", "version": "v0.90.0", "sdk": True}
    assert result == DiagnoseResult(environment, VersionReport("rpg-maker-mz", "1.7.0", "v0.90.0"))
    assert capsys.readouterr() == ("", "")


def test_diagnose_nwjs_explicit_version_overrides_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_MZ, game_root)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.save(AppConfig(preferred_runtime="v0.90.0", prefer_sdk=True))
    runtime = RuntimeInfo(RuntimeSpec("v0.112.0", "x64"), tmp_path, tmp_path / "nw")
    selected: dict[str, object] = {}

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def fake_select(
        catalog: object, architecture: str, version: str | None, sdk: bool
    ) -> RuntimeInfo:
        selected["version"] = version
        selected["sdk"] = sdk
        return runtime

    monkeypatch.setattr(api_diagnose, "detect_game", fake_detect)
    monkeypatch.setattr(api_diagnose, "current_architecture", lambda: "x64")
    monkeypatch.setattr(api_diagnose, "select_runtime", fake_select)
    monkeypatch.setattr(
        api_diagnose, "collect_environment", lambda: Environment("Linux", "1.0", "x86_64")
    )

    def fake_nwjs_versions(actual_game: GameInfo, actual_runtime: RuntimeInfo) -> VersionReport:
        assert actual_game == game
        assert actual_runtime == runtime
        return VersionReport("rpg-maker-mz", None, "v0.112.0")

    monkeypatch.setattr(api_diagnose, "collect_versions", fake_nwjs_versions)

    result = api_diagnose.diagnose(paths, repository, game_root, "v0.112.0", False)

    assert selected == {"version": "v0.112.0", "sdk": True}
    assert result.versions.nwjs == "v0.112.0"


def test_diagnose_result_is_frozen() -> None:
    result = DiagnoseResult(
        Environment("Linux", "1.0", "x86_64"),
        VersionReport("rpg-maker-mz", None, "v0.112.0"),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.environment = Environment(  # type: ignore[reportAttributeAccessIssue]
            "Linux", "2.0", "x86_64"
        )


def test_confirm_add_root_returns_true_and_sanitizes_prompt() -> None:
    prompts: list[str] = []
    raw = Path("/tmp/game\x1b[31m")

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return "yes"

    interaction = api_interaction.ConsoleInteraction(read=read, write=lambda _: None)

    assert interaction.confirm_add_root(raw) is True
    assert len(prompts) == 1
    assert "\x1b" not in prompts[0]
    assert "\\x1b" in prompts[0]


def test_confirm_add_root_denial_returns_false() -> None:
    interaction = api_interaction.ConsoleInteraction(read=lambda _: "no", write=lambda _: None)

    assert interaction.confirm_add_root(Path("/tmp/game")) is False


def test_confirm_add_root_eof_raises_authorization_error() -> None:
    def missing(prompt: str) -> str:
        raise EOFError

    interaction = api_interaction.ConsoleInteraction(read=missing, write=lambda _: None)

    with pytest.raises(GameValidationError, match="was not authorized"):
        interaction.confirm_add_root(Path("/tmp/game"))


def test_confirm_x11_warns_on_stderr_and_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    errors: list[str] = []
    interaction = api_interaction.ConsoleInteraction(
        read=lambda _: "yes", write=lambda _: None, write_error=errors.append
    )

    assert interaction.confirm_x11(":0") is True
    assert len(errors) == 1
    assert ":0" in errors[0]


def test_confirm_x11_sanitizes_display() -> None:
    errors: list[str] = []
    interaction = api_interaction.ConsoleInteraction(
        read=lambda _: "yes", write=lambda _: None, write_error=errors.append
    )

    assert interaction.confirm_x11(":0\x1b[31m\n") is True
    assert "\x1b" not in errors[0]
    assert "\n" not in errors[0]


@pytest.mark.parametrize("answer", ["n", "no", ""])
def test_confirm_x11_denial_returns_false(answer: str) -> None:
    interaction = api_interaction.ConsoleInteraction(
        read=lambda _: answer, write=lambda _: None, write_error=lambda _: None
    )

    assert interaction.confirm_x11(":0") is False


def test_confirm_x11_eof_raises() -> None:
    def missing(prompt: str) -> str:
        raise EOFError

    interaction = api_interaction.ConsoleInteraction(
        read=missing, write=lambda _: None, write_error=lambda _: None
    )

    with pytest.raises(GameValidationError, match="was not confirmed"):
        interaction.confirm_x11(":0")


@pytest.mark.parametrize(
    ("answers", "expected"),
    [(["2"], 1), ([""], 0), (["9", "x", "1"], 0)],
)
def test_choose_runtime_returns_index(answers: list[str], expected: int) -> None:
    remaining = list(answers)
    written: list[str] = []

    def read(prompt: str) -> str:
        assert remaining, f"unexpected prompt: {prompt}"
        return remaining.pop(0)

    interaction = api_interaction.ConsoleInteraction(read=read, write=written.append)

    assert interaction.choose_runtime("nwjs", ("v0.115.0", "v0.112.0"), "title") == expected
    assert written[0] == "title"
    assert written[1] == "  1. v0.115.0"
    assert written[2] == "  2. v0.112.0"
    if len(answers) > 1:
        assert "Invalid selection." in written


@pytest.mark.parametrize("answer", ["q", "Q"])
def test_choose_runtime_quit_returns_none(answer: str) -> None:
    interaction = api_interaction.ConsoleInteraction(read=lambda _: answer, write=lambda _: None)

    assert interaction.choose_runtime("easyrpg", ("0.8.1", "0.8.0"), "title") is None


def test_choose_runtime_eof_raises() -> None:
    def missing(prompt: str) -> str:
        raise EOFError

    interaction = api_interaction.ConsoleInteraction(read=missing, write=lambda _: None)

    with pytest.raises(GameValidationError, match="cancelled"):
        interaction.choose_runtime("nwjs", ("v0.115.0",), "title")


def test_choose_runtime_rejects_empty_candidates() -> None:
    interaction = api_interaction.ConsoleInteraction(read=lambda _: "", write=lambda _: None)

    with pytest.raises(GameValidationError, match="cancelled"):
        interaction.choose_runtime("nwjs", (), "title")
