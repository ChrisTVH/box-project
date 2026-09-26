"""Frontend-owned default preferences (runtimes, language, automatic updates)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

from box.paths import AppPaths

__all__ = [
    "DEFAULT_UPDATE_INTERVAL",
    "UPDATE_INTERVAL_CODES",
    "DefaultsError",
    "DefaultsRepository",
    "RuntimeDefaults",
]

_DEFAULTS_VERSION = 1

# Update-check cadence codes shown in the General settings page, in UI order.
# Single source of codes: settings_dialog.py imports this instead of copying it.
UPDATE_INTERVAL_CODES: tuple[str, ...] = ("off", "12h", "daily", "2d", "3d", "weekly")
_UPDATE_INTERVALS: tuple[str, ...] = UPDATE_INTERVAL_CODES
DEFAULT_UPDATE_INTERVAL = "weekly"
_DEFAULT_UPDATE_INTERVAL = DEFAULT_UPDATE_INTERVAL


class DefaultsError(ValueError):
    """Raised when the frontend defaults file cannot be read or written."""


@dataclass(frozen=True, slots=True)
class RuntimeDefaults:
    """Global frontend defaults applied to new library entries."""

    preferred_easyrpg_runtime: str | None = None
    preferred_language: str | None = None
    # Automatic update preferences (check cadence, last check time, skip).
    # The backend has no independent release channel: the expected backend
    # derives from the embedded AppImage tag, so discovery, prompt, and skip
    # live only on the AppImage domain and no backend fields exist here.
    update_interval: str = _DEFAULT_UPDATE_INTERVAL
    last_appimage_check_at: float | None = None
    skipped_appimage_version: str | None = None
    # ci-mount daemon diagnosis: the trace switch and its log file. A None
    # log keeps the default under the launcher cache root, and the switch is
    # replayed into the process environment at startup (see
    # core.cimount_debug), so the daemon inherits it on the next launch.
    ci_mount_debug_enabled: bool = False
    ci_mount_debug_log: str | None = None


class DefaultsRepository:
    """JSON-backed defaults stored under the shared configuration root."""

    def __init__(self, paths: AppPaths) -> None:
        """Remember the paths used to locate the defaults file."""
        self._paths = paths
        self._file = paths.config_root / "defaults.json"

    @property
    def defaults_file(self) -> Path:
        """Return the JSON file backing the defaults."""
        return self._file

    def load(self) -> RuntimeDefaults:
        """Load defaults, or blank defaults when no file exists yet."""
        try:
            raw = self._file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return RuntimeDefaults()
        except OSError as exc:
            raise DefaultsError(f"cannot read defaults file {self._file}: {exc}") from exc
        try:
            payload: object = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DefaultsError(f"cannot parse defaults file {self._file}: {exc}") from exc
        return _decode_defaults(payload, self._file)

    def save(self, defaults: RuntimeDefaults) -> RuntimeDefaults:
        """Persist defaults atomically with user-only file permissions."""
        payload: dict[str, object] = {
            "version": _DEFAULTS_VERSION,
            "preferred_easyrpg_runtime": defaults.preferred_easyrpg_runtime,
            "preferred_language": defaults.preferred_language,
            "update_interval": defaults.update_interval,
            "last_appimage_check_at": defaults.last_appimage_check_at,
            "skipped_appimage_version": defaults.skipped_appimage_version,
            "ci_mount_debug_enabled": defaults.ci_mount_debug_enabled,
            "ci_mount_debug_log": defaults.ci_mount_debug_log,
        }
        content = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        _atomic_write_text(self._file, content)
        return defaults

    def set_preferred_easyrpg_runtime(self, version: str | None) -> RuntimeDefaults:
        """Store or clear the preferred EasyRPG Player version."""
        return self.save(replace(self._load_or_blank(), preferred_easyrpg_runtime=version))

    def set_preferred_language(self, language: str | None) -> RuntimeDefaults:
        """Store or clear the preferred UI language (None means system default)."""
        return self.save(replace(self._load_or_blank(), preferred_language=language))

    def set_update_interval(self, interval: str) -> RuntimeDefaults:
        """Store the automatic update-check cadence, keeping other fields."""
        # Normalize unknown codes to weekly on write, matching read-time healing.
        normalized = interval if interval in _UPDATE_INTERVALS else _DEFAULT_UPDATE_INTERVAL
        return self.save(replace(self._load_or_blank(), update_interval=normalized))

    def record_appimage_check(self, checked_at: float | None = None) -> RuntimeDefaults:
        """Record an AppImage update check time (defaults to now)."""
        return self.save(
            replace(
                self._load_or_blank(),
                last_appimage_check_at=checked_at if checked_at is not None else time.time(),
            )
        )

    def set_skipped_appimage_version(self, version: str | None) -> RuntimeDefaults:
        """Store or clear the skipped AppImage version, keeping other fields."""
        return self.save(replace(self._load_or_blank(), skipped_appimage_version=version))

    def set_ci_mount_debug(self, enabled: bool, log: str | None = None) -> RuntimeDefaults:
        """Store the ci-mount trace switch and log file, keeping other fields.

        A blank log stores None so the default cache-root path applies, and
        disabling keeps the previously chosen file so the developer does not
        retype it on the next diagnosis.
        """
        normalized = log.strip() if log is not None else None
        return self.save(
            replace(
                self._load_or_blank(),
                ci_mount_debug_enabled=bool(enabled),
                ci_mount_debug_log=normalized or None,
            )
        )

    def _load_or_blank(self) -> RuntimeDefaults:
        """Load defaults, healing unreadable files with blank defaults."""
        try:
            return self.load()
        except DefaultsError:
            return RuntimeDefaults()


def _decode_defaults(payload: object, source: Path) -> RuntimeDefaults:
    """Validate the top-level schema and convert it to RuntimeDefaults."""
    if not isinstance(payload, dict):
        raise DefaultsError(f"invalid defaults file {source}: top-level value must be an object")
    version = payload.get("version")
    if version != _DEFAULTS_VERSION:
        raise DefaultsError(f"unsupported defaults version in {source}: {version!r}")
    runtime_value = payload.get("preferred_easyrpg_runtime")
    if runtime_value is not None and not isinstance(runtime_value, str):
        raise DefaultsError(f"invalid defaults file {source}: bad preferred_easyrpg_runtime")
    if runtime_value == "":
        runtime_value = None
    language_value = payload.get("preferred_language")
    if language_value is not None and not isinstance(language_value, str):
        raise DefaultsError(f"invalid defaults file {source}: bad preferred_language")
    if language_value == "":
        language_value = None
    # Unknown or missing cadences heal to weekly so old/corrupt files stay usable.
    interval_value = payload.get("update_interval", _DEFAULT_UPDATE_INTERVAL)
    if not isinstance(interval_value, str) or interval_value not in _UPDATE_INTERVALS:
        interval_value = _DEFAULT_UPDATE_INTERVAL
    appimage_checked = _decode_timestamp(
        payload.get("last_appimage_check_at"), source, "last_appimage_check_at"
    )
    skipped_appimage = payload.get("skipped_appimage_version")
    if skipped_appimage is not None and not isinstance(skipped_appimage, str):
        raise DefaultsError(f"invalid defaults file {source}: bad skipped_appimage_version")
    if skipped_appimage == "":
        skipped_appimage = None
    # Missing ci-mount keys mean "no trace", matching a clean environment.
    ci_mount_enabled = _decode_flag(
        payload.get("ci_mount_debug_enabled"), source, "ci_mount_debug_enabled"
    )
    ci_mount_log = payload.get("ci_mount_debug_log")
    if ci_mount_log is not None and not isinstance(ci_mount_log, str):
        raise DefaultsError(f"invalid defaults file {source}: bad ci_mount_debug_log")
    if ci_mount_log == "":
        ci_mount_log = None
    # Unknown keys are ignored, including legacy backend keys
    # (last_backend_check_at, skipped_backend_version): the backend has no
    # independent release channel, so only the AppImage domain persists here.
    return RuntimeDefaults(
        preferred_easyrpg_runtime=runtime_value,
        preferred_language=language_value,
        update_interval=interval_value,
        last_appimage_check_at=appimage_checked,
        skipped_appimage_version=skipped_appimage,
        ci_mount_debug_enabled=ci_mount_enabled,
        ci_mount_debug_log=ci_mount_log,
    )


def _decode_flag(value: object, source: Path, field: str) -> bool:
    """Validate an optional on/off flag, defaulting to False when absent."""
    if value is None:
        return False
    if not isinstance(value, bool):
        raise DefaultsError(f"invalid defaults file {source}: bad {field}")
    return value


def _decode_timestamp(value: object, source: Path, field: str) -> float | None:
    """Validate an optional epoch timestamp, rejecting non-numeric values."""
    if value is None:
        return None
    # bool is an int subclass; reject it explicitly to keep timestamps numeric.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DefaultsError(f"invalid defaults file {source}: bad {field}")
    return float(value)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically with user-only file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".defaults-", dir=path.parent, text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
