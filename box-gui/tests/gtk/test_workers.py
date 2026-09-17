"""Direct unit tests for launch flag forwarding with old backends."""

# pyright: reportMissingImports=false
# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from box.errors import LaunchError

from box_gui.gtk import workers


def _call(
    monkeypatch: pytest.MonkeyPatch,
    fake_launch: Any,
    *,
    gamemode: bool = False,
    ci_mount: bool = False,
) -> Any:
    """Invoke the helper with a stubbed backend launch."""
    monkeypatch.setattr(workers, "launch", fake_launch)
    return workers._launch_with_gamemode(
        cast(Any, None),
        cast(Any, None),
        Path("/game"),
        cast(Any, None),
        version=None,
        sdk=False,
        copy_root_files=(),
        allow_network=False,
        allow_game_writes=False,
        x11=False,
        gamemode=gamemode,
        ci_mount=ci_mount,
    )


def test_old_backend_without_flags_launches_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backends predating both flags launch without extra keywords."""
    seen: dict[str, Any] = {}

    def _old_launch(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return 0

    assert _call(monkeypatch, _old_launch) == 0
    assert "use_gamemode" not in seen
    assert "ci_mount" not in seen


def test_old_backend_rejects_requested_ci_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """A requested mount fails closed instead of launching without it."""

    def _old_launch(
        paths: Any,
        repository: Any,
        game_path: Any,
        *,
        version: Any = None,
        sdk: Any = False,
        copy_root_files: Any = (),
        allow_network: Any = False,
        allow_game_writes: Any = False,
        x11: Any = False,
        interaction: Any = None,
    ) -> Any:
        raise AssertionError("must not launch without the requested flag")

    with pytest.raises(LaunchError):
        _call(monkeypatch, _old_launch, ci_mount=True)


def test_old_backend_rejects_requested_gamemode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A requested GameMode keeps its existing fail-closed behavior."""

    def _old_launch(
        paths: Any,
        repository: Any,
        game_path: Any,
        *,
        version: Any = None,
        sdk: Any = False,
        copy_root_files: Any = (),
        allow_network: Any = False,
        allow_game_writes: Any = False,
        x11: Any = False,
        interaction: Any = None,
    ) -> Any:
        raise AssertionError("must not launch without the requested flag")

    with pytest.raises(LaunchError):
        _call(monkeypatch, _old_launch, gamemode=True)


def test_new_backend_forwards_both_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backends with both keywords receive both requested values."""
    seen: dict[str, Any] = {}

    def _new_launch(
        paths: Any,
        repository: Any,
        game_path: Any,
        *,
        version: Any = None,
        sdk: Any = False,
        copy_root_files: Any = (),
        allow_network: Any = False,
        allow_game_writes: Any = False,
        x11: Any = False,
        use_gamemode: Any = False,
        ci_mount: Any = False,
        interaction: Any = None,
    ) -> Any:
        seen["use_gamemode"] = use_gamemode
        seen["ci_mount"] = ci_mount
        return 0

    assert _call(monkeypatch, _new_launch, gamemode=True, ci_mount=True) == 0
    assert seen == {"use_gamemode": True, "ci_mount": True}
