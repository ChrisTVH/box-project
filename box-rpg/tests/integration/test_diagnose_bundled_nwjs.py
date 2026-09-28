"""Tests for bundled NW.js version detection in Windows game exports."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from pathlib import Path

import pytest

from box.api import diagnose as api_diagnose
from box.config.repository import ConfigRepository
from box.diagnostics import versions as versions_module
from box.diagnostics.environment import Environment
from box.diagnostics.report import render_report
from box.diagnostics.versions import VersionReport, detect_bundled_nwjs_version
from box.engines.registry import EngineRegistry
from box.models import EngineName, GameInfo, RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime.easyrpg import EasyRPGRuntime

_STAMP = b"process.versions['node-webkit'] = '0.89.0';"
_OTHER_STAMP = b"process.versions['nw'] = '0.90.0';"


def _write(path: Path, data: bytes) -> Path:
    """Create a fixture file with its parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _mz_game(root: Path) -> GameInfo:
    """Create a minimal detectable MZ tree and return its game info."""
    _write(root / "index.html", b"<html></html>")
    _write(root / "js" / "rmmz_core.js", b'RPGMAKER_VERSION = "1.9.0"\n')
    return GameInfo(EngineName.RPG_MAKER_MZ, root, root / "index.html", None)


def _runtime(tmp_path: Path) -> RuntimeInfo:
    """Return a managed runtime pointing at a temporary directory."""
    return RuntimeInfo(RuntimeSpec("v0.90.0", "x64"), tmp_path, tmp_path / "nw")


def _paths(tmp_path: Path) -> AppPaths:
    """Build isolated launcher paths inside a temporary directory."""
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


def test_detects_source_dll_and_normalizes_prefix(tmp_path: Path) -> None:
    source = tmp_path / "source"
    game_root = tmp_path / "game"
    game_root.mkdir(parents=True)
    _write(source / "nw.dll", b"\x00padding" + _STAMP + b"\x00trailing")

    assert detect_bundled_nwjs_version(game_root, source) == "v0.89.0"


def test_prefers_source_dll_over_effective_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    game_root = tmp_path / "game"
    _write(source / "nw.dll", _STAMP)
    _write(game_root / "nw.dll", _OTHER_STAMP)

    assert detect_bundled_nwjs_version(game_root, source) == "v0.89.0"


def test_falls_back_to_effective_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    game_root = tmp_path / "game"
    source.mkdir(parents=True)
    _write(game_root / "nw.dll", _OTHER_STAMP)

    assert detect_bundled_nwjs_version(game_root, source) == "v0.90.0"


def test_falls_back_to_source_executable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    game_root = tmp_path / "profile-game"
    source.mkdir(parents=True)
    game_root.mkdir(parents=True)
    executable = _write(source / "Game.exe", b"MZ\x90\x00" + _STAMP + b"\x00tail")

    assert detect_bundled_nwjs_version(game_root, source, executable) == "v0.89.0"


def test_missing_files_return_none(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir(parents=True)

    assert detect_bundled_nwjs_version(game_root, tmp_path / "absent") is None
    assert detect_bundled_nwjs_version(game_root) is None


def test_symlinked_dll_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    source.mkdir(parents=True)
    real = _write(outside / "real.dll", _STAMP)
    (source / "nw.dll").symlink_to(real)

    assert detect_bundled_nwjs_version(tmp_path / "game", source) is None


def test_non_numeric_stamp_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "nw.dll", b"process.versions['node-webkit'] = 'nightly';")

    assert detect_bundled_nwjs_version(tmp_path / "game", source) is None


def test_invalid_stamp_does_not_hide_later_valid_stamp(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(
        source / "nw.dll",
        b"process.versions['nw'] = 'nightly';" + b"\x00" * 64 + _STAMP,
    )

    assert detect_bundled_nwjs_version(tmp_path / "game", source) == "v0.89.0"


def test_restored_executable_wins_over_source_executable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    game_root = tmp_path / "profile-game"
    executable = _write(source / "Game.exe", _STAMP)
    _write(game_root / "Game.exe", _OTHER_STAMP)

    assert detect_bundled_nwjs_version(game_root, source, executable) == "v0.90.0"


def test_empty_file_returns_none(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "nw.dll", b"")

    assert detect_bundled_nwjs_version(tmp_path / "game", source) is None


def test_directory_named_dll_returns_none(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "nw.dll").mkdir(parents=True)

    assert detect_bundled_nwjs_version(tmp_path / "game", source) is None


def test_over_budget_file_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(versions_module, "_BUNDLED_SCAN_BUDGET", 10)
    source = tmp_path / "source"
    _write(source / "nw.dll", _STAMP)

    assert detect_bundled_nwjs_version(tmp_path / "game", source) is None


def test_stamp_split_across_chunks_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(versions_module, "_BUNDLED_SCAN_CHUNK", 32)
    source = tmp_path / "source"
    _write(source / "nw.dll", b"A" * 30 + _STAMP + b"B" * 30)

    assert detect_bundled_nwjs_version(tmp_path / "game", source) == "v0.89.0"


def test_diagnose_attaches_bundled_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Diagnose reports the source nw.dll version beside the managed runtime."""
    game_dir = tmp_path / "game"
    game = _mz_game(game_dir)
    _write(game_dir / "nw.dll", _STAMP)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    runtime = _runtime(tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        assert path == game_dir
        return game

    def fake_architecture() -> str:
        return "x64"

    def fake_select(
        catalog: object, architecture: str, version: str | None, sdk: bool
    ) -> RuntimeInfo:
        return runtime

    def fake_environment() -> Environment:
        return Environment("Linux", "1.0", "x86_64")

    def fake_binary_version(executable: Path, fallback: str) -> str:
        return fallback

    monkeypatch.setattr(api_diagnose, "detect_game", fake_detect)
    monkeypatch.setattr(api_diagnose, "current_architecture", fake_architecture)
    monkeypatch.setattr(api_diagnose, "select_runtime", fake_select)
    monkeypatch.setattr(api_diagnose, "collect_environment", fake_environment)
    monkeypatch.setattr(versions_module, "_binary_version", fake_binary_version)

    result = api_diagnose.diagnose(paths, repository, game_dir, None, False)

    assert result.versions.nwjs == "v0.90.0"
    assert result.versions.game_nwjs == "v0.89.0"


def test_diagnose_without_bundled_dll_omits_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Games without a shipped nw.dll keep the report unchanged."""
    game_dir = tmp_path / "game"
    game = _mz_game(game_dir)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    runtime = _runtime(tmp_path)

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def fake_architecture() -> str:
        return "x64"

    def fake_select(
        catalog: object, architecture: str, version: str | None, sdk: bool
    ) -> RuntimeInfo:
        return runtime

    def fake_environment() -> Environment:
        return Environment("Linux", "1.0", "x86_64")

    def fake_binary_version(executable: Path, fallback: str) -> str:
        return fallback

    monkeypatch.setattr(api_diagnose, "detect_game", fake_detect)
    monkeypatch.setattr(api_diagnose, "current_architecture", fake_architecture)
    monkeypatch.setattr(api_diagnose, "select_runtime", fake_select)
    monkeypatch.setattr(api_diagnose, "collect_environment", fake_environment)
    monkeypatch.setattr(versions_module, "_binary_version", fake_binary_version)

    result = api_diagnose.diagnose(paths, repository, game_dir, None, False)

    assert result.versions.game_nwjs is None


def test_diagnose_easyrpg_ignores_bundled_dll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EasyRPG games never report a bundled NW.js version."""
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True)
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_dir)
    _write(game_dir / "nw.dll", _STAMP)
    paths = _paths(tmp_path)
    repository = ConfigRepository(paths)
    player = EasyRPGRuntime("0.8.1", tmp_path)

    class FakeCatalog:
        def __init__(self, actual_paths: AppPaths) -> None:
            assert actual_paths == paths

        def get(self, version: str) -> EasyRPGRuntime:
            raise AssertionError("latest must be used without a version")

        def latest(self) -> EasyRPGRuntime:
            return player

    def fake_detect(path: Path, registry: EngineRegistry) -> GameInfo:
        return game

    def fake_easyrpg_versions(actual_game: GameInfo, runtime: EasyRPGRuntime) -> VersionReport:
        assert actual_game == game
        return VersionReport("rpg-maker-2000-2003", None, None, runtime.version)

    def fake_environment() -> Environment:
        return Environment("Linux", "1.0", "x86_64")

    monkeypatch.setattr(api_diagnose, "detect_game", fake_detect)
    monkeypatch.setattr(api_diagnose, "EasyRPGCatalog", FakeCatalog)
    monkeypatch.setattr(api_diagnose, "collect_easyrpg_versions", fake_easyrpg_versions)
    monkeypatch.setattr(api_diagnose, "collect_environment", fake_environment)

    result = api_diagnose.diagnose(paths, repository, game_dir, None, False)

    assert result.versions.game_nwjs is None
    assert result.versions.easyrpg_player == "0.8.1"


def test_resolve_effective_game_without_packing(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    game_dir = tmp_path / "game"
    (game_dir / "data").mkdir(parents=True)

    effective, source_root, source_exe = api_diagnose._resolve_effective_game(paths, game_dir)

    assert effective == game_dir
    assert source_root == game_dir.resolve(strict=True)
    assert source_exe is None


def test_resolve_effective_game_unresolvable(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    missing = tmp_path / "absent"

    assert api_diagnose._resolve_effective_game(paths, missing) == (missing, None, None)


def test_resolve_effective_game_packed_returns_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.runtime import evb as evb_module

    paths = _paths(tmp_path)
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True)
    executable = _write(game_dir / "Game.exe", b"fake packed executable")
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()

    def fake_find(root: Path) -> Path | None:
        assert root == game_dir.resolve(strict=True)
        return executable

    def fake_unpack(actual_paths: AppPaths, exe: Path) -> Path:
        assert actual_paths == paths
        assert exe == executable
        return unpacked

    monkeypatch.setattr(evb_module, "find_packed_executable", fake_find)
    monkeypatch.setattr(evb_module, "ensure_unpacked", fake_unpack)

    effective, source_root, source_exe = api_diagnose._resolve_effective_game(paths, game_dir)

    assert effective == unpacked
    assert source_root == game_dir.resolve(strict=True)
    assert source_exe == executable


def test_render_report_includes_bundled_version_only_when_present() -> None:
    environment = Environment("Linux", "1.0", "x86_64")
    with_bundled = VersionReport("rpg-maker-mz", "1.9.0", "v0.116.0", None, "v0.89.0")
    payload = json.loads(render_report(environment, with_bundled))
    assert payload["versions"]["game_nwjs"] == "v0.89.0"

    without_bundled = VersionReport("rpg-maker-mz", "1.9.0", "v0.116.0")
    payload = json.loads(render_report(environment, without_bundled))
    assert "game_nwjs" not in payload["versions"]
