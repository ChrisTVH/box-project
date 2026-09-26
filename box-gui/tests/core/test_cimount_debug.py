"""Env switch tests for the ci-mount daemon debug log (no GTK dependency)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from box.paths import AppPaths

from box_gui.core.cimount_debug import (
    CIMOUNT_DEBUG_LOG_ENV,
    CIMOUNT_DEBUG_LOG_FILENAME,
    apply_debug_log,
    default_debug_log_path,
    disable_debug_log,
    enable_debug_log,
)


def _paths(tmp_path: Path) -> AppPaths:
    """Build isolated AppPaths under tmp_path."""
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


def test_default_path_sits_under_the_cache_root(tmp_path: Path) -> None:
    """The unconfigured trace lands in the launcher-owned cache root."""
    paths = _paths(tmp_path)

    assert default_debug_log_path(paths) == (tmp_path / "cache" / CIMOUNT_DEBUG_LOG_FILENAME)


def test_enable_exports_the_chosen_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Enabling sets the env var the daemon reads, not an empty value."""
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)
    target = tmp_path / "traces" / "ci.log"

    assert enable_debug_log(target) == str(target)

    assert os.environ[CIMOUNT_DEBUG_LOG_ENV] == str(target)


def test_enable_expands_the_user_prefix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A ~ path is expanded because the daemon cannot expand it itself."""
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert enable_debug_log("~/traces/ci.log") == str(tmp_path / "traces" / "ci.log")


def test_enable_makes_a_relative_path_absolute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative path is resolved now, never against the daemon's cwd."""
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)

    assert enable_debug_log("ci.log") == str(tmp_path / "ci.log")


def test_enable_creates_the_parent_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The parent is created because the daemon opens only the file."""
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)
    target = tmp_path / "missing" / "nested" / "ci.log"

    enable_debug_log(target)

    assert target.parent.is_dir()
    assert not target.exists()


def test_enable_refuses_a_blank_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank path never exports a value the daemon reads as no logging."""
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)

    with pytest.raises(ValueError):
        enable_debug_log("   ")

    assert CIMOUNT_DEBUG_LOG_ENV not in os.environ


def test_disable_removes_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling unsets the env var instead of blanking it."""
    monkeypatch.setenv(CIMOUNT_DEBUG_LOG_ENV, "/tmp/ci.log")

    disable_debug_log()

    assert CIMOUNT_DEBUG_LOG_ENV not in os.environ


def test_disable_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling an already disabled trace stays a no-op."""
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)

    disable_debug_log()

    assert CIMOUNT_DEBUG_LOG_ENV not in os.environ


def test_apply_enabled_without_path_uses_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Enabling without a chosen file falls back to the default path."""
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)
    paths = _paths(tmp_path)

    result = apply_debug_log(True, None, paths)

    assert result == str(tmp_path / "cache" / CIMOUNT_DEBUG_LOG_FILENAME)
    assert os.environ[CIMOUNT_DEBUG_LOG_ENV] == result


def test_apply_enabled_with_blank_path_uses_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cleared entry row behaves like no path at all."""
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)
    paths = _paths(tmp_path)
    (tmp_path / "cache").mkdir()

    result = apply_debug_log(True, "   ", paths)

    assert result == str(tmp_path / "cache" / CIMOUNT_DEBUG_LOG_FILENAME)


def test_apply_enabled_uses_the_chosen_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit path wins over the default and is returned for display."""
    monkeypatch.delenv(CIMOUNT_DEBUG_LOG_ENV, raising=False)
    chosen = tmp_path / "custom.log"

    result = apply_debug_log(True, chosen, _paths(tmp_path))

    assert result == str(chosen)
    assert os.environ[CIMOUNT_DEBUG_LOG_ENV] == str(chosen)


def test_apply_disabled_removes_the_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Disabling drops the env var even when a path is still remembered."""
    monkeypatch.setenv(CIMOUNT_DEBUG_LOG_ENV, str(tmp_path / "old.log"))

    result = apply_debug_log(False, str(tmp_path / "remembered.log"), _paths(tmp_path))

    assert result is None
    assert CIMOUNT_DEBUG_LOG_ENV not in os.environ
