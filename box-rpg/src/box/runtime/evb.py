"""Source-keyed unpacking of packed single-executable game directories."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import cast

from box.engines.registry import default_registry
from box.errors import GameValidationError, RuntimeError
from box.games.identity import game_id
from box.paths import AppPaths
from box.runtime import evb_unpack
from box.runtime.evb_unpack import EvbProgressCallback
from box.runtime.security import cache_lock
from box.utils.i18n import _

__all__ = [
    "default_tree_validator",
    "ensure_unpacked",
    "find_packed_executable",
]

#: Profile child holding the unpacked game tree, beside sandbox/.
PROFILE_GAME_DIRNAME = "game"

#: Profile child recording the packed source fingerprint for staleness checks.
PROFILE_MARKER_NAME = ".evb.json"

#: Prefix for private unpack staging directories inside one profile.
PROFILE_STAGING_PREFIX = ".game-unpack-"

#: Backup directory holding the previous game tree during atomic re-publish.
PROFILE_BACKUP_NAME = ".game-backup"

#: Previous-release cache component scanned once for migration, then abandoned.
_LEGACY_CACHE_COMPONENT = "evb"

#: Upper bound for legacy entries inspected during one migration scan.
_LEGACY_SCAN_LIMIT = 256

#: Upper bound for carried save directories and visited entries per re-unpack.
_MAX_SAVE_DIRECTORIES = 32
_MAX_SAVE_SCAN_ENTRIES = 5000

#: Largest fingerprint marker accepted when checking staleness.
_MAX_MARKER_BYTES = 4096

#: Largest packed source accepted for hashing and extraction.
MAX_EVB_SOURCE_BYTES = 1024 * 1024 * 1024

#: Game layouts that rule out a packed single-executable directory.
_UNPACKED_LAYOUT_NAMES = frozenset({"www", "data"})

TreeValidator = Callable[[Path], bool]


def default_tree_validator(path: Path) -> bool:
    """Accept extracted trees detectable as a supported game engine."""
    return default_registry().detect(path) is not None


def find_packed_executable(root: Path) -> Path | None:
    """Return the single packed-executable candidate below one directory.

    A candidate directory holds exactly one regular non-symlink ``*.exe``
    under any basename, has no ``www/`` or ``Data/``/``data/`` entries, and
    is not already detectable as an unpacked game. Returns None for every
    non-candidate without raising for ordinary filesystem conditions.
    """
    try:
        with os.scandir(root) as entries:
            items = list(entries)
    except OSError:
        return None
    executables = [
        entry
        for entry in items
        if entry.name.lower().endswith(".exe") and entry.is_file(follow_symlinks=False)
    ]
    if len(executables) != 1:
        return None
    for entry in items:
        if entry.name.lower() in _UNPACKED_LAYOUT_NAMES:
            return None
    try:
        if default_registry().detect(root) is not None:
            return None
    except OSError, GameValidationError, ValueError:
        return None
    return Path(executables[0].path)


def ensure_unpacked(
    paths: AppPaths,
    source_exe: Path,
    *,
    validator: TreeValidator | None = None,
    progress: EvbProgressCallback | None = None,
) -> Path:
    """Unpack one packed executable into its source-keyed profile game tree.

    The tree lives at ``profiles/<game_id(source root)>/game/``, beside the
    persistent ``sandbox/`` profile, so sessions, saves, and profiles survive
    executable replacements. A ``.evb.json`` marker records the unpacked
    source fingerprint (content hash, size, modification time): a matching
    marker reuses the tree, while a changed source re-unpacks over the same
    directory with save directories carried forward. Previous-release
    ``cache/evb/`` entries matching the current source migrate in place the
    first time. Publication holds the cooperative per-profile cache lock
    while staging into a private temporary directory and atomically renaming
    it over the final name. Accepted trees must validate for a supported
    engine unless the caller supplies its own validator.
    """
    check = default_tree_validator if validator is None else validator
    digest, size, modified = _identify_source(source_exe)
    fingerprint = (digest, size, modified)
    identifier = _profile_identifier(source_exe)
    profile_descriptor = paths.open_or_create_private_cache_directory("profiles", identifier)
    try:
        with cache_lock(profile_descriptor, PROFILE_GAME_DIRNAME):
            managed_profile = paths.ensure_managed_profile_path(paths.profiles_root / identifier)
            game_path = paths.ensure_managed_profile_game_path(
                managed_profile / PROFILE_GAME_DIRNAME
            )
            marker_path = managed_profile / PROFILE_MARKER_NAME
            if _marker_matches(marker_path, fingerprint) and _is_usable_tree(game_path):
                _check_source_unchanged(source_exe, size, modified)
                return game_path
            if not _is_usable_tree(game_path):
                migrated = _migrate_legacy_entry(
                    paths, source_exe, game_path, marker_path, fingerprint, check
                )
                if migrated is not None:
                    _check_source_unchanged(source_exe, size, modified)
                    _check_source_fingerprint(source_exe, fingerprint)
                    return paths.ensure_managed_profile_game_path(migrated)
            return _publish_unpacked(
                paths, source_exe, game_path, marker_path, fingerprint, check, progress
            )
    except OSError as exc:
        raise RuntimeError(
            _("cannot unpack game executable {path}: {error}").format(
                path=str(source_exe), error=exc
            )
        ) from exc
    finally:
        os.close(profile_descriptor)


def _publish_unpacked(
    paths: AppPaths,
    source_exe: Path,
    game_path: Path,
    marker_path: Path,
    fingerprint: tuple[str, int, int],
    check: TreeValidator,
    progress: EvbProgressCallback | None,
) -> Path:
    """Stage a fresh tree, carry saves forward, and publish it atomically.

    Publication keeps the previous tree in a sibling backup directory while
    the fresh staging tree moves into place: rename the old tree aside,
    replace staging over the final name, then drop the backup. A failure
    before the move restores the backup when the final name is missing, so
    a failed re-unpack never loses the previous tree with its saves.
    """
    _digest, size, modified = fingerprint
    _drop_stale_staging(game_path.parent)
    # Stage below the real profile path (not a /proc/self/fd alias) so
    # engine detection can inspect the tree without symlink components.
    staging = Path(tempfile.mkdtemp(prefix=PROFILE_STAGING_PREFIX, dir=str(game_path.parent)))
    backup: Path | None = None
    try:
        evb_unpack.unpack_packed_executable(
            source_exe,
            staging,
            validator=check,
            progress=progress,
        )
        _check_source_unchanged(source_exe, size, modified)
        _check_source_fingerprint(source_exe, fingerprint)
        if _is_usable_tree(game_path):
            _carry_saves(game_path, staging)
            backup_path = game_path.parent / PROFILE_BACKUP_NAME
            if os.path.lexists(backup_path):
                if backup_path.is_dir() and not backup_path.is_symlink():
                    shutil.rmtree(backup_path, ignore_errors=True)
                else:
                    with suppress(OSError):
                        os.unlink(backup_path)
            os.rename(game_path, backup_path)
            backup = backup_path
            try:
                os.replace(staging, game_path)
            except BaseException:
                with suppress(OSError):
                    if not os.path.lexists(game_path) and os.path.lexists(backup):
                        os.rename(backup, game_path)
                        backup = None
                raise
            shutil.rmtree(backup, ignore_errors=True)
            backup = None
        elif os.path.lexists(game_path):
            os.unlink(game_path)
            try:
                os.replace(staging, game_path)
            except OSError:
                with suppress(OSError):
                    if not os.path.lexists(game_path):
                        os.rename(staging, game_path)
                raise
        else:
            try:
                os.replace(staging, game_path)
            except OSError:
                with suppress(OSError):
                    if not os.path.lexists(game_path):
                        os.rename(staging, game_path)
                raise
        _write_marker(marker_path, fingerprint)
    except BaseException:
        if backup is not None:
            if not os.path.lexists(game_path) and os.path.lexists(backup):
                with suppress(OSError):
                    os.rename(backup, game_path)
            elif os.path.lexists(game_path):
                shutil.rmtree(backup, ignore_errors=True)
        if os.path.lexists(staging) and (os.path.lexists(game_path) or backup is None):
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return paths.ensure_managed_profile_game_path(game_path)


def _migrate_legacy_entry(
    paths: AppPaths,
    source_exe: Path,
    game_path: Path,
    marker_path: Path,
    fingerprint: tuple[str, int, int],
    check: TreeValidator,
) -> Path | None:
    """Adopt a matching previous-release cache entry into the profile.

    Previous releases keyed unpacked trees by content hash, size, and
    modification time below ``cache/evb/``. When one entry matches the
    current source it is renamed into place, so saves stored inside travel
    with it; the legacy directory goes away when empty. Entries that match
    nothing are left for a fresh unpack, and there is no permanent reader
    for the old layout beyond this one-shot migration. Adoption requires
    both a matching marker and engine validation; invalid trees fall
    through to a fresh unpack. A lost race for the same legacy entry
    (missing source on rename) also falls through instead of failing.
    """
    match = _find_legacy_match(paths, fingerprint)
    if match is None:
        return None
    if not _marker_matches(match / PROFILE_MARKER_NAME, fingerprint):
        return None
    try:
        accepted = check(match)
    except Exception:
        return None
    if not accepted:
        return None
    try:
        os.rename(match, game_path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(
            _("cannot unpack game executable {path}: {error}").format(
                path=str(source_exe), error=exc
            )
        ) from exc
    _write_marker(marker_path, fingerprint)
    with suppress(OSError):
        os.rmdir(paths.cache_root / _LEGACY_CACHE_COMPONENT)
    return game_path


def _find_legacy_match(paths: AppPaths, fingerprint: tuple[str, int, int]) -> Path | None:
    """Return one legacy entry matching the current source, if any."""
    digest, size, modified = fingerprint
    expected = f"{digest}-{size}-{modified}"
    legacy_root = paths.cache_root / _LEGACY_CACHE_COMPONENT
    try:
        with os.scandir(legacy_root) as entries:
            for index, entry in enumerate(entries):
                if index >= _LEGACY_SCAN_LIMIT:
                    break
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not is_dir or entry.name.startswith("."):
                    continue
                if entry.name == expected or _marker_matches(
                    Path(entry.path) / PROFILE_MARKER_NAME, fingerprint
                ):
                    return Path(entry.path)
    except OSError:
        return None
    return None


def _carry_saves(previous: Path, staging: Path) -> None:
    """Copy save directories from a stale tree into a fresh unpack.

    Engine save locations always sit below a directory named exactly
    ``save`` (``www/save`` for MV, the game root for MZ and EasyRPG), so
    every such directory travels forward and player progress survives an
    executable replacement. Only regular files and directories travel;
    symlinks, FIFOs, sockets, and other specials are skipped without
    blocking. Failures abort before publication, leaving the previous
    tree untouched.
    """
    for relative in _save_directories(previous):
        _copy_save_tree(previous / relative, staging / relative)


def _copy_save_tree(source: Path, destination: Path) -> None:
    """Copy one save directory carrying only regular files and dirs."""
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with os.scandir(source) as entries:
            children = list(entries)
    except OSError:
        return
    for entry in children:
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISLNK(entry_stat.st_mode):
            continue
        if stat.S_ISDIR(entry_stat.st_mode):
            _copy_save_tree(Path(entry.path), destination / entry.name)
        elif stat.S_ISREG(entry_stat.st_mode):
            _copy_regular_file(Path(entry.path), destination / entry.name)
        else:
            continue


def _copy_regular_file(source: Path, destination: Path) -> None:
    """Copy one regular save file without following symlinks or blocking."""
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return
    try:
        entry = os.fstat(source_descriptor)
        if not stat.S_ISREG(entry.st_mode):
            return
        os.set_blocking(source_descriptor, True)
        try:
            destination_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            return
        try:
            with (
                os.fdopen(source_descriptor, "rb", closefd=False) as origin,
                os.fdopen(destination_descriptor, "wb", closefd=False) as target,
            ):
                while chunk := origin.read(65536):
                    target.write(chunk)
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)


def _save_directories(tree: Path) -> list[Path]:
    """List save directories below one tree without following symlinks."""
    found: list[Path] = []
    pending: list[Path] = [Path(".")]
    seen = 0
    while pending and len(found) < _MAX_SAVE_DIRECTORIES and seen < _MAX_SAVE_SCAN_ENTRIES:
        relative = pending.pop()
        seen += 1
        try:
            with os.scandir(tree / relative) as entries:
                children = list(entries)
        except OSError:
            continue
        for entry in children:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if not is_dir:
                continue
            child = relative / entry.name
            if entry.name == "save":
                found.append(child)
            else:
                pending.append(child)
    return found


def _drop_stale_staging(profile_dir: Path) -> None:
    """Remove leftover staging from crashed runs while the old tree stands.

    Runs under the per-profile lock, so only this profile writes here and
    any ``.game-unpack-*`` directory is a previous failed run. Litter is
    kept when no game tree stands, since it may hold the only copy of
    rescued saves after a failed publication. A leftover backup directory
    is restored when the game tree is missing (crash between rename-aside
    and replace) and dropped when the game tree stands (crash between
    replace and backup removal).
    """
    try:
        with os.scandir(profile_dir) as entries:
            names = [
                entry.name
                for entry in entries
                if entry.name.startswith(PROFILE_STAGING_PREFIX)
                or entry.name in (PROFILE_GAME_DIRNAME, PROFILE_BACKUP_NAME)
            ]
    except OSError:
        return
    game_present = PROFILE_GAME_DIRNAME in names
    backup_present = PROFILE_BACKUP_NAME in names
    if game_present:
        for name in names:
            if not name.startswith(PROFILE_STAGING_PREFIX):
                continue
            with suppress(OSError):
                candidate = profile_dir / name
                if candidate.is_dir() and not candidate.is_symlink():
                    shutil.rmtree(candidate, ignore_errors=True)
        if backup_present:
            with suppress(OSError):
                backup = profile_dir / PROFILE_BACKUP_NAME
                if backup.is_dir() and not backup.is_symlink():
                    shutil.rmtree(backup, ignore_errors=True)
                elif os.path.lexists(backup):
                    os.unlink(backup)
        return
    if backup_present:
        backup = profile_dir / PROFILE_BACKUP_NAME
        target = profile_dir / PROFILE_GAME_DIRNAME
        try:
            if backup.is_dir() and not backup.is_symlink():
                os.rename(backup, target)
                for name in names:
                    if not name.startswith(PROFILE_STAGING_PREFIX):
                        continue
                    with suppress(OSError):
                        candidate = profile_dir / name
                        if candidate.is_dir() and not candidate.is_symlink():
                            shutil.rmtree(candidate, ignore_errors=True)
                return
        except OSError:
            pass
        return


def _marker_matches(marker: Path, fingerprint: tuple[str, int, int]) -> bool:
    """Return whether the staleness marker records the current source."""
    digest, size, modified = fingerprint
    try:
        if os.path.islink(marker):
            return False
        raw = marker.read_bytes()
    except OSError:
        return False
    if len(raw) > _MAX_MARKER_BYTES:
        return False
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except UnicodeError, ValueError:
        return False
    if not isinstance(decoded, dict):
        return False
    payload = cast(dict[str, object], decoded)
    return (
        payload.get("digest") == digest
        and payload.get("size") == size
        and payload.get("mtime_ns") == modified
    )


def _write_marker(marker: Path, fingerprint: tuple[str, int, int]) -> None:
    """Record the unpacked source fingerprint atomically."""
    digest, size, modified = fingerprint
    payload = json.dumps({"digest": digest, "size": size, "mtime_ns": modified}).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".evb-", dir=str(marker.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, marker)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _is_usable_tree(path: Path) -> bool:
    """Return whether a published game tree stands without symlink tricks."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _profile_identifier(source_exe: Path) -> str:
    """Key one profile by the packed source's parent directory."""
    try:
        return game_id(source_exe.parent)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(
            _("cannot unpack game executable {path}: {error}").format(
                path=str(source_exe), error=exc
            )
        ) from exc


def _identify_source(source_exe: Path) -> tuple[str, int, int]:
    """Hash one packed source without following symlinks and within budget."""
    try:
        descriptor = os.open(source_exe, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise RuntimeError(
            _("cannot read packed executable {path}: {error}").format(
                path=str(source_exe), error=exc
            )
        ) from exc
    try:
        entry = os.fstat(descriptor)
        if not stat.S_ISREG(entry.st_mode):
            raise RuntimeError(
                _("packed source is not a regular file: {path}").format(path=str(source_exe))
            )
        if entry.st_size > MAX_EVB_SOURCE_BYTES:
            raise RuntimeError(
                _("packed executable exceeds source size budget: {path}").format(
                    path=str(source_exe)
                )
            )
        os.set_blocking(descriptor, True)
        digest = hashlib.sha256()
        remaining = entry.st_size
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
        return (digest.hexdigest(), entry.st_size, entry.st_mtime_ns)
    except OSError as exc:
        raise RuntimeError(
            _("cannot read packed executable {path}: {error}").format(
                path=str(source_exe), error=exc
            )
        ) from exc
    finally:
        os.close(descriptor)


def _check_source_unchanged(source_exe: Path, size: int, modified: int) -> None:
    """Reject a source that changed between cache-key computation and publish."""
    try:
        entry = os.lstat(source_exe)
    except OSError as exc:
        raise RuntimeError(
            _("packed source changed during unpacking: {path}").format(path=str(source_exe))
        ) from exc
    if not stat.S_ISREG(entry.st_mode) or entry.st_size != size or entry.st_mtime_ns != modified:
        raise RuntimeError(
            _("packed source changed during unpacking: {path}").format(path=str(source_exe))
        )


def _check_source_fingerprint(source_exe: Path, fingerprint: tuple[str, int, int]) -> None:
    """Re-hash the source after unpacking and compare the full fingerprint.

    Size and modification time alone miss a same-size content swap that
    keeps mtime; comparing the fresh content digest before publication
    closes that window so new bytes never publish under an old digest.
    """
    current = _identify_source(source_exe)
    if current != fingerprint:
        raise RuntimeError(
            _("packed source changed during unpacking: {path}").format(path=str(source_exe))
        )
