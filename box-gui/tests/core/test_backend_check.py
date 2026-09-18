"""Unit tests for the backend gate and dependency probes (no GTK dependency)."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import box_gui.core.backend_check as backend_check_module
from box_gui.core.backend_check import (
    BackendStatus,
    DependencyStatus,
    check_backend,
    probe_dependencies,
)


def _ok_import() -> None:
    """Pretend the backend package imports cleanly."""
    return None


def test_compatible_on_exact_match(monkeypatch: Any) -> None:
    """An exact version match needs no setup."""
    monkeypatch.setattr(backend_check_module, "_import_box", _ok_import)
    monkeypatch.setattr(backend_check_module, "_installed_box_version", lambda: "26.9.43")

    status = check_backend("26.9.43")

    assert status.state == "compatible"
    assert status.needs_setup is False
    assert status.installed_version == "26.9.43"
    assert status.expected_version == "26.9.43"


def test_missing_when_backend_unimportable(monkeypatch: Any) -> None:
    """A missing backend routes to setup instead of guessing."""

    def _missing() -> None:
        raise ImportError("No module named 'box'")

    monkeypatch.setattr(backend_check_module, "_import_box", _missing)

    status = check_backend("26.9.43")

    assert status.state == "missing"
    assert status.needs_setup is True
    assert status.installed_version is None
    assert "box-rpg" in status.message


def test_error_on_broken_environment(monkeypatch: Any) -> None:
    """A broken (non-import) failure fails closed with the raw message."""

    def _broken() -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(backend_check_module, "_import_box", _broken)

    status = check_backend("26.9.43")

    assert status.state == "error"
    assert status.needs_setup is True
    assert status.message == "denied"


def test_missing_when_version_undeterminable(monkeypatch: Any) -> None:
    """An importable backend without metadata still routes to setup."""
    monkeypatch.setattr(backend_check_module, "_import_box", _ok_import)
    monkeypatch.setattr(backend_check_module, "_installed_box_version", lambda: None)

    status = check_backend("26.9.43")

    assert status.state == "missing"
    assert status.needs_setup is True


def test_mismatch_reports_both_versions(monkeypatch: Any) -> None:
    """A version skew names both sides without guessing compatibility."""
    monkeypatch.setattr(backend_check_module, "_import_box", _ok_import)
    monkeypatch.setattr(backend_check_module, "_installed_box_version", lambda: "26.9.42")

    status = check_backend("26.9.43")

    assert status.state == "mismatch"
    assert status.needs_setup is True
    assert "26.9.42" in status.message
    assert "26.9.43" in status.message


def test_error_when_metadata_probe_breaks(monkeypatch: Any) -> None:
    """A metadata failure other than absence fails closed verbatim."""

    def _broken() -> str | None:
        raise OSError("unreadable dist-info")

    monkeypatch.setattr(backend_check_module, "_import_box", _ok_import)
    monkeypatch.setattr(backend_check_module, "_installed_box_version", _broken)

    status = check_backend("26.9.43")

    assert status.state == "error"
    assert status.message == "unreadable dist-info"


def test_expected_version_defaults_to_app_info(monkeypatch: Any) -> None:
    """Without an explicit expectation the aligned version is used."""
    monkeypatch.setattr(backend_check_module, "_import_box", _ok_import)
    monkeypatch.setattr(backend_check_module, "_installed_box_version", lambda: "99.0.1")
    monkeypatch.setattr(backend_check_module, "get_expected_backend_version", lambda: "99.0.1")

    assert check_backend().state == "compatible"


def _fixed_probe(key: str, *, available: bool, required: bool = True) -> Any:
    """Build a dependency probe stub answering one fixed status."""

    def _probe() -> DependencyStatus:
        return DependencyStatus(
            key=key, label=key, available=available, required=required, detail="stub"
        )

    _probe.__name__ = f"_probe_{key}"
    return _probe


def test_probe_dependencies_collects_every_probe(monkeypatch: Any) -> None:
    """Detection aggregates python, pip, bwrap, gamemode, and icoextract in order."""
    monkeypatch.setattr(
        backend_check_module, "_probe_python", _fixed_probe("python", available=True)
    )
    monkeypatch.setattr(backend_check_module, "_probe_pip", _fixed_probe("pip", available=True))
    monkeypatch.setattr(
        backend_check_module, "_probe_bwrap", _fixed_probe("bwrap", available=False)
    )
    monkeypatch.setattr(
        backend_check_module,
        "_probe_gamemode",
        _fixed_probe("gamemode", available=False, required=False),
    )
    monkeypatch.setattr(
        backend_check_module,
        "_probe_icoextract",
        _fixed_probe("icoextract", available=False, required=False),
    )

    probed = probe_dependencies()

    assert [item.key for item in probed] == ["python", "pip", "bwrap", "gamemode", "icoextract"]
    assert [item.available for item in probed] == [True, True, False, False, False]
    assert probed[3].required is False
    assert probed[4].required is False


def test_probe_dependencies_degrades_per_probe(monkeypatch: Any) -> None:
    """One exploding probe degrades to unavailable instead of aborting."""

    def _boom() -> DependencyStatus:
        raise RuntimeError("probe exploded")

    _boom.__name__ = "_probe_pip"
    monkeypatch.setattr(
        backend_check_module, "_probe_python", _fixed_probe("python", available=True)
    )
    monkeypatch.setattr(backend_check_module, "_probe_pip", _boom)
    monkeypatch.setattr(backend_check_module, "_probe_bwrap", _fixed_probe("bwrap", available=True))
    monkeypatch.setattr(
        backend_check_module,
        "_probe_gamemode",
        _fixed_probe("gamemode", available=True, required=False),
    )
    monkeypatch.setattr(
        backend_check_module,
        "_probe_icoextract",
        _fixed_probe("icoextract", available=True, required=False),
    )

    probed = {item.key: item for item in probe_dependencies()}

    assert probed["_probe_pip"].available is False
    assert probed["_probe_pip"].detail == "probe exploded"
    assert probed["python"].available is True


def test_gamemode_probe_uses_the_backend_probe(monkeypatch: Any) -> None:
    """GameMode availability delegates to box.api.launch when present."""
    import box.api.launch as launch_api

    monkeypatch.setattr(launch_api, "is_gamemode_available", lambda: True)

    assert backend_check_module._is_gamemode_available() is True

    monkeypatch.setattr(launch_api, "is_gamemode_available", lambda: False)

    assert backend_check_module._is_gamemode_available() is False


def test_gamemode_probe_degrades_on_old_backends(monkeypatch: Any) -> None:
    """Old backends without the probe read as unavailable, never a crash."""
    import box.api.launch as launch_api

    monkeypatch.delattr(launch_api, "is_gamemode_available", raising=False)

    assert backend_check_module._is_gamemode_available() is False


def test_gamemode_probe_degrades_on_probe_errors(monkeypatch: Any) -> None:
    """A crashing probe reads as unavailable, never a crash."""
    import box.api.launch as launch_api

    def _boom() -> bool:
        raise RuntimeError("gamemoderun exploded")

    monkeypatch.setattr(launch_api, "is_gamemode_available", _boom)

    assert backend_check_module._is_gamemode_available() is False


def test_gamemode_probe_falls_back_to_host_without_backend(monkeypatch: Any) -> None:
    """Without a backend the host wrapper binaries decide, never a crash."""
    import box.api as box_api

    monkeypatch.delattr(box_api, "launch", raising=False)
    monkeypatch.setitem(sys.modules, "box.api.launch", None)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(os, "access", lambda path, mode: True)

    assert backend_check_module._is_gamemode_available() is True

    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.setattr(backend_check_module.shutil, "which", lambda name: None)

    assert backend_check_module._is_gamemode_available() is False


def test_icoextract_probe_maps_importability(monkeypatch: Any) -> None:
    """The icoextract probe is optional and never raises."""
    monkeypatch.setattr(backend_check_module.importlib.util, "find_spec", lambda name: object())

    probed = backend_check_module._probe_icoextract()

    assert (probed.key, probed.label, probed.available, probed.required) == (
        "icoextract",
        "icoextract",
        True,
        False,
    )

    monkeypatch.setattr(backend_check_module.importlib.util, "find_spec", lambda name: None)

    probed = backend_check_module._probe_icoextract()

    assert probed.available is False
    assert probed.required is False


def test_bwrap_probe_maps_install_problems(monkeypatch: Any, tmp_path: Path) -> None:
    """The bwrap probe mirrors the install.py problem codes."""
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(backend_check_module, "_BWRAP", fake)

    monkeypatch.setattr(backend_check_module, "_tool_runs", lambda _cmd: True)
    assert backend_check_module._probe_bwrap().available is True

    calls: list[list[str]] = []

    def _fail_userns(command: list[str]) -> bool:
        calls.append(command)
        return "--unshare-user" not in command

    monkeypatch.setattr(backend_check_module, "_tool_runs", _fail_userns)
    probed = backend_check_module._probe_bwrap()
    assert probed.available is False
    assert "namespaces" in probed.detail

    monkeypatch.setattr(backend_check_module, "_tool_runs", lambda _cmd: False)
    assert backend_check_module._probe_bwrap().detail == "installed but does not run"

    monkeypatch.setattr(backend_check_module, "_BWRAP", tmp_path / "absent-bwrap")
    assert backend_check_module._probe_bwrap().detail == "missing"


def test_pip_probe_uses_the_running_interpreter(monkeypatch: Any) -> None:
    """The pip probe shells the same interpreter that runs the install."""
    seen: list[list[str]] = []

    def _record(command: list[str]) -> bool:
        seen.append(command)
        return True

    monkeypatch.setattr(backend_check_module, "_tool_runs", _record)

    assert backend_check_module._probe_pip().available is True
    assert seen == [[sys.executable, "-I", "-m", "pip", "--version"]]


def test_backend_status_needs_setup() -> None:
    """Only the compatible state skips the setup page."""
    for state in ("missing", "mismatch", "error"):
        status = BackendStatus(
            state=state,  # type: ignore[arg-type]
            expected_version="26.9.43",
            installed_version=None,
            message="reason",
        )
        assert status.needs_setup is True
