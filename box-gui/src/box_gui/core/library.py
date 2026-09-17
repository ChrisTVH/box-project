"""Frontend-owned game library persistence."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from box.paths import AppPaths

__all__ = [
    "GHOST_THRESHOLD",
    "LibraryEntry",
    "LibraryError",
    "LibraryRepository",
    "is_ghost",
]

GHOST_THRESHOLD: int = 3
"""Consecutive missing-folder sightings before an entry counts as a ghost.

Hardcoded on purpose: there is no settings UI for it. Below the
threshold a missing folder stays a silent normal row; at or above it
the presentation layers dim the row and offer relocation instead of
deleting anything.
"""

_LIBRARY_VERSION = 6

ReorderDirection = Literal["up", "down", "top", "bottom"]


class LibraryError(ValueError):
    """Raised when the frontend library file cannot be read or written."""


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    """One game remembered by the frontend library."""

    path: Path
    display_name: str
    order: int
    preferred_runtime: str | None
    preferred_sdk: bool
    copy_root_files: tuple[str, ...]
    engine: str | None = None
    allow_network: bool = False
    allow_game_writes: bool = False
    allow_x11: bool = False
    use_gamemode: bool = False
    use_ci_mount: bool = False
    icon_path: Path | None = None
    missing_streak: int = 0


def is_ghost(entry: LibraryEntry) -> bool:
    """Return True when a missing streak reached the ghost threshold."""
    return entry.missing_streak >= GHOST_THRESHOLD


def _probe_present(path: Path) -> bool:
    """Advisory narrow check: True only while path is a directory.

    Presentation state only, never a security boundary: inspect()
    and launch() still run the backend's real validation regardless.
    """
    try:
        return Path(path).is_dir()
    except OSError:
        return False


class LibraryRepository:
    """JSON-backed library stored under the shared configuration root."""

    def __init__(self, paths: AppPaths) -> None:
        """Remember the paths used to locate the library file."""
        self._paths = paths
        self._file = paths.config_root / "library.json"

    @property
    def library_file(self) -> Path:
        """Return the JSON file backing the library."""
        return self._file

    def load(self) -> tuple[LibraryEntry, ...]:
        """Load entries sorted by order, or an empty tuple when no file exists."""
        try:
            raw = self._file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise LibraryError(f"cannot read library file {self._file}: {exc}") from exc
        try:
            payload: object = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LibraryError(f"cannot parse library file {self._file}: {exc}") from exc
        return _decode_library(payload, self._file)

    def save(self, entries: Sequence[LibraryEntry]) -> None:
        """Persist entries atomically with user-only file permissions."""
        ordered = sorted(entries, key=lambda entry: entry.order)
        payload: dict[str, object] = {
            "version": _LIBRARY_VERSION,
            "entries": [_encode_entry(entry) for entry in ordered],
        }
        content = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        _atomic_write_text(self._file, content)

    def add(
        self,
        path: Path,
        display_name: str,
        engine: str | None = None,
        preferred_runtime: str | None = None,
    ) -> LibraryEntry:
        """Append a game with default runtime options and return the new entry."""
        entries = list(self.load())
        for existing in entries:
            if existing.path == path:
                raise LibraryError(f"game is already in the library: {path}")
        next_order = max((entry.order for entry in entries), default=-1) + 1
        created = LibraryEntry(
            path=path,
            display_name=display_name,
            order=next_order,
            preferred_runtime=preferred_runtime,
            preferred_sdk=False,
            copy_root_files=(),
            engine=engine,
            allow_network=False,
            allow_game_writes=False,
            allow_x11=False,
            use_gamemode=False,
            use_ci_mount=False,
            missing_streak=0,
        )
        entries.append(created)
        self.save(tuple(entries))
        return created

    def remove(self, entry: LibraryEntry) -> None:
        """Remove the entry matching the given path, ignoring unknown paths."""
        remaining = [stored for stored in self.load() if stored.path != entry.path]
        self.save(tuple(remaining))

    def reorder(self, entry: LibraryEntry, direction: ReorderDirection) -> tuple[LibraryEntry, ...]:
        """Move one entry and renumber orders sequentially from zero."""
        loaded = list(self.load())
        index: int | None = None
        for position, stored in enumerate(loaded):
            if stored.path == entry.path:
                index = position
                break
        if index is None:
            raise LibraryError(f"game is not in the library: {entry.path}")
        if direction == "up":
            if index > 0:
                loaded[index - 1], loaded[index] = loaded[index], loaded[index - 1]
        elif direction == "down":
            if index < len(loaded) - 1:
                loaded[index + 1], loaded[index] = loaded[index], loaded[index + 1]
        elif direction == "top":
            item = loaded.pop(index)
            loaded.insert(0, item)
        elif direction == "bottom":
            item = loaded.pop(index)
            loaded.append(item)
        else:
            raise LibraryError(f"unknown reorder direction: {direction}")
        reordered = [
            LibraryEntry(
                path=stored.path,
                display_name=stored.display_name,
                order=position,
                preferred_runtime=stored.preferred_runtime,
                preferred_sdk=stored.preferred_sdk,
                copy_root_files=stored.copy_root_files,
                engine=stored.engine,
                allow_network=stored.allow_network,
                allow_game_writes=stored.allow_game_writes,
                allow_x11=stored.allow_x11,
                use_gamemode=stored.use_gamemode,
                use_ci_mount=stored.use_ci_mount,
                icon_path=stored.icon_path,
                missing_streak=stored.missing_streak,
            )
            for position, stored in enumerate(loaded)
        ]
        self.save(tuple(reordered))
        return tuple(reordered)

    def update(self, entry: LibraryEntry) -> LibraryEntry:
        """Persist display-name, runtime, SDK, file, icon, permission, GameMode, mount, and streak edits.

        Entries normally match by path. When the path itself changed (the
        Locate-folder flow points a ghost at a new folder), the entry
        matches by its unique order instead so the row keeps its slot.
        Relocating onto a path owned by another entry raises LibraryError.
        """
        loaded = list(self.load())
        path_position: int | None = None
        for index, stored in enumerate(loaded):
            if stored.path == entry.path:
                path_position = index
                break
        order_positions = [
            index for index, stored in enumerate(loaded) if stored.order == entry.order
        ]
        order_position = order_positions[0] if len(order_positions) == 1 else None
        if (
            path_position is not None
            and order_position is not None
            and path_position != order_position
        ):
            raise LibraryError(f"game is already in the library: {entry.path}")
        position = path_position if path_position is not None else order_position
        if position is None:
            raise LibraryError(f"game is not in the library: {entry.path}")
        stored = loaded[position]
        merged = LibraryEntry(
            path=entry.path,
            display_name=entry.display_name,
            order=stored.order,
            preferred_runtime=entry.preferred_runtime,
            preferred_sdk=entry.preferred_sdk,
            copy_root_files=entry.copy_root_files,
            engine=entry.engine if entry.engine is not None else stored.engine,
            allow_network=entry.allow_network,
            allow_game_writes=entry.allow_game_writes,
            allow_x11=entry.allow_x11,
            use_gamemode=entry.use_gamemode,
            use_ci_mount=entry.use_ci_mount,
            icon_path=entry.icon_path,
            missing_streak=entry.missing_streak,
        )
        loaded[position] = merged
        self.save(tuple(loaded))
        return merged

    def note_missing_presentation_state(self) -> tuple[LibraryEntry, ...]:
        """Refresh missing streaks from a narrow folder probe, saving only if changed.

        Each entry gets one advisory ``is_dir`` check: a hit increments its
        missing streak, a success resets it to zero. This never inspects
        games and never raises ``GameValidationError``; only the library
        remembers the outcome, game files are never touched. Unlike
        prune_missing, nothing is ever deleted here.
        """
        entries = self.load()
        refreshed: list[LibraryEntry] = []
        changed = False
        for entry in entries:
            present = _probe_present(entry.path)
            if present:
                if entry.missing_streak != 0:
                    refreshed.append(replace(entry, missing_streak=0))
                    changed = True
                else:
                    refreshed.append(entry)
            else:
                refreshed.append(replace(entry, missing_streak=entry.missing_streak + 1))
                changed = True
        if changed:
            self.save(tuple(refreshed))
        return tuple(refreshed)

    def note_missing_entry(self, entry: LibraryEntry) -> tuple[LibraryEntry, bool]:
        """Record one advisory sighting for a single entry.

        Probes ``entry.path`` with ``is_dir`` only (never inspect, never
        ``GameValidationError``) and persists the streak change. Returns
        the stored entry and whether the folder is present. Entries
        unknown to the file are returned untouched with their probe
        outcome and never trigger a save, so callers can still block
        on a missing folder they cannot track.
        """
        present = _probe_present(entry.path)
        stored_entries = list(self.load())
        for index, stored in enumerate(stored_entries):
            if stored.path != entry.path:
                continue
            updated = replace(stored, missing_streak=0 if present else stored.missing_streak + 1)
            if updated.missing_streak == stored.missing_streak:
                return updated, present
            stored_entries[index] = updated
            self.save(tuple(stored_entries))
            return updated, present
        return entry, present

    def update_streak(self) -> tuple[LibraryEntry, ...]:
        """Alias for note_missing_presentation_state for shorter call sites."""
        return self.note_missing_presentation_state()

    def prune_missing(self) -> tuple[LibraryEntry, ...]:
        """Drop entries whose game directory no longer exists without deleting files."""
        entries = self.load()
        kept: list[LibraryEntry] = []
        removed: list[LibraryEntry] = []
        for entry in entries:
            try:
                resolved = entry.path.resolve(strict=True)
            except FileNotFoundError:
                removed.append(entry)
                continue
            except OSError as exc:
                raise LibraryError(f"cannot inspect library path {entry.path}: {exc}") from exc
            try:
                is_directory = stat.S_ISDIR(resolved.stat().st_mode)
            except FileNotFoundError:
                removed.append(entry)
                continue
            except OSError as exc:
                raise LibraryError(f"cannot inspect library path {entry.path}: {exc}") from exc
            if is_directory:
                kept.append(entry)
            else:
                removed.append(entry)
        if removed:
            self.save(tuple(kept))
        return tuple(removed)


def _decode_library(payload: object, source: Path) -> tuple[LibraryEntry, ...]:
    """Validate the top-level schema and return entries sorted by order."""
    if not isinstance(payload, dict):
        raise LibraryError(f"invalid library file {source}: top-level value must be an object")
    version = payload.get("version")
    if version not in (1, 2, 3, 4, 5, 6):
        raise LibraryError(f"unsupported library version in {source}: {version!r}")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise LibraryError(f"invalid library file {source}: entries must be a list")
    entries = [_decode_entry(item, source, number) for number, item in enumerate(raw_entries)]
    entries.sort(key=lambda entry: entry.order)
    return tuple(entries)


def _decode_entry(raw: object, source: Path, number: int) -> LibraryEntry:
    """Validate one entry mapping and convert it to a LibraryEntry."""
    if not isinstance(raw, dict):
        raise LibraryError(f"invalid library entry #{number} in {source}: must be an object")
    path_value = raw.get("path")
    display_value = raw.get("display_name")
    order_value = raw.get("order")
    runtime_value = raw.get("preferred_runtime")
    sdk_value = raw.get("preferred_sdk")
    files_value = raw.get("copy_root_files")
    engine_value = raw.get("engine")
    network_value = raw.get("allow_network", False)
    writes_value = raw.get("allow_game_writes", False)
    x11_value = raw.get("allow_x11", False)
    gamemode_value = raw.get("use_gamemode", False)
    ci_mount_value = raw.get("use_ci_mount", False)
    streak_value = raw.get("missing_streak", 0)
    icon_value = raw.get("icon_path")
    if not isinstance(path_value, str) or not path_value:
        raise LibraryError(f"invalid library entry #{number} in {source}: bad path")
    if not isinstance(display_value, str):
        raise LibraryError(f"invalid library entry #{number} in {source}: bad display_name")
    if not isinstance(order_value, int) or isinstance(order_value, bool):
        raise LibraryError(f"invalid library entry #{number} in {source}: bad order")
    if runtime_value is not None and not isinstance(runtime_value, str):
        raise LibraryError(f"invalid library entry #{number} in {source}: bad preferred_runtime")
    if not isinstance(sdk_value, bool):
        raise LibraryError(f"invalid library entry #{number} in {source}: bad preferred_sdk")
    if not isinstance(files_value, list) or any(not isinstance(name, str) for name in files_value):
        raise LibraryError(f"invalid library entry #{number} in {source}: bad copy_root_files")
    if engine_value is not None and not isinstance(engine_value, str):
        raise LibraryError(f"invalid library entry #{number} in {source}: bad engine")
    if engine_value == "":
        engine_value = None
    if icon_value is not None and not isinstance(icon_value, str):
        raise LibraryError(f"invalid library entry #{number} in {source}: bad icon_path")
    if icon_value == "":
        icon_value = None
    for label, value in (
        ("allow_network", network_value),
        ("allow_game_writes", writes_value),
        ("allow_x11", x11_value),
        ("use_gamemode", gamemode_value),
        ("use_ci_mount", ci_mount_value),
    ):
        if not isinstance(value, bool):
            raise LibraryError(f"invalid library entry #{number} in {source}: bad {label}")
    if not isinstance(streak_value, int) or isinstance(streak_value, bool) or streak_value < 0:
        raise LibraryError(f"invalid library entry #{number} in {source}: bad missing_streak")
    return LibraryEntry(
        path=Path(path_value),
        display_name=display_value,
        order=order_value,
        preferred_runtime=runtime_value,
        preferred_sdk=sdk_value,
        copy_root_files=tuple(files_value),
        engine=engine_value,
        allow_network=network_value,
        allow_game_writes=writes_value,
        allow_x11=x11_value,
        use_gamemode=gamemode_value,
        use_ci_mount=ci_mount_value,
        icon_path=Path(icon_value) if icon_value is not None else None,
        missing_streak=streak_value,
    )


def _encode_entry(entry: LibraryEntry) -> dict[str, object]:
    """Convert one entry to its JSON-serializable mapping."""
    return {
        "path": str(entry.path),
        "display_name": entry.display_name,
        "order": entry.order,
        "preferred_runtime": entry.preferred_runtime,
        "preferred_sdk": entry.preferred_sdk,
        "copy_root_files": list(entry.copy_root_files),
        "engine": entry.engine,
        "allow_network": entry.allow_network,
        "allow_game_writes": entry.allow_game_writes,
        "allow_x11": entry.allow_x11,
        "use_gamemode": entry.use_gamemode,
        "use_ci_mount": entry.use_ci_mount,
        "icon_path": str(entry.icon_path) if entry.icon_path is not None else None,
        "missing_streak": entry.missing_streak,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically with user-only file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".library-", dir=path.parent, text=True)
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
