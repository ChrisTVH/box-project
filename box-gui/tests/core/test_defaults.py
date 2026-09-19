"""Defaults repository tests for the preferred language preference."""

from __future__ import annotations

import json
from pathlib import Path

from box.paths import AppPaths

from box_gui.core.defaults import DefaultsRepository, RuntimeDefaults


def _repository(tmp_path: Path) -> DefaultsRepository:
    """Build an isolated repository under tmp_path."""
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    return DefaultsRepository(paths)


def test_load_missing_file_returns_blank(tmp_path: Path) -> None:
    """A first run without defaults.json reports no preferences."""
    assert _repository(tmp_path).load() == RuntimeDefaults()


def test_set_language_round_trip(tmp_path: Path) -> None:
    """Storing a language persists it and reads back unchanged."""
    repository = _repository(tmp_path)
    repository.set_preferred_language("es")
    assert repository.load().preferred_language == "es"
    repository.set_preferred_language(None)
    assert repository.load().preferred_language is None


def test_setters_preserve_each_other(tmp_path: Path) -> None:
    """Setting one preference keeps the other intact."""
    repository = _repository(tmp_path)
    repository.set_preferred_easyrpg_runtime("0.8.1")
    repository.set_preferred_language("es")
    assert repository.load() == RuntimeDefaults(
        preferred_easyrpg_runtime="0.8.1", preferred_language="es"
    )
    repository.set_preferred_easyrpg_runtime(None)
    assert repository.load() == RuntimeDefaults(preferred_language="es")


def test_empty_language_normalizes_to_none(tmp_path: Path) -> None:
    """Empty language strings load as the system default."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text(
        json.dumps({"version": 1, "preferred_language": ""}), encoding="utf-8"
    )
    assert repository.load().preferred_language is None


def test_corrupt_file_heals_on_set(tmp_path: Path) -> None:
    """Setting a preference over garbage replaces it instead of failing."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text("not json", encoding="utf-8")
    repository.set_preferred_language("en")
    assert repository.load().preferred_language == "en"
