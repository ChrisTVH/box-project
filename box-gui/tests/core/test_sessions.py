"""Advisory session-probe unit tests without any GTK dependency."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from box.errors import LaunchError

from box_gui.core.sessions import (
    game_identifier,
    is_session_running,
    live_session_names,
    poll_session_status,
    session_key_for_entry,
    stop_session,
)


def _entry(path: Path, **fields: Any) -> SimpleNamespace:
    """Build a minimal entry-shaped object keyed by path."""
    return SimpleNamespace(path=path, display_name=path.name, order=0, **fields)


def _paths(tmp_path: Path) -> Any:
    """Build isolated AppPaths under tmp_path."""
    from box.paths import AppPaths

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    return paths


def test_session_key_prefers_session_id() -> None:
    entry = _entry(Path("/games/a"), session_id="session-1")

    assert session_key_for_entry(entry) == "session-1"


def test_session_key_falls_back_to_path() -> None:
    entry = _entry(Path("/games/a"))

    assert session_key_for_entry(entry) == Path("/games/a")


def test_game_identifier_stable_for_existing_dir(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()

    assert game_identifier(game) == game_identifier(game)
    assert isinstance(game_identifier(game), str)


def test_game_identifier_none_for_missing_dir(tmp_path: Path) -> None:
    assert game_identifier(tmp_path / "gone") is None


def test_live_names_empty_on_old_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import box.api.launch as launch_api

    monkeypatch.delattr(launch_api, "find_live_sessions", raising=False)
    game = tmp_path / "game"
    game.mkdir()

    assert live_session_names(_paths(tmp_path), _entry(game)) == ()


def test_live_names_reports_backend_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import box.api.launch as launch_api

    game = tmp_path / "game"
    game.mkdir()
    monkeypatch.setattr(
        launch_api,
        "find_live_sessions",
        lambda paths, identifier: ["s1", "s2"],
        raising=False,
    )

    assert live_session_names(_paths(tmp_path), _entry(game)) == ("s1", "s2")


def test_live_names_empty_on_probe_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import box.api.launch as launch_api

    game = tmp_path / "game"
    game.mkdir()

    def _boom(paths: Any, identifier: str) -> list[str]:
        raise LaunchError("cannot list")

    monkeypatch.setattr(launch_api, "find_live_sessions", _boom, raising=False)

    assert live_session_names(_paths(tmp_path), _entry(game)) == ()


def test_is_session_running_uses_listing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import box.api.launch as launch_api

    live = tmp_path / "live"
    live.mkdir()
    idle = tmp_path / "idle"
    idle.mkdir()
    paths = _paths(tmp_path)
    live_id = game_identifier(live)
    monkeypatch.setattr(
        launch_api,
        "find_live_sessions",
        lambda got_paths, identifier: ["s1"] if identifier == live_id else [],
        raising=False,
    )

    assert is_session_running(paths, _entry(live)) is True
    assert is_session_running(paths, _entry(idle)) is False


def test_is_session_running_prefers_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import box.api.launch as launch_api

    monkeypatch.setattr(
        launch_api, "is_session_running", lambda key: key == "live-1", raising=False
    )
    monkeypatch.delattr(launch_api, "find_live_sessions", raising=False)

    assert is_session_running(None, _entry(Path("/games/a"), session_id="live-1")) is True
    assert is_session_running(None, _entry(Path("/games/a"), session_id="dead-9")) is False


def test_poll_status_reports_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import box.api.launch as launch_api

    game = tmp_path / "game"
    game.mkdir()
    paths = _paths(tmp_path)
    entry = _entry(game)
    identifier = game_identifier(game)
    seen: list[Any] = []

    def _fake_poll(got_paths: Any, got_identifier: str, name: str) -> int | None:
        seen.append((got_identifier, name))
        return 3 if name == "done" else None

    monkeypatch.setattr(launch_api, "poll_launch_status", _fake_poll, raising=False)

    assert poll_session_status(paths, entry, "done") == 3
    assert poll_session_status(paths, entry, "live") is None
    assert seen == [(identifier, "done"), (identifier, "live")]


def test_poll_status_none_on_old_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import box.api.launch as launch_api

    game = tmp_path / "game"
    game.mkdir()
    monkeypatch.delattr(launch_api, "poll_launch_status", raising=False)

    assert poll_session_status(_paths(tmp_path), _entry(game), "s1") is None


def test_stop_session_missing_raises_launch_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import box.api.launch as launch_api

    game = tmp_path / "game"
    game.mkdir()
    monkeypatch.delattr(launch_api, "stop_session", raising=False)

    with pytest.raises(LaunchError):
        stop_session(_paths(tmp_path), _entry(game), "s1")


def test_stop_session_without_live_names_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import box.api.launch as launch_api

    game = tmp_path / "game"
    game.mkdir()
    stopped: list[Any] = []
    monkeypatch.setattr(
        launch_api, "stop_session", lambda *args: stopped.append(args), raising=False
    )
    monkeypatch.setattr(launch_api, "find_live_sessions", lambda *args: [], raising=False)

    with pytest.raises(LaunchError):
        stop_session(_paths(tmp_path), _entry(game))
    assert stopped == []


def test_stop_session_calls_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import box.api.launch as launch_api

    game = tmp_path / "game"
    game.mkdir()
    paths = _paths(tmp_path)
    entry = _entry(game)
    identifier = game_identifier(game)
    stopped: list[Any] = []
    monkeypatch.setattr(
        launch_api, "stop_session", lambda *args: stopped.append(args), raising=False
    )

    stop_session(paths, entry, "s1")

    assert stopped == [(paths, identifier, "s1")]


def test_packed_source_probes_use_source_keyed_identifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Packed folders probe and stop with the source-keyed session identifier.

    The library keeps the source folder while launch keys sessions by the
    source root, so Running badges and stop must use that same key for
    packed games to show as running and stoppable.
    """
    import box.api.launch as launch_api
    from box.games.identity import game_id

    source = tmp_path / "game"
    source.mkdir()
    (source / "Game.exe").write_bytes(b"fake packed executable")
    paths = _paths(tmp_path)
    entry = _entry(source)
    expected = game_id(source)
    assert game_identifier(source) == expected
    seen: list[str] = []

    def _fake_listing(got_paths: Any, identifier: str) -> list[str]:
        seen.append(identifier)
        return ["s1"] if identifier == expected else []

    monkeypatch.setattr(launch_api, "find_live_sessions", _fake_listing, raising=False)

    assert live_session_names(paths, entry) == ("s1",)
    assert seen == [expected]
    assert is_session_running(paths, entry) is True

    stopped: list[Any] = []
    monkeypatch.setattr(
        launch_api, "stop_session", lambda *args: stopped.append(args), raising=False
    )

    stop_session(paths, entry, "s1")

    assert stopped == [(paths, expected, "s1")]
