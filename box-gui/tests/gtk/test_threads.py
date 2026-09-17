"""Backend-free threading primitive tests (no box import allowed)."""

# pyright: reportMissingImports=false

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

try:
    import box_gui.gtk.threads as threads_module

    _threads_available = True
except Exception:
    threads_module = None  # type: ignore[assignment]
    _threads_available = False

pytestmark = pytest.mark.skipif(not _threads_available, reason="gi unavailable")


def _block_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Purge backend modules so any box import fails like a fresh setup."""
    children = (
        "box_gui.gtk.threads",
        "box_gui.gtk.workers",
        "box_gui.pages.backend_setup_page",
    )
    for name in [
        name for name in sys.modules if name == "box" or name.startswith("box.") or name in children
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    # Reimporting a purged child must rebind the parent attribute too:
    # otherwise teardown restores a stale sys.modules entry while the
    # parent keeps the fresh module, splitting the package state.
    for dotted in children:
        parent_name, _, child = dotted.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            monkeypatch.delattr(parent, child, raising=False)
    monkeypatch.setitem(sys.modules, "box", None)


def test_threads_import_needs_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setup-page helper imports without pulling workers or box."""
    _block_backend(monkeypatch)

    from box_gui.gtk.threads import run_in_thread

    assert callable(run_in_thread)
    # Proves the purge forced a genuinely fresh import, not a cache hit.
    assert sys.modules["box_gui.gtk.threads"] is not threads_module
    assert "box_gui.gtk.workers" not in sys.modules


def test_probes_survive_missing_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dependency detection degrades per probe without the backend."""
    _block_backend(monkeypatch)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(os, "access", lambda path, mode: True)

    from box_gui.core.backend_check import probe_dependencies

    found = {item.key: item for item in probe_dependencies()}

    assert set(found) == {"python", "pip", "bwrap", "gamemode"}
    assert found["gamemode"].available is True

    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    found = {item.key: item for item in probe_dependencies()}

    assert found["gamemode"].available is False


def test_workers_reexports_the_same_helper() -> None:
    """Every other caller keeps working through the workers re-export."""
    box = pytest.importorskip("box")

    from box_gui.gtk import threads, workers

    assert workers.run_in_thread is threads.run_in_thread
    assert box is not None
