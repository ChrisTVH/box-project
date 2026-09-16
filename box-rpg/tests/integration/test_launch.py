from collections.abc import Callable
from pathlib import Path

import pytest

# pyright: reportPrivateUsage=false
from box.cli.launch import _abbreviate_prompt_path, _setup_desktop, authorize_game, execute
from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.engines.registry import EngineRegistry
from box.errors import GameValidationError, LaunchError, RuntimeError
from box.launch.sandbox import Sandbox
from box.launch.supervisor import LaunchedSession
from box.models import EngineName, GameInfo, RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime.easyrpg import EasyRPGRuntime
from box.runtime.platform import current_architecture


def _patch_run_as_detached(
    monkeypatch: pytest.MonkeyPatch,
    run: Callable[..., int],
    poll_code: int | None = None,
) -> None:
    """Adapt a legacy blocking run mock to the detached supervisor contract.

    Calls the legacy run(command, cwd=None, pass_fds) for its assertions and
    captures, returns a fake handle from spawn, and makes the CLI foreground
    poll return the same exit code.
    """

    codes: list[int] = []

    def fake_spawn(
        paths: AppPaths,
        identifier: str,
        name: str,
        command: list[str],
        pass_fds: tuple[int, ...] = (),
        *,
        parent_descriptor: int,
        session_descriptor: int,
        use_gamemode: bool = False,
        gamemode_proxy: Path | None = None,
    ) -> LaunchedSession:
        code = run(command, None, pass_fds)
        codes.append(code)
        return LaunchedSession(identifier, name, paths.sessions_root / identifier / name)

    def fake_poll(paths: AppPaths, identifier: str, name: str) -> int | None:
        if poll_code is not None:
            return poll_code
        return codes[0] if codes else 0

    monkeypatch.setattr("box.api.launch.spawn_detached", fake_spawn)
    monkeypatch.setattr("box.cli.launch.api_poll_status", fake_poll)


def test_authorize_game_registers_the_detected_game_root_after_confirmation(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "games" / "sample"
    game_root.mkdir(parents=True)
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    config = authorize_game(game, repository.load(), repository, read=lambda _: "yes")

    assert config.allowed_game_roots == (game_root,)
    assert repository.load() == config


def test_authorize_game_rejects_root_moved_outside_during_prompt(tmp_path: Path) -> None:
    game_root = tmp_path / "games" / "sample"
    game_root.mkdir(parents=True)
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)

    def confirm(_: str) -> str:
        game_root.rename(tmp_path / "outside")
        game_root.mkdir()
        return "yes"

    with pytest.raises(GameValidationError, match="changed since detection"):
        authorize_game(game, repository.load(), repository, read=confirm)
    assert repository.load().allowed_game_roots == ()


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_authorization_revalidates_after_persistence_load_before_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    original = AppConfig(preferred_runtime="v0.90.0")
    repository.save(original)
    stored = paths.config_file.read_bytes()
    real_load = repository.load
    confirmed = False

    def confirm(_: str) -> str:
        nonlocal confirmed
        confirmed = True
        return "yes"

    def racing_load() -> AppConfig:
        config = real_load()
        if confirmed:
            game_root.rename(tmp_path / "original")
            if replacement == "symlink":
                game_root.symlink_to(Path("/"), target_is_directory=True)
            else:
                game_root.mkdir()
        return config

    monkeypatch.setattr(repository, "load", racing_load)
    with pytest.raises(GameValidationError):
        authorize_game(game, original, repository, read=confirm)

    assert real_load() == original
    assert paths.config_file.read_bytes() == stored


def test_authorization_escapes_prompt_controls_without_changing_stored_path(tmp_path: Path) -> None:
    game_root = tmp_path / "game\x1b[31m\n\t\x9b\u202e"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    prompts: list[str] = []

    def confirm(prompt: str) -> str:
        prompts.append(prompt)
        return "yes"

    config = authorize_game(game, repository.load(), repository, read=confirm)

    assert len(prompts) == 1
    assert r"game\x1b[31m\x0a\x09\x9b\u202e" in prompts[0]
    assert all(control not in prompts[0] for control in ("\x1b", "\n", "\t", "\x9b", "\u202e"))
    assert config.allowed_game_roots == (game_root,)
    assert repository.load().allowed_game_roots == (game_root,)


def test_prompt_path_abbreviation_ladder(tmp_path: Path) -> None:
    home = tmp_path / "home"
    game = home / "Documentos" / "Juegos" / "Linux" / "The Demon King's Reclusive Strategist"
    full = "~/Documentos/Juegos/Linux/The Demon King's Reclusive Strategist"
    assert _abbreviate_prompt_path(game, 80, home) == full
    assert (
        _abbreviate_prompt_path(game, 45, home) == "~/D/J/L/The Demon King's Reclusive Strategist"
    )
    assert _abbreviate_prompt_path(game, 44, home) == "…D/J/L/The Demon King's Reclusive Strategist"
    assert _abbreviate_prompt_path(game, 43, home) == "…/J/L/The Demon King's Reclusive Strategist"
    assert _abbreviate_prompt_path(game, 41, home) == "…/L/The Demon King's Reclusive Strategist"
    assert _abbreviate_prompt_path(game, 39, home) == "…/The Demon King's Reclusive Strategist"
    assert _abbreviate_prompt_path(game, 30, home) == "…/The Demon King's Reclusive S"
    assert _abbreviate_prompt_path(home, 80, home) == "~"


def test_prompt_path_outside_home_uses_absolute_ladder(tmp_path: Path) -> None:
    home = tmp_path / "home"
    game = tmp_path / "mnt" / "games" / "Xyz Quest"
    assert _abbreviate_prompt_path(game, 400, home) == game.as_posix()
    assert _abbreviate_prompt_path(game, 12, home) == "…/Xyz Quest"


@pytest.mark.parametrize("engine", [EngineName.RPG_MAKER_MZ, EngineName.RPG_MAKER_2000_2003])
def test_authorization_prompt_abbreviates_home_and_stores_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: EngineName
) -> None:
    home = tmp_path / "home"
    game_root = home / "Documentos" / "Juegos" / "Linux" / "The Demon King's Reclusive Strategist"
    game_root.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("COLUMNS", "60")
    game = GameInfo(engine, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    prompts: list[str] = []

    def confirm(prompt: str) -> str:
        prompts.append(prompt)
        return "yes"

    config = authorize_game(game, repository.load(), repository, read=confirm)

    assert len(prompts) == 1
    assert "…/The Demon King's Reclusi" in prompts[0]
    assert len(prompts[0]) <= 60
    assert "Documentos" not in prompts[0]
    assert config.allowed_game_roots == (game_root,)
    assert repository.load().allowed_game_roots == (game_root,)


def test_authorize_game_requires_confirmation_for_an_unregistered_game(tmp_path: Path) -> None:
    game_root = tmp_path / "games" / "sample"
    other_root = tmp_path / "games" / "other"
    game_root.mkdir(parents=True)
    other_root.mkdir()
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(other_root)

    with pytest.raises(GameValidationError, match="was not authorized"):
        authorize_game(game, repository.load(), repository, read=lambda _: "no")

    assert repository.load().allowed_game_roots == (other_root,)


def test_authorize_game_rejects_unregistered_games_without_an_interactive_reader(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "games" / "sample"
    other_root = tmp_path / "games" / "other"
    game_root.mkdir(parents=True)
    other_root.mkdir()
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(other_root)

    with pytest.raises(GameValidationError, match="outside configured roots"):
        authorize_game(game, repository.load(), repository)


def test_authorize_game_prunes_a_deleted_root_before_adding_a_new_game(tmp_path: Path) -> None:
    deleted_root = tmp_path / "games" / "deleted"
    game_root = tmp_path / "games" / "new"
    deleted_root.mkdir(parents=True)
    game_root.mkdir()
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(deleted_root)
    deleted_root.rmdir()

    config = authorize_game(game, repository.load(), repository, read=lambda _: "yes")

    assert config.allowed_game_roots == (game_root,)
    assert repository.load() == config


def test_authorize_game_prunes_deleted_roots_before_validating_an_allowed_game(
    tmp_path: Path,
) -> None:
    deleted_root = tmp_path / "games" / "deleted"
    game_root = tmp_path / "games" / "allowed"
    deleted_root.mkdir(parents=True)
    game_root.mkdir()
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.save(AppConfig(allowed_game_roots=(deleted_root, game_root)))
    deleted_root.rmdir()

    config = authorize_game(game, repository.load(), repository)

    assert config.allowed_game_roots == (game_root,)
    assert repository.load() == config


def test_authorize_game_rejects_a_registered_root_replaced_by_a_symlink(tmp_path: Path) -> None:
    game_root = tmp_path / "games" / "sample"
    game_root.mkdir(parents=True)
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    authorize_game(game, repository.load(), repository, read=lambda _: "yes")
    outside = tmp_path / "outside"
    outside.mkdir()
    game_root.rmdir()
    game_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(GameValidationError, match="contains a symlink"):
        authorize_game(game, repository.load(), repository)


def test_execute_does_not_register_a_game_when_no_runtime_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "games" / "sample"
    game_root.mkdir(parents=True)
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    monkeypatch.setattr("box.api.launch.detect_game", detect_game)

    with pytest.raises(RuntimeError, match=r"box-rpg runtime nwjs available --interactive"):
        execute(paths, repository, game_root, None, False)

    assert repository.load().allowed_game_roots == ()


@pytest.mark.parametrize("allow_network", [False, True])
def test_execute_launches_rpg_rt_projects_with_easyrpg_fullscreen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_network: bool
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    executable = runtime_root / "easyrpg-player"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    calls: list[tuple[list[str], Path | None]] = []

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    def latest(_: object) -> EasyRPGRuntime:
        return EasyRPGRuntime("0.8.1.1", runtime_root)

    def run(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
        calls.append((command, cwd))
        return 0

    monkeypatch.setattr("box.api.launch.detect_game", detect_game)
    monkeypatch.setattr("box.api.launch.EasyRPGCatalog.latest", latest)

    def authorize(
        _: GameInfo,
        __: AppConfig,
        ___: ConfigRepository,
        ____: Callable[[str], str] | None = None,
    ) -> AppConfig:
        return repository.load()

    monkeypatch.setattr("box.api.launch.authorize_game", authorize)
    _patch_run_as_detached(monkeypatch, run)

    def desktop(_: Sandbox) -> None:
        pass

    def probe(_: Sandbox) -> str:
        return "wayland"

    def devices(_: Sandbox) -> None:
        pass

    def audio(_: Sandbox) -> None:
        pass

    monkeypatch.setattr(Sandbox, "desktop", desktop)
    monkeypatch.setattr(Sandbox, "display_probe", probe)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    monkeypatch.delenv("DISPLAY", raising=False)

    assert execute(paths, repository, game_root, None, False, allow_network=allow_network) == 0
    command = calls[0][0]
    assert command[0] == "/usr/bin/bwrap"
    assert command[-6:] == [
        "/runtime/easyrpg-player",
        "--project-path",
        "/game",
        "--fullscreen",
        "--save-path",
        "/game/save",
    ]
    assert ("--unshare-net" not in command) == allow_network
    assert calls[0][1] is None


@pytest.mark.parametrize("allow_network", [False, True])
def test_execute_passes_network_policy_to_nwjs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_network: bool
) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "index.html").write_text("fixture")
    (root / "package.json").write_text('{"name": "fixture"}')
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, root / "index.html", root / "package.json")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    binary = runtime_root / "nw"
    binary.write_text("fixture")
    runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), runtime_root, binary)
    paths = AppPaths(tmp_path / "config", tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(root)

    def detect(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    def select(*_: object) -> RuntimeInfo:
        return runtime

    def desktop(_: Sandbox) -> None:
        pass

    def probe(_: Sandbox) -> str:
        return "wayland"

    def devices(_: Sandbox) -> None:
        pass

    def audio(_: Sandbox) -> None:
        pass

    def run(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
        assert command[0] == "/usr/bin/bwrap"
        assert "/runtime/nw" in command
        assert ("--unshare-net" not in command) == allow_network
        assert pass_fds
        return 0

    monkeypatch.setattr("box.api.launch.detect_game", detect)
    monkeypatch.setattr("box.api.launch.select_runtime", select)
    monkeypatch.setattr(Sandbox, "desktop", desktop)
    monkeypatch.setattr(Sandbox, "display_probe", probe)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    _patch_run_as_detached(monkeypatch, run)
    assert execute(paths, repository, root, None, False, allow_network=allow_network) == 0


def test_execute_rejects_nwjs_options_for_rpg_rt_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    monkeypatch.setattr("box.api.launch.detect_game", detect_game)

    with pytest.raises(GameValidationError, match="--copy-root-file"):
        execute(paths, repository, game_root, None, False, copy_root_files=("messages.csv",))


def test_execute_rejects_relocation_during_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    (game_root / "index.html").write_text("original", encoding="utf-8")
    (game_root / "package.json").write_text('{"name": "Original"}', encoding="utf-8")
    game = GameInfo(
        EngineName.RPG_MAKER_MZ,
        game_root,
        game_root / "index.html",
        game_root / "package.json",
    )
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), runtime_root, runtime_root / "nw")
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)

    def authorize(
        _: GameInfo,
        __: AppConfig,
        ___: ConfigRepository,
        ____: Callable[[str], str] | None = None,
    ) -> AppConfig:
        game_root.rename(tmp_path / "original")
        game_root.mkdir()
        (game_root / "index.html").write_text("replacement", encoding="utf-8")
        return repository.load()

    def run(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
        pytest.fail("a relocated game must not launch")

    def detect(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    def select(*_: object) -> RuntimeInfo:
        return runtime

    monkeypatch.setattr("box.api.launch.detect_game", detect)
    monkeypatch.setattr("box.api.launch.select_runtime", select)
    monkeypatch.setattr("box.api.launch.authorize_game", authorize)
    _patch_run_as_detached(monkeypatch, run)

    with pytest.raises(GameValidationError, match="changed since detection"):
        execute(paths, repository, game_root, None, False)


class _FakeDisplaySandbox(Sandbox):
    """Minimal display double for _setup_desktop tests."""

    def __init__(self, probe: str) -> None:
        super().__init__()
        self._probe = probe
        self.calls: list[str] = []

    def display_probe(self) -> str:
        return self._probe

    def desktop(self) -> None:
        self.calls.append("desktop")

    def x11(self) -> None:
        self.calls.append("x11")


def test_setup_desktop_wayland_never_prompts() -> None:
    def forbidden(prompt: str) -> str:
        raise AssertionError("wayland must not prompt")

    sandbox = _FakeDisplaySandbox("wayland")

    assert _setup_desktop(sandbox, forbidden) == "wayland"
    assert sandbox.calls == ["desktop"]


def test_setup_desktop_wayland_without_reader() -> None:
    sandbox = _FakeDisplaySandbox("wayland")

    assert _setup_desktop(sandbox, None) == "wayland"
    assert sandbox.calls == ["desktop"]


@pytest.mark.parametrize("answer", ["y", "Y", "yes", " YES "])
def test_setup_desktop_x11_accepts_confirmation(
    answer: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    sandbox = _FakeDisplaySandbox("x11")

    def confirm(prompt: str) -> str:
        return answer

    assert _setup_desktop(sandbox, confirm) == "x11"
    assert sandbox.calls == ["x11"]
    captured = capsys.readouterr()
    assert "Continue with X11?" not in captured.out
    assert ":0" in captured.err
    assert "keylog" in captured.err.lower() or "capture" in captured.err.lower()


@pytest.mark.parametrize("answer", ["n", "no", "", "maybe"])
def test_setup_desktop_x11_declines_without_consent(answer: str) -> None:
    sandbox = _FakeDisplaySandbox("x11")

    def decline(prompt: str) -> str:
        return answer

    with pytest.raises(GameValidationError):
        _setup_desktop(sandbox, decline)
    assert sandbox.calls == []


def test_setup_desktop_x11_rejects_end_of_input() -> None:
    def missing(prompt: str) -> str:
        raise EOFError

    sandbox = _FakeDisplaySandbox("x11")

    with pytest.raises(GameValidationError):
        _setup_desktop(sandbox, missing)
    assert sandbox.calls == []


def test_setup_desktop_x11_requires_interactive_consent() -> None:
    sandbox = _FakeDisplaySandbox("x11")

    with pytest.raises(GameValidationError, match="consent"):
        _setup_desktop(sandbox, None)
    assert sandbox.calls == []


def test_setup_desktop_x11_sanitizes_display(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISPLAY", ":0\x1b[31m\n")
    sandbox = _FakeDisplaySandbox("x11")

    def confirm(prompt: str) -> str:
        return "yes"

    assert _setup_desktop(sandbox, confirm) == "x11"
    captured = capsys.readouterr()
    assert "\x1b" not in captured.err
    assert "\n" not in captured.err.strip().splitlines()[0] or "\\x" in captured.err
    assert "keylog" in captured.err.lower() or "capture" in captured.err.lower()


@pytest.mark.parametrize("answer", ["y", "yes"])
def test_setup_desktop_extra_x11_prompts_on_wayland(
    answer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    sandbox = _FakeDisplaySandbox("wayland")

    def confirm(prompt: str) -> str:
        return answer

    assert _setup_desktop(sandbox, confirm, extra_x11=True) == "wayland"
    assert sandbox.calls == ["desktop", "x11"]


@pytest.mark.parametrize("answer", ["n", "no", ""])
def test_setup_desktop_extra_x11_decline_aborts(
    answer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    sandbox = _FakeDisplaySandbox("wayland")

    def decline(prompt: str) -> str:
        return answer

    with pytest.raises(GameValidationError):
        _setup_desktop(sandbox, decline, extra_x11=True)
    assert sandbox.calls == ["desktop"]


def test_setup_desktop_extra_x11_requires_interactive_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    sandbox = _FakeDisplaySandbox("wayland")

    with pytest.raises(GameValidationError, match="consent"):
        _setup_desktop(sandbox, None, extra_x11=True)
    assert sandbox.calls == ["desktop"]


def test_setup_desktop_extra_x11_skipped_without_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    sandbox = _FakeDisplaySandbox("wayland")

    def forbidden(prompt: str) -> str:
        raise AssertionError("no X11 available, must not prompt")

    assert _setup_desktop(sandbox, forbidden, extra_x11=True) == "wayland"
    assert sandbox.calls == ["desktop"]


def test_setup_desktop_ignores_x11_without_extra_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    sandbox = _FakeDisplaySandbox("wayland")

    def forbidden(prompt: str) -> str:
        raise AssertionError("extra_x11 not requested, must not prompt")

    assert _setup_desktop(sandbox, forbidden) == "wayland"
    assert sandbox.calls == ["desktop"]


def test_setup_desktop_forced_x11_needs_no_prompt() -> None:
    sandbox = _FakeDisplaySandbox("wayland")

    def forbidden(prompt: str) -> str:
        raise AssertionError("explicit --x11 is consent, must not prompt")

    assert _setup_desktop(sandbox, None, force_x11=True) == "x11"
    assert _setup_desktop(sandbox, forbidden, force_x11=True) == "x11"
    assert sandbox.calls == ["x11", "x11"]


def test_setup_desktop_without_display_preserves_desktop_error() -> None:
    class _FailingSandbox(_FakeDisplaySandbox):
        def desktop(self) -> None:
            self.calls.append("desktop")
            raise LaunchError("a local Wayland socket is required")

    sandbox = _FailingSandbox("none")

    def confirm(prompt: str) -> str:
        return "yes"

    with pytest.raises(LaunchError):
        _setup_desktop(sandbox, confirm)
    assert sandbox.calls == ["desktop"]


def _prepare_easyrpg_game(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AppPaths, ConfigRepository, GameInfo]:
    game_root = tmp_path / "game"
    game_root.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    executable = runtime_root / "easyrpg-player"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)

    def detect_game(game_path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def latest(catalog: object) -> EasyRPGRuntime:
        return EasyRPGRuntime("0.8.1.1", runtime_root)

    def authorize(
        authorized_game: GameInfo,
        config: AppConfig,
        authorized_repository: ConfigRepository,
        read: Callable[[str], str] | None = None,
    ) -> AppConfig:
        return repository.load()

    def probe(sandbox: Sandbox) -> str:
        return "wayland"

    def desktop(sandbox: Sandbox) -> None:
        pass

    monkeypatch.setattr("box.api.launch.detect_game", detect_game)
    monkeypatch.setattr("box.api.launch.EasyRPGCatalog.latest", latest)
    monkeypatch.setattr("box.api.launch.authorize_game", authorize)
    monkeypatch.setattr(Sandbox, "display_probe", probe)
    monkeypatch.setattr(Sandbox, "desktop", desktop)
    # Hermetic display: the extra-X11 consent reads the real DISPLAY.
    monkeypatch.delenv("DISPLAY", raising=False)
    return paths, repository, game


@pytest.mark.parametrize("allow_game_writes", [False, True])
def test_execute_forwards_sandbox_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_game_writes: bool
) -> None:
    paths, repository, game = _prepare_easyrpg_game(tmp_path, monkeypatch)
    seen: dict[str, bool] = {}
    real_init = Sandbox.__init__

    def spy(self: Sandbox, *, allow_network: bool = False, allow_game_writes: bool = False) -> None:
        seen["allow_network"] = allow_network
        seen["allow_game_writes"] = allow_game_writes
        real_init(self, allow_network=allow_network, allow_game_writes=allow_game_writes)

    def devices(sandbox: Sandbox) -> None:
        pass

    def audio(sandbox: Sandbox) -> None:
        pass

    def run(
        command: list[str],
        cwd: Path | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> int:
        return 0

    monkeypatch.setattr(Sandbox, "__init__", spy)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    _patch_run_as_detached(monkeypatch, run)

    assert (
        execute(
            paths,
            repository,
            game.root,
            None,
            False,
            allow_network=True,
            allow_game_writes=allow_game_writes,
        )
        == 0
    )
    assert seen == {"allow_network": True, "allow_game_writes": allow_game_writes}


@pytest.mark.parametrize("allow_game_writes", [False, True])
def test_execute_easyrpg_game_writable_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_game_writes: bool
) -> None:
    paths, repository, game = _prepare_easyrpg_game(tmp_path, monkeypatch)
    binds: list[tuple[str, bool]] = []
    writable_calls: list[int] = []

    def fake_bind(
        self: Sandbox, descriptor: int, destination: str, *, writable: bool = False
    ) -> None:
        binds.append((destination, writable))

    def fake_writable(self: Sandbox, descriptor: int) -> None:
        writable_calls.append(descriptor)
        self._fds.append(descriptor)

    def devices(sandbox: Sandbox) -> None:
        pass

    def audio(sandbox: Sandbox) -> None:
        pass

    seen_fds: list[tuple[int, ...]] = []

    def run(
        command: list[str],
        cwd: Path | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> int:
        seen_fds.append(pass_fds)
        return 0

    monkeypatch.setattr(Sandbox, "bind", fake_bind)
    monkeypatch.setattr(Sandbox, "game_writable", fake_writable)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    _patch_run_as_detached(monkeypatch, run)

    assert (
        execute(paths, repository, game.root, None, False, allow_game_writes=allow_game_writes) == 0
    )
    assert ("/game/save", True) in binds
    if allow_game_writes:
        assert len(writable_calls) == 1
        assert writable_calls[0] in seen_fds[0]
        assert ("/game", False) not in binds
        assert ("/game", True) not in binds
    else:
        assert writable_calls == []
        assert ("/game", False) in binds


def test_execute_nwjs_forwards_game_writes_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "index.html").write_text("fixture")
    (root / "package.json").write_text('{"name": "fixture"}')
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, root / "index.html", root / "package.json")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    binary = runtime_root / "nw"
    binary.write_text("fixture")
    runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), runtime_root, binary)
    paths = AppPaths(tmp_path / "config", tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(root)
    seen: dict[str, bool] = {}
    real_init = Sandbox.__init__

    def spy(self: Sandbox, *, allow_network: bool = False, allow_game_writes: bool = False) -> None:
        seen["allow_network"] = allow_network
        seen["allow_game_writes"] = allow_game_writes
        real_init(self, allow_network=allow_network, allow_game_writes=allow_game_writes)

    def detect(game_path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def select(catalog: object, architecture: str, version: str | None, sdk: bool) -> RuntimeInfo:
        return runtime

    def probe(sandbox: Sandbox) -> str:
        return "wayland"

    def desktop(sandbox: Sandbox) -> None:
        pass

    def devices(sandbox: Sandbox) -> None:
        pass

    def audio(sandbox: Sandbox) -> None:
        pass

    def run(
        command: list[str],
        cwd: Path | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> int:
        return 0

    monkeypatch.setattr("box.api.launch.detect_game", detect)
    monkeypatch.setattr("box.api.launch.select_runtime", select)
    monkeypatch.setattr(Sandbox, "__init__", spy)
    monkeypatch.setattr(Sandbox, "display_probe", probe)
    monkeypatch.setattr(Sandbox, "desktop", desktop)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    _patch_run_as_detached(monkeypatch, run)

    assert execute(paths, repository, root, None, False, allow_game_writes=True) == 0
    assert seen == {"allow_network": False, "allow_game_writes": True}


def test_execute_nwjs_starts_inside_the_game_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relative plugin paths (such as ./www/...) must match a stock export."""
    root = tmp_path / "game"
    (root / "www").mkdir(parents=True)
    (root / "www" / "index.html").write_text("fixture")
    (root / "package.json").write_text('{"name": "fixture"}')
    game = GameInfo(
        EngineName.RPG_MAKER_MV, root, root / "www" / "index.html", root / "package.json"
    )
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    binary = runtime_root / "nw"
    binary.write_text("fixture")
    runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), runtime_root, binary)
    paths = AppPaths(tmp_path / "config", tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(root)
    commands: list[list[str]] = []

    def detect(game_path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def select(catalog: object, architecture: str, version: str | None, sdk: bool) -> RuntimeInfo:
        return runtime

    def probe(sandbox: Sandbox) -> str:
        return "wayland"

    def desktop(sandbox: Sandbox) -> None:
        pass

    def devices(sandbox: Sandbox) -> None:
        pass

    def audio(sandbox: Sandbox) -> None:
        pass

    def run(
        command: list[str],
        cwd: Path | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> int:
        commands.append(command)
        return 0

    monkeypatch.setattr("box.api.launch.detect_game", detect)
    monkeypatch.setattr("box.api.launch.select_runtime", select)
    monkeypatch.setattr(Sandbox, "display_probe", probe)
    monkeypatch.setattr(Sandbox, "desktop", desktop)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    _patch_run_as_detached(monkeypatch, run)

    assert execute(paths, repository, root, None, False) == 0
    assert len(commands) == 1
    chdir = commands[0].index("--chdir")
    assert commands[0][chdir + 1] == "/session/game"
    assert commands[0].index("/session") < chdir


@pytest.mark.parametrize("engine", ["easyrpg", "nwjs"])
def test_execute_calls_devices_and_audio_in_both_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    calls: list[str] = []

    def probe(sandbox: Sandbox) -> str:
        return "wayland"

    def desktop(sandbox: Sandbox) -> None:
        pass

    def devices(sandbox: Sandbox) -> None:
        calls.append("devices")

    def audio(sandbox: Sandbox) -> None:
        calls.append("audio")

    def run(
        command: list[str],
        cwd: Path | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> int:
        return 0

    monkeypatch.setattr(Sandbox, "display_probe", probe)
    monkeypatch.setattr(Sandbox, "desktop", desktop)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    _patch_run_as_detached(monkeypatch, run)
    if engine == "easyrpg":
        paths, repository, game = _prepare_easyrpg_game(tmp_path, monkeypatch)
        assert execute(paths, repository, game.root, None, False) == 0
    else:
        root = tmp_path / "game"
        root.mkdir()
        (root / "index.html").write_text("fixture")
        (root / "package.json").write_text('{"name": "fixture"}')
        game = GameInfo(EngineName.RPG_MAKER_MZ, root, root / "index.html", root / "package.json")
        runtime_root = tmp_path / "runtime"
        runtime_root.mkdir()
        binary = runtime_root / "nw"
        binary.write_text("fixture")
        runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), runtime_root, binary)
        paths = AppPaths(tmp_path / "config", tmp_path / "cache")
        repository = ConfigRepository(paths)
        repository.add_allowed_root(root)

        def detect(game_path: Path, registry: EngineRegistry) -> GameInfo:
            return game

        def select(
            catalog: object, architecture: str, version: str | None, sdk: bool
        ) -> RuntimeInfo:
            return runtime

        monkeypatch.setattr("box.api.launch.detect_game", detect)
        monkeypatch.setattr("box.api.launch.select_runtime", select)
        assert execute(paths, repository, root, None, False) == 0
    assert calls == ["devices", "audio"]


def test_execute_nwjs_uses_x11_ozone_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "index.html").write_text("fixture")
    (root / "package.json").write_text('{"name": "fixture"}')
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, root / "index.html", root / "package.json")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    binary = runtime_root / "nw"
    binary.write_text("fixture")
    runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), runtime_root, binary)
    paths = AppPaths(tmp_path / "config", tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(root)
    commands: list[list[str]] = []
    monkeypatch.setenv("DISPLAY", ":0")

    def detect(game_path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def select(catalog: object, architecture: str, version: str | None, sdk: bool) -> RuntimeInfo:
        return runtime

    def probe(sandbox: Sandbox) -> str:
        return "x11"

    def desktop(sandbox: Sandbox) -> None:
        pytest.fail("wayland must not be used")

    def use_x11(sandbox: Sandbox) -> None:
        pass

    def devices(sandbox: Sandbox) -> None:
        pass

    def audio(sandbox: Sandbox) -> None:
        pass

    def stdin_is_tty() -> bool:
        return True

    def confirm(prompt: str) -> str:
        return "yes"

    monkeypatch.setattr("box.api.launch.detect_game", detect)
    monkeypatch.setattr("box.api.launch.select_runtime", select)
    monkeypatch.setattr(Sandbox, "display_probe", probe)
    monkeypatch.setattr(Sandbox, "desktop", desktop)
    monkeypatch.setattr(Sandbox, "x11", use_x11)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    monkeypatch.setattr("sys.stdin.isatty", stdin_is_tty)
    monkeypatch.setattr("builtins.input", confirm)

    def run(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
        commands.append(command)
        return 0

    _patch_run_as_detached(monkeypatch, run)

    assert execute(paths, repository, root, None, False) == 0
    assert any("--ozone-platform=x11" in command for command in commands)
    assert not any("--ozone-platform=wayland" in command for command in commands)


def test_execute_nwjs_forced_x11_skips_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "index.html").write_text("fixture")
    (root / "package.json").write_text('{"name": "fixture"}')
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, root / "index.html", root / "package.json")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    binary = runtime_root / "nw"
    binary.write_text("fixture")
    runtime = RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), runtime_root, binary)
    paths = AppPaths(tmp_path / "config", tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(root)
    commands: list[list[str]] = []
    x11_calls: list[None] = []

    def detect(game_path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def select(catalog: object, architecture: str, version: str | None, sdk: bool) -> RuntimeInfo:
        return runtime

    def probe(sandbox: Sandbox) -> str:
        return "wayland"

    def desktop(sandbox: Sandbox) -> None:
        pass

    def use_x11(sandbox: Sandbox) -> None:
        x11_calls.append(None)

    def devices(sandbox: Sandbox) -> None:
        pass

    def audio(sandbox: Sandbox) -> None:
        pass

    def forbidden(prompt: str) -> str:
        raise AssertionError("explicit --x11 is consent, must not prompt")

    monkeypatch.setattr("box.api.launch.detect_game", detect)
    monkeypatch.setattr("box.api.launch.select_runtime", select)
    monkeypatch.setattr(Sandbox, "display_probe", probe)
    monkeypatch.setattr(Sandbox, "desktop", desktop)
    monkeypatch.setattr(Sandbox, "x11", use_x11)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    monkeypatch.setattr("builtins.input", forbidden)

    def run(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
        commands.append(command)
        return 0

    _patch_run_as_detached(monkeypatch, run)

    assert execute(paths, repository, root, None, False, x11=True) == 0
    assert len(x11_calls) == 1
    assert any("--ozone-platform=x11" in command for command in commands)
    assert not any("--ozone-platform=wayland" in command for command in commands)


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_execute_easyrpg_extra_x11_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    executable = runtime_root / "easyrpg-player"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    x11_calls: list[None] = []
    monkeypatch.setenv("DISPLAY", ":0")

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    def latest(_: object) -> EasyRPGRuntime:
        return EasyRPGRuntime("0.8.1.1", runtime_root)

    def authorize(
        _: GameInfo,
        __: AppConfig,
        ___: ConfigRepository,
        ____: Callable[[str], str] | None = None,
    ) -> AppConfig:
        return repository.load()

    def probe(_: Sandbox) -> str:
        return "wayland"

    def desktop(_: Sandbox) -> None:
        pass

    def use_x11(_: Sandbox) -> None:
        x11_calls.append(None)

    def devices(_: Sandbox) -> None:
        pass

    def audio(_: Sandbox) -> None:
        pass

    def run(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
        return 0

    monkeypatch.setattr("box.api.launch.detect_game", detect_game)
    monkeypatch.setattr("box.api.launch.EasyRPGCatalog.latest", latest)
    monkeypatch.setattr("box.api.launch.authorize_game", authorize)
    monkeypatch.setattr(Sandbox, "display_probe", probe)
    monkeypatch.setattr(Sandbox, "desktop", desktop)
    monkeypatch.setattr(Sandbox, "x11", use_x11)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    _patch_run_as_detached(monkeypatch, run)

    def stdin_is_tty() -> bool:
        return True

    def confirm(prompt: str) -> str:
        return answer

    monkeypatch.setattr("sys.stdin.isatty", stdin_is_tty)
    monkeypatch.setattr("builtins.input", confirm)

    if answer == "yes":
        assert execute(paths, repository, game_root, None, False) == 0
        assert len(x11_calls) == 1
    else:
        with pytest.raises(GameValidationError):
            execute(paths, repository, game_root, None, False)
        assert x11_calls == []


def test_execute_easyrpg_forced_x11_skips_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    executable = runtime_root / "easyrpg-player"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    repository = ConfigRepository(paths)
    x11_calls: list[None] = []

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    def latest(_: object) -> EasyRPGRuntime:
        return EasyRPGRuntime("0.8.1.1", runtime_root)

    def authorize(
        _: GameInfo,
        __: AppConfig,
        ___: ConfigRepository,
        ____: Callable[[str], str] | None = None,
    ) -> AppConfig:
        return repository.load()

    def desktop(_: Sandbox) -> None:
        pytest.fail("forced --x11 must not use wayland")

    def use_x11(_: Sandbox) -> None:
        x11_calls.append(None)

    def devices(_: Sandbox) -> None:
        pass

    def audio(_: Sandbox) -> None:
        pass

    def forbidden(prompt: str) -> str:
        raise AssertionError("explicit --x11 is consent, must not prompt")

    def run(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
        return 0

    monkeypatch.setattr("box.api.launch.detect_game", detect_game)
    monkeypatch.setattr("box.api.launch.EasyRPGCatalog.latest", latest)
    monkeypatch.setattr("box.api.launch.authorize_game", authorize)
    monkeypatch.setattr(Sandbox, "desktop", desktop)
    monkeypatch.setattr(Sandbox, "x11", use_x11)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    _patch_run_as_detached(monkeypatch, run)
    monkeypatch.setattr("builtins.input", forbidden)

    assert execute(paths, repository, game_root, None, False, x11=True) == 0
    assert len(x11_calls) == 1


def _prepare_runtime_choice_game(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    versions: tuple[str, ...],
    answers: list[str],
    *,
    tty: bool = True,
) -> tuple[AppPaths, ConfigRepository, GameInfo, list[Path]]:
    """Fake an NW.js game with installed runtimes and scripted terminal input."""
    root = tmp_path / "game"
    root.mkdir()
    (root / "index.html").write_text("fixture")
    (root / "package.json").write_text('{"name": "fixture"}')
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, root / "index.html", root / "package.json")
    architecture = current_architecture()
    binaries: list[Path] = []
    for version in versions:
        binary = (
            tmp_path
            / "cache"
            / "runtimes"
            / "nwjs"
            / f"linux-{architecture}"
            / f"standard-{version}"
            / "nw"
        )
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o700)
        binaries.append(binary)
    paths = AppPaths(tmp_path / "config", tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(root)

    def detect(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    def desktop(_: Sandbox) -> None:
        pass

    def devices(_: Sandbox) -> None:
        pass

    def audio(_: Sandbox) -> None:
        pass

    remaining = list(answers)

    def confirm(prompt: str) -> str:
        assert remaining, f"unexpected prompt: {prompt}"
        return remaining.pop(0)

    def stdin_is_tty() -> bool:
        return tty

    def run(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
        return 0

    monkeypatch.setattr("box.api.launch.detect_game", detect)
    monkeypatch.setattr(Sandbox, "desktop", desktop)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    _patch_run_as_detached(monkeypatch, run)
    monkeypatch.setattr("sys.stdin.isatty", stdin_is_tty)
    monkeypatch.setattr("builtins.input", confirm)
    return paths, repository, game, binaries


def _record_selected_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Path]:
    seen: list[Path] = []

    def fake_runtime(self: Sandbox, executable: Path) -> str:
        seen.append(executable)
        return "/runtime/nw"

    monkeypatch.setattr(Sandbox, "runtime", fake_runtime)
    return seen


@pytest.mark.parametrize(
    ("answers", "selected"),
    [(["2"], 1), ([""], 0), (["9", "x", "1"], 0)],
)
def test_execute_launch_prompts_for_runtime_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: list[str],
    selected: int,
) -> None:
    versions = ("v0.115.0", "v0.112.0")
    paths, repository, game, binaries = _prepare_runtime_choice_game(
        tmp_path, monkeypatch, versions, answers
    )
    seen = _record_selected_runtime(monkeypatch)

    assert execute(paths, repository, game.root, None, False) == 0
    assert seen == [binaries[selected]]
    output = capsys.readouterr().out
    assert "Installed NW.js runtimes" in output
    assert "  1. v0.115.0" in output
    assert "  2. v0.112.0" in output
    if len(answers) > 1:
        assert "Invalid selection." in output


@pytest.mark.parametrize("answer", ["q", "Q"])
def test_execute_launch_runtime_decline_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    paths, repository, game, _binaries = _prepare_runtime_choice_game(
        tmp_path, monkeypatch, ("v0.115.0", "v0.112.0"), [answer]
    )
    seen = _record_selected_runtime(monkeypatch)

    with pytest.raises(GameValidationError, match="cancelled"):
        execute(paths, repository, game.root, None, False)
    assert seen == []


def test_execute_launch_runtime_end_of_input_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, repository, game, _binaries = _prepare_runtime_choice_game(
        tmp_path, monkeypatch, ("v0.115.0", "v0.112.0"), []
    )

    def missing(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", missing)

    with pytest.raises(GameValidationError, match="cancelled"):
        execute(paths, repository, game.root, None, False)


def test_execute_launch_skips_prompt_with_single_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, repository, game, binaries = _prepare_runtime_choice_game(
        tmp_path, monkeypatch, ("v0.115.0",), []
    )
    seen = _record_selected_runtime(monkeypatch)

    assert execute(paths, repository, game.root, None, False) == 0
    assert seen == [binaries[0]]


def test_execute_launch_skips_prompt_with_explicit_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, repository, game, binaries = _prepare_runtime_choice_game(
        tmp_path, monkeypatch, ("v0.115.0", "v0.112.0"), []
    )
    seen = _record_selected_runtime(monkeypatch)

    assert execute(paths, repository, game.root, "v0.112.0", False) == 0
    assert seen == [binaries[1]]


def test_execute_launch_skips_prompt_with_preferred_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, repository, game, binaries = _prepare_runtime_choice_game(
        tmp_path, monkeypatch, ("v0.115.0", "v0.112.0"), []
    )
    repository.save(AppConfig(allowed_game_roots=(game.root,), preferred_runtime="v0.112.0"))
    seen = _record_selected_runtime(monkeypatch)

    assert execute(paths, repository, game.root, None, False) == 0
    assert seen == [binaries[1]]


def test_execute_launch_skips_prompt_without_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, repository, game, binaries = _prepare_runtime_choice_game(
        tmp_path, monkeypatch, ("v0.115.0", "v0.112.0"), [], tty=False
    )
    seen = _record_selected_runtime(monkeypatch)

    assert execute(paths, repository, game.root, None, False) == 0
    assert seen == [binaries[0]]


def _prepare_easyrpg_choice_game(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    versions: tuple[str, ...],
    answers: list[str],
    *,
    tty: bool = True,
) -> tuple[AppPaths, ConfigRepository, GameInfo, list[Path], list[list[str]]]:
    """Fake a 2000/2003 game with installed players and scripted terminal input."""
    from box.runtime.easyrpg import EasyRPGCatalog

    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    binaries: list[Path] = []
    for version in versions:
        binary = tmp_path / "cache" / "runtimes" / "easyrpg" / version / "easyrpg-player"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o700)
        binaries.append(binary)
    paths = AppPaths(tmp_path / "config", tmp_path / "cache")
    repository = ConfigRepository(paths)
    repository.add_allowed_root(game_root)
    candidates = [runtime.root / "easyrpg-player" for runtime in EasyRPGCatalog(paths).list()]

    def detect_game(_: Path, __: EngineRegistry) -> GameInfo:
        return game

    def desktop(_: Sandbox) -> None:
        pass

    def devices(_: Sandbox) -> None:
        pass

    def audio(_: Sandbox) -> None:
        pass

    remaining = list(answers)

    def confirm(prompt: str) -> str:
        assert remaining, f"unexpected prompt: {prompt}"
        return remaining.pop(0)

    def stdin_is_tty() -> bool:
        return tty

    launched: list[list[str]] = []

    def run(command: list[str], cwd: Path | None = None, pass_fds: tuple[int, ...] = ()) -> int:
        launched.append(command)
        return 0

    monkeypatch.setattr("box.api.launch.detect_game", detect_game)
    monkeypatch.setattr(Sandbox, "desktop", desktop)
    monkeypatch.setattr(Sandbox, "devices", devices)
    monkeypatch.setattr(Sandbox, "audio", audio)
    _patch_run_as_detached(monkeypatch, run)
    monkeypatch.setattr("sys.stdin.isatty", stdin_is_tty)
    monkeypatch.setattr("builtins.input", confirm)
    # Hermetic display: the extra-X11 consent reads the real DISPLAY.
    monkeypatch.delenv("DISPLAY", raising=False)
    return paths, repository, game, candidates, launched


@pytest.mark.parametrize(
    ("answers", "selected"),
    [(["2"], 1), ([""], 0), (["9", "x", "1"], 0)],
)
def test_execute_easyrpg_prompts_for_player_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: list[str],
    selected: int,
) -> None:
    paths, repository, game, candidates, launched = _prepare_easyrpg_choice_game(
        tmp_path, monkeypatch, ("0.8.1.1", "0.8"), answers
    )
    seen: list[Path] = []

    def fake_runtime(self: Sandbox, executable: Path) -> str:
        seen.append(executable)
        return "/runtime/easyrpg-player"

    monkeypatch.setattr(Sandbox, "runtime", fake_runtime)

    assert execute(paths, repository, game.root, None, False) == 0
    assert len(launched) == 1
    assert seen == [candidates[selected]]
    output = capsys.readouterr().out
    assert "Installed EasyRPG Player runtimes (x64):" in output
    assert "  1. 0.8.1.1" in output
    assert "  2. 0.8" in output
    if len(answers) > 1:
        assert "Invalid selection." in output


@pytest.mark.parametrize("answer", ["q", "Q"])
def test_execute_easyrpg_player_decline_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    paths, repository, game, _candidates, launched = _prepare_easyrpg_choice_game(
        tmp_path, monkeypatch, ("0.8.1.1", "0.8"), [answer]
    )

    with pytest.raises(GameValidationError, match="cancelled"):
        execute(paths, repository, game.root, None, False)
    assert launched == []


def test_execute_easyrpg_player_end_of_input_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, repository, game, _candidates, launched = _prepare_easyrpg_choice_game(
        tmp_path, monkeypatch, ("0.8.1.1", "0.8"), []
    )

    def missing(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", missing)

    with pytest.raises(GameValidationError, match="cancelled"):
        execute(paths, repository, game.root, None, False)
    assert launched == []


def test_execute_easyrpg_skips_prompt_with_single_player(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, repository, game, _candidates, launched = _prepare_easyrpg_choice_game(
        tmp_path, monkeypatch, ("0.8.1.1",), []
    )

    assert execute(paths, repository, game.root, None, False) == 0
    assert len(launched) == 1


def test_execute_easyrpg_skips_prompt_without_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, repository, game, candidates, launched = _prepare_easyrpg_choice_game(
        tmp_path, monkeypatch, ("0.8.1.1", "0.8"), [], tty=False
    )

    assert execute(paths, repository, game.root, None, False) == 0
    assert len(launched) == 1
    assert candidates[0].name == "easyrpg-player"


def test_execute_easyrpg_uses_explicit_version_without_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, repository, game, candidates, launched = _prepare_easyrpg_choice_game(
        tmp_path, monkeypatch, ("0.8.1.1", "0.8"), []
    )
    seen: list[Path] = []

    def fake_runtime(self: Sandbox, executable: Path) -> str:
        seen.append(executable)
        return "/runtime/easyrpg-player"

    monkeypatch.setattr(Sandbox, "runtime", fake_runtime)

    assert execute(paths, repository, game.root, "0.8", False) == 0
    assert len(launched) == 1
    assert seen == [candidates[1]]


def test_execute_easyrpg_rejects_unknown_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, repository, game, _candidates, launched = _prepare_easyrpg_choice_game(
        tmp_path, monkeypatch, ("0.8.1.1",), []
    )

    with pytest.raises(RuntimeError, match="not installed"):
        execute(paths, repository, game.root, "0.7.0", False)
    assert launched == []
