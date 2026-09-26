"""Defaults repository tests for language and automatic update preferences."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from box.paths import AppPaths

from box_gui.core.defaults import DefaultsError, DefaultsRepository, RuntimeDefaults


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


def test_update_interval_round_trip(tmp_path: Path) -> None:
    """Each cadence code persists and reads back unchanged."""
    repository = _repository(tmp_path)
    for code in ("off", "12h", "daily", "2d", "3d", "weekly"):
        repository.set_update_interval(code)
        assert repository.load().update_interval == code


def test_update_check_timestamps_round_trip(tmp_path: Path) -> None:
    """Explicit check times persist as floats and read back unchanged."""
    repository = _repository(tmp_path)
    repository.record_appimage_check(1_700_000_000.0)
    loaded = repository.load()
    assert loaded.last_appimage_check_at == 1_700_000_000.0


def test_skipped_versions_round_trip(tmp_path: Path) -> None:
    """Skipped versions persist, and clearing stores None."""
    repository = _repository(tmp_path)
    repository.set_skipped_appimage_version("26.9.1")
    loaded = repository.load()
    assert loaded.skipped_appimage_version == "26.9.1"
    repository.set_skipped_appimage_version(None)
    loaded = repository.load()
    assert loaded.skipped_appimage_version is None


def test_update_setters_preserve_other_fields(tmp_path: Path) -> None:
    """Update setters keep language, runtime, and sibling update fields."""
    repository = _repository(tmp_path)
    repository.set_preferred_easyrpg_runtime("0.8.1")
    repository.set_preferred_language("es")
    repository.set_update_interval("daily")
    repository.record_appimage_check(1_700_000_000.0)
    repository.set_skipped_appimage_version("26.9.1")
    assert repository.load() == RuntimeDefaults(
        preferred_easyrpg_runtime="0.8.1",
        preferred_language="es",
        update_interval="daily",
        last_appimage_check_at=1_700_000_000.0,
        skipped_appimage_version="26.9.1",
    )
    repository.set_update_interval("off")
    assert repository.load().preferred_language == "es"
    assert repository.load().last_appimage_check_at == 1_700_000_000.0
    assert repository.load().skipped_appimage_version == "26.9.1"
    repository.set_preferred_language("en")
    assert repository.load().update_interval == "off"


def test_corrupt_file_heals_on_set_update_interval(tmp_path: Path) -> None:
    """Setting a cadence over garbage replaces it instead of failing."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text("not json", encoding="utf-8")
    repository.set_update_interval("daily")
    assert repository.load().update_interval == "daily"


def test_missing_update_fields_default_sensibly(tmp_path: Path) -> None:
    """Old files without update keys load weekly cadence and empty state."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text(
        json.dumps({"version": 1, "preferred_language": "es"}), encoding="utf-8"
    )
    assert repository.load() == RuntimeDefaults(preferred_language="es")


def test_unknown_interval_heals_to_weekly(tmp_path: Path) -> None:
    """Unknown cadence codes load as weekly instead of failing."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text(
        json.dumps({"version": 1, "update_interval": "fortnightly"}), encoding="utf-8"
    )
    assert repository.load().update_interval == "weekly"


def test_empty_skipped_versions_normalize_to_none(tmp_path: Path) -> None:
    """Empty skipped versions load as no skipped version."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text(
        json.dumps({"version": 1, "skipped_appimage_version": ""}),
        encoding="utf-8",
    )
    loaded = repository.load()
    assert loaded.skipped_appimage_version is None


@pytest.mark.parametrize("bad", ["yesterday", True, False, [], {}])
def test_bad_timestamps_raise(tmp_path: Path, bad: object) -> None:
    """Non-numeric timestamps fail validation instead of loading silently."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text(
        json.dumps({"version": 1, "last_appimage_check_at": bad}), encoding="utf-8"
    )
    with pytest.raises(DefaultsError):
        repository.load()


def test_int_timestamp_loads_as_float(tmp_path: Path) -> None:
    """Integer timestamps load as floats for a stable in-memory type."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text(
        json.dumps({"version": 1, "last_appimage_check_at": 1700000000}),
        encoding="utf-8",
    )
    assert repository.load().last_appimage_check_at == 1_700_000_000.0


def test_unknown_interval_normalizes_to_weekly_on_write(tmp_path: Path) -> None:
    """Unknown cadence codes store as weekly, matching read-time healing."""
    repository = _repository(tmp_path)
    repository.set_update_interval("fortnightly")
    assert repository.load().update_interval == "weekly"
    repository.set_update_interval("")
    assert repository.load().update_interval == "weekly"


def test_legacy_backend_keys_are_ignored(tmp_path: Path) -> None:
    """Old files with backend keys load cleanly, dropping those keys."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text(
        json.dumps(
            {
                "version": 1,
                "preferred_language": "es",
                "update_interval": "daily",
                "last_appimage_check_at": 1700000000,
                "last_backend_check_at": 1700000123,
                "skipped_appimage_version": "26.9.1",
                "skipped_backend_version": "1.2.3",
                "unknown_future_key": "kept-ignored",
            }
        ),
        encoding="utf-8",
    )
    loaded = repository.load()
    assert loaded == RuntimeDefaults(
        preferred_language="es",
        update_interval="daily",
        last_appimage_check_at=1_700_000_000.0,
        skipped_appimage_version="26.9.1",
    )


def test_save_omits_backend_keys(tmp_path: Path) -> None:
    """Persisted payloads carry only the AppImage update domain."""
    repository = _repository(tmp_path)
    repository.set_update_interval("daily")
    repository.record_appimage_check(1_700_000_000.0)
    repository.set_skipped_appimage_version("26.9.1")
    payload = json.loads(repository.defaults_file.read_text(encoding="utf-8"))
    assert "last_backend_check_at" not in payload
    assert "skipped_backend_version" not in payload
    assert payload["update_interval"] == "daily"


def test_ci_mount_debug_round_trip(tmp_path: Path) -> None:
    """The ci-mount trace switch and log file persist and read back."""
    repository = _repository(tmp_path)
    repository.set_ci_mount_debug(True, "/tmp/ci-mount.log")

    loaded = repository.load()
    assert loaded.ci_mount_debug_enabled is True
    assert loaded.ci_mount_debug_log == "/tmp/ci-mount.log"

    repository.set_ci_mount_debug(False, "/tmp/ci-mount.log")
    loaded = repository.load()
    assert loaded.ci_mount_debug_enabled is False
    assert loaded.ci_mount_debug_log == "/tmp/ci-mount.log"


def test_ci_mount_debug_omitted_log_keeps_the_default(
    tmp_path: Path,
) -> None:
    """A cleared path stores None so the cache-root default applies."""
    repository = _repository(tmp_path)
    repository.set_ci_mount_debug(True, "   ")

    loaded = repository.load()
    assert loaded.ci_mount_debug_enabled is True
    assert loaded.ci_mount_debug_log is None


def test_ci_mount_debug_without_log_stores_none(tmp_path: Path) -> None:
    """Enabling without a path keeps the file unset, never a blank string."""
    repository = _repository(tmp_path)
    repository.set_ci_mount_debug(True)

    assert repository.load().ci_mount_debug_log is None


def test_missing_ci_mount_fields_default_to_no_trace(tmp_path: Path) -> None:
    """Old files without the ci-mount keys load with logging off."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text(
        json.dumps({"version": 1, "preferred_language": "es"}), encoding="utf-8"
    )

    loaded = repository.load()
    assert loaded.ci_mount_debug_enabled is False
    assert loaded.ci_mount_debug_log is None


def test_empty_ci_mount_log_normalizes_to_none(tmp_path: Path) -> None:
    """An empty stored log loads as no log file."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text(
        json.dumps({"version": 1, "ci_mount_debug_enabled": True, "ci_mount_debug_log": ""}),
        encoding="utf-8",
    )

    loaded = repository.load()
    assert loaded.ci_mount_debug_enabled is True
    assert loaded.ci_mount_debug_log is None


@pytest.mark.parametrize("bad", ["yes", 1, 0, [], {}])
def test_bad_ci_mount_flag_raises(tmp_path: Path, bad: object) -> None:
    """Non-boolean trace flags fail validation instead of loading silently."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text(
        json.dumps({"version": 1, "ci_mount_debug_enabled": bad}), encoding="utf-8"
    )
    with pytest.raises(DefaultsError):
        repository.load()


def test_bad_ci_mount_log_raises(tmp_path: Path) -> None:
    """A non-string trace path fails validation."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text(
        json.dumps({"version": 1, "ci_mount_debug_log": 42}), encoding="utf-8"
    )
    with pytest.raises(DefaultsError):
        repository.load()


def test_ci_mount_debug_preserves_other_fields(tmp_path: Path) -> None:
    """The trace setter keeps language, runtime, and update preferences."""
    repository = _repository(tmp_path)
    repository.set_preferred_easyrpg_runtime("0.8.1")
    repository.set_preferred_language("es")
    repository.set_update_interval("daily")
    repository.set_ci_mount_debug(True, "/tmp/ci-mount.log")

    loaded = repository.load()
    assert loaded == RuntimeDefaults(
        preferred_easyrpg_runtime="0.8.1",
        preferred_language="es",
        update_interval="daily",
        ci_mount_debug_enabled=True,
        ci_mount_debug_log="/tmp/ci-mount.log",
    )


def test_save_persists_ci_mount_keys(tmp_path: Path) -> None:
    """The written payload carries both ci-mount fields for the next run."""
    repository = _repository(tmp_path)
    repository.set_ci_mount_debug(True, "/tmp/ci-mount.log")

    payload = json.loads(repository.defaults_file.read_text(encoding="utf-8"))
    assert payload["ci_mount_debug_enabled"] is True
    assert payload["ci_mount_debug_log"] == "/tmp/ci-mount.log"


def test_corrupt_file_heals_on_set_ci_mount_debug(tmp_path: Path) -> None:
    """Storing the trace over garbage replaces the file instead of failing."""
    repository = _repository(tmp_path)
    repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    repository.defaults_file.write_text("not json", encoding="utf-8")
    repository.set_ci_mount_debug(True, "/tmp/ci-mount.log")

    loaded = repository.load()
    assert loaded.ci_mount_debug_enabled is True
    assert loaded.ci_mount_debug_log == "/tmp/ci-mount.log"
