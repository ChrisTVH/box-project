"""In-house recovery of packed executables bundling a virtual file tree.

Offline, standard-library-only unpacking for single-folder packed inputs:
the embedded virtual tree is reconstructed under an output directory and a
restored host executable is written beside it. Compatibility profiles are
tried newest-first until a caller-supplied tree validator accepts the
result; the winning profile is reported.

Reusable logic never writes to the terminal. Progress arrives only through
an optional caller-provided callback and diagnostics travel in the report.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from box.errors import RuntimeError
from box.utils.i18n import _

__all__ = [
    "PROFILES",
    "EvbProgressCallback",
    "TreeValidator",
    "UnpackReport",
    "unpack_packed_executable",
]

#: Caller-provided progress receiver: cumulative ``(files_written, bytes_written)``.
#: Events fire live per attempted extraction; the final event of the winning
#: attempt reports its totals.
EvbProgressCallback = Callable[[int, int], None]

#: Caller-supplied tree validator: accepts an extracted tree directory.
#: A non-empty tree is necessary but not sufficient; only a validator
#: acceptance marks a profile trial as successful.
TreeValidator = Callable[[Path], bool]

_SIGNATURE = b"EVB\x00"
_PACK_HEADER_SIZE = 64
_ENTRY_HEADER_SIZE = 16
_NAME_OFFSET = 16
_ROOT_COUNT_OFFSET = 12
# Observed modern layout: the 16-byte root record is followed by three zero
# pad bytes, and the first entry head starts one byte into that pad. Its zero
# length field therefore overlaps the root trailing zero byte (both zero, so
# no information is lost); every entry keeps the uniform 16-byte head with
# the name at offset 16. Legacy first entries start at root start plus root
# length plus four instead.
_MODERN_FIRST_ENTRY_OFFSET = 15
_MODERN_FOLDER_FILLER = 25
_MODERN_TRAIL_SIZE = 53
_LEGACY_TRAIL_SIZE = 49
_PLACEHOLDER_FOLDER = "%DEFAULT FOLDER%"
_UNKNOWN_KIND_NOTE_LIMIT = 8

#: Newest-first compatibility trial: (profile name, directory grouping).
#: Profile A covers the newest stamps with modern grouping, profile B the
#: middle stamp with modern grouping, and profile C the oldest stamp with
#: legacy grouping.
PROFILES: tuple[tuple[str, str], ...] = (
    ("10_70", "modern"),
    ("9_70", "modern"),
    ("7_80", "legacy"),
)

# Budgets mirror box.runtime.limits so hostile inputs fail before harm.
_MAX_SOURCE_BYTES = 1024 * 1024 * 1024
_MAX_TOTAL_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_FILE_BYTES = 1024 * 1024 * 1024
_MAX_ENTRIES = 50_000
_MAX_DEPTH = 32
_TIME_BUDGET_SECONDS = 15 * 60

# Executable-restoration layouts per (profile, bitness): total loader-header
# extent plus (import address, import size, relocation address, relocation
# size, thread address, thread size) field offsets from the loader header.
_PE_EXTENTS: dict[tuple[str, int], int] = {
    ("10_70", 32): 108,
    ("9_70", 32): 104,
    ("7_80", 32): 100,
    ("10_70", 64): 144,
    ("9_70", 64): 132,
    ("7_80", 64): 128,
}
_PE_FIELD_OFFSETS: dict[tuple[str, int], tuple[int, int, int, int, int, int]] = {
    ("10_70", 32): (84, 88, 92, 96, 100, 104),
    ("9_70", 32): (80, 84, 88, 92, 96, 100),
    ("7_80", 32): (76, 80, 84, 88, 92, 96),
    ("10_70", 64): (120, 124, 128, 132, 136, 140),
    ("9_70", 64): (108, 112, 116, 120, 124, 128),
    ("7_80", 64): (104, 108, 112, 116, 120, 124),
}
_TLS_TEMPLATE_LENGTH = {32: 16, 64: 32}
_TLS_PRESENT_SIZE = {32: 24, 64: 40}
_TLS_LOCATOR_KEY_LENGTH = 12
_EXCEPTION_STEP = {32: 20, 64: 12}
_HOST_ALIGNMENT = {32: 4, 64: 8}

_KIND_FILE = 2
_KIND_FOLDER = 3

_DIRECTORY_IMPORT = 1
_DIRECTORY_EXCEPTION = 3
_DIRECTORY_RELOCATION = 5
_DIRECTORY_TLS = 9


@dataclass(frozen=True, slots=True)
class UnpackReport:
    """The outcome of a successful packed-executable recovery."""

    profile: str
    grouping: str
    files: int
    unpacked_bytes: int
    restored_executable: Path | None
    warnings: tuple[str, ...]
    notes: tuple[str, ...]


class _ProfileMismatch(Exception):
    """Internal signal: the active profile cannot parse this input."""


class _EmptyTree(Exception):
    """Internal signal: the active profile parsed a valid but empty tree."""


class _BranchRefused(Exception):
    """Internal signal: one branch failed while siblings may continue."""


class _StopLevel(Exception):
    """Internal signal: stop consuming sibling records at one level."""

    def __init__(self, resume: int | None = None) -> None:
        super().__init__("stop level")
        self.resume = resume


def unpack_packed_executable(
    source: Path,
    destination: Path,
    *,
    validator: TreeValidator | None = None,
    profiles: tuple[tuple[str, str], ...] = PROFILES,
    progress: EvbProgressCallback | None = None,
    extract_tree: bool = True,
    restore_executable: bool = True,
    out_pe: Path | None = None,
) -> UnpackReport:
    """Recover the virtual tree and the restored executable from one input.

    Tries profiles newest-first; each attempt extracts into an isolated trial
    directory and only the winning attempt is published into ``destination``.
    ``validator`` accepts the winning tree (without one, any non-empty tree
    passes). Raises ``RuntimeError`` naming the input when no profile
    validates, when nothing is extracted, or when any budget breaks.
    """
    source_label = str(source)
    _check_profiles(profiles)
    if not extract_tree and not restore_executable:
        raise ValueError("nothing to do: tree extraction and restoration are both disabled")
    image = _read_source(source)
    if image.find(_SIGNATURE) < 0:
        raise RuntimeError(
            _("not a packed executable: signature not found in {path}").format(path=source_label)
        )
    _ensure_output_directory(destination)
    warnings: list[str] = []
    if not extract_tree:
        warnings.append(_("file tree reconstruction skipped for {path}").format(path=source_label))
    if not restore_executable:
        warnings.append(_("executable restoration skipped for {path}").format(path=source_label))
    notes: list[str] = []
    selected: str | None = None
    grouping = profiles[0][1]
    files = 0
    unpacked_bytes = 0
    if extract_tree:
        selected, grouping, files, unpacked_bytes, notes = _trial_profiles(
            image,
            source_label,
            destination,
            validator=validator,
            profiles=profiles,
            progress=progress,
        )
    restored: Path | None = None
    if restore_executable:
        restored, active_profile = _restore_requested_executable(
            image,
            source_label,
            destination,
            source_name=source.name,
            profile=selected,
            profiles=profiles,
            out_pe=out_pe,
            warnings=warnings,
            notes=notes,
        )
        if selected is None:
            selected = active_profile
    if progress is not None:
        progress(files, unpacked_bytes)
    return UnpackReport(
        profile=selected if selected is not None else profiles[0][0],
        grouping=grouping,
        files=files,
        unpacked_bytes=unpacked_bytes,
        restored_executable=restored,
        warnings=tuple(warnings),
        notes=tuple(notes),
    )


def _check_profiles(profiles: tuple[tuple[str, str], ...]) -> None:
    """Reject unknown profile selectors before touching any input.

    Accepted combinations are exactly ``PROFILES`` newest-first:
    ``10_70+modern``, ``9_70+modern``, and ``7_80+legacy``. Any other
    (profile, grouping) pair is rejected, including swapped groupings
    such as ``10_70+legacy``.
    """
    if not profiles:
        raise ValueError("at least one compatibility profile is required")
    supported = frozenset(PROFILES)
    for name, grouping in profiles:
        if (name, grouping) not in supported:
            raise ValueError(f"unknown compatibility profile: {name}+{grouping}")


def _read_source(source: Path) -> bytes:
    """Read one packed input without following symlinks and within budget."""
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise RuntimeError(
            _("cannot read packed executable {path}: {error}").format(path=str(source), error=exc)
        ) from exc
    try:
        entry = os.fstat(descriptor)
        if not stat.S_ISREG(entry.st_mode):
            raise RuntimeError(
                _("packed source is not a regular file: {path}").format(path=str(source))
            )
        if entry.st_size > _MAX_SOURCE_BYTES:
            raise RuntimeError(
                _("packed executable exceeds source size budget: {path}").format(path=str(source))
            )
        os.set_blocking(descriptor, True)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(_MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise RuntimeError(
            _("cannot read packed executable {path}: {error}").format(path=str(source), error=exc)
        ) from exc
    finally:
        os.close(descriptor)
    if len(data) > _MAX_SOURCE_BYTES:
        raise RuntimeError(
            _("packed executable exceeds source size budget: {path}").format(path=str(source))
        )
    return data


def _ensure_output_directory(destination: Path) -> None:
    """Create the output directory, refusing symlinks and non-directories."""
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            _("cannot create output directory {path}: {error}").format(
                path=str(destination), error=exc
            )
        ) from exc
    try:
        entry = os.lstat(destination)
    except OSError as exc:
        raise RuntimeError(
            _("cannot inspect output directory {path}: {error}").format(
                path=str(destination), error=exc
            )
        ) from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise RuntimeError(
            _("refusing unsafe output directory: {path}").format(path=str(destination))
        )


def _trial_profiles(
    image: bytes,
    source_label: str,
    destination: Path,
    *,
    validator: TreeValidator | None,
    profiles: tuple[tuple[str, str], ...],
    progress: EvbProgressCallback | None,
) -> tuple[str, str, int, int, list[str]]:
    """Try each profile and publish only the winning attempt.

    The first pass additionally requires plausible recovered loader values;
    the fallback pass accepts any validated tree so degraded restoration can
    still succeed with warnings.
    """
    deadline = time.monotonic() + _TIME_BUDGET_SECONDS
    stats = _TrialStats()
    winner = _trial_pass(
        image,
        source_label,
        destination,
        validator,
        profiles,
        progress,
        deadline,
        stats,
        require_loader=True,
    )
    if winner is None:
        winner = _trial_pass(
            image,
            source_label,
            destination,
            validator,
            profiles,
            progress,
            deadline,
            stats,
            require_loader=False,
        )
    if winner is not None:
        return winner
    if stats.budget_failure is not None:
        raise stats.budget_failure
    tried = ", ".join(f"{name}+{grouping}" for name, grouping in profiles)
    if stats.best_files > 0 or not stats.saw_empty:
        raise RuntimeError(
            _("no valid file tree in {path} after trying {profiles}").format(
                path=source_label, profiles=tried
            )
        )
    raise RuntimeError(
        _("empty extraction: no files recovered from {path}").format(path=source_label)
    )


class _TrialStats:
    """Outcome accounting shared by both trial passes.

    The total unpacked-byte budget and the entry count are shared by every
    profile attempt across both passes so worst-case cumulative I/O honors
    the per-run cap instead of resetting per attempt.
    """

    def __init__(self) -> None:
        self.budget_failure: RuntimeError | None = None
        self.saw_empty = False
        self.best_files = 0
        self.total_remaining = _MAX_TOTAL_UNPACKED_BYTES
        self.total_entries = 0


def _trial_pass(
    image: bytes,
    source_label: str,
    destination: Path,
    validator: TreeValidator | None,
    profiles: tuple[tuple[str, str], ...],
    progress: EvbProgressCallback | None,
    deadline: float,
    stats: _TrialStats,
    *,
    require_loader: bool,
) -> tuple[str, str, int, int, list[str]] | None:
    """Run one trial pass, returning the winning attempt when any validates.

    Each attempt stages into a fresh empty trial directory created with
    ``mkdtemp`` below the output directory; only the winning attempt is
    published. Per-attempt progress events are buffered and only the
    winner's events are replayed so losing attempts stay silent.
    """
    for name, grouping in profiles:
        _check_deadline(deadline, source_label)
        trial_dir = Path(tempfile.mkdtemp(prefix=".evb-trial-", dir=destination))
        try:
            attempt_events: list[tuple[int, int]] = []

            def _buffer(
                files: int, total: int, _events: list[tuple[int, int]] = attempt_events
            ) -> None:
                _events.append((files, total))

            buffered: EvbProgressCallback | None = None if progress is None else _buffer
            files, unpacked_bytes, notes = _extract_attempt(
                image,
                source_label,
                grouping,
                trial_dir,
                progress=buffered,
                deadline=deadline,
                shared=stats,
            )
            if files == 0:
                continue
            stats.best_files = max(stats.best_files, files)
            if require_loader and not _loader_values_validate(image, name, source_label):
                continue
            try:
                accepted = validator is None or bool(validator(trial_dir))
            except Exception:
                continue
            if not accepted:
                continue
            if progress is not None:
                for reported_files, reported_bytes in attempt_events:
                    progress(reported_files, reported_bytes)
            _publish_trial(trial_dir, destination)
            return (name, grouping, files, unpacked_bytes, notes)
        except _EmptyTree:
            stats.saw_empty = True
        except _ProfileMismatch:
            continue
        except RuntimeError as exc:
            if stats.budget_failure is None:
                stats.budget_failure = exc
        finally:
            _remove_trial(trial_dir)
    return None


def _loader_values_validate(image: bytes, profile: str, source_label: str) -> bool:
    """Check the loader header under one profile before accepting its tree."""
    del source_label
    layout = _parse_pe(image)
    if layout is None or layout.section_count < 2:
        return False
    key = (profile, layout.bitness)
    if key not in _PE_EXTENTS:
        return False
    first_loader = layout.sections[-2]
    if first_loader.raw_size < _PE_EXTENTS[key]:
        return False
    try:
        _validate_loader_ranges(image, layout, first_loader, layout.sections[-1], "probe")
    except RuntimeError:
        return False
    preserved = layout.sections[:-2]
    preserved_ranges = _preserved_ranges(layout, preserved)
    offsets = _PE_FIELD_OFFSETS[key]
    base = first_loader.raw_pointer
    values = [_u32(image, base + offset) for offset in offsets[0:4]]
    if any(value is None for value in values):
        return False
    import_address = int(values[0] or 0)
    import_size = int(values[1] or 0)
    relocation_address = int(values[2] or 0)
    relocation_size = int(values[3] or 0)
    if import_size == 0 or relocation_size == 0:
        return False
    if not _range_is_preserved(import_address, import_size, preserved_ranges):
        return False
    return _range_is_preserved(relocation_address, relocation_size, preserved_ranges)


def _remove_trial(trial_dir: Path) -> None:
    """Drop one failed attempt without touching published output."""
    shutil.rmtree(trial_dir, ignore_errors=True)


def _publish_trial(trial_dir: Path, destination: Path) -> None:
    """Move a winning attempt into the output directory, truncating clashes."""
    for entry in sorted(trial_dir.iterdir()):
        target = destination / entry.name
        try:
            os.replace(entry, target)
        except OSError:
            if entry.is_dir() and not entry.is_symlink():
                shutil.copytree(entry, target, symlinks=False, dirs_exist_ok=True)
                shutil.rmtree(entry, ignore_errors=True)
            else:
                raise
    try:
        trial_dir.rmdir()
    except OSError:
        _remove_trial(trial_dir)


class _TrialState:
    """Mutable per-attempt walk, write, and budget accounting.

    Entry counts and the total unpacked-byte budget live in the shared
    per-run stats object; per-attempt file counts stay local.
    """

    def __init__(
        self,
        *,
        image: bytes,
        source_label: str,
        grouping: str,
        trail_size: int,
        payload_base: int,
        trial_dir: Path,
        progress: EvbProgressCallback | None,
        deadline: float,
        shared: _TrialStats,
    ) -> None:
        self.image = image
        self.source_label = source_label
        self.grouping = grouping
        self.trail_size = trail_size
        self.payload_base = payload_base
        self.accumulated = 0
        self.files = 0
        self.unpacked_bytes = 0
        self.shared = shared
        self.unknown_kinds = 0
        self.notes: list[str] = []
        self.trial_dir = trial_dir
        self.progress = progress
        self.deadline = deadline

    def check_deadline(self) -> None:
        """Abort promptly on expiry at chunk, file, and branch boundaries."""
        _check_deadline(self.deadline, self.source_label)

    def count_entry(self) -> None:
        """Count one parsed record against the shared entry budget."""
        self.shared.total_entries += 1
        if self.shared.total_entries > _MAX_ENTRIES:
            raise RuntimeError(
                _("packed executable exceeds entry count budget: {path}").format(
                    path=self.source_label
                )
            )

    def reserve(self, unpacked: int) -> None:
        """Charge declared bytes to the shared total before writing."""
        if unpacked < 0 or unpacked > _MAX_FILE_BYTES:
            raise RuntimeError(
                _("packed file exceeds per-file size budget: {path}").format(path=self.source_label)
            )
        self.shared.total_remaining -= unpacked
        if self.shared.total_remaining < 0:
            raise RuntimeError(
                _("packed executable exceeds total size budget: {path}").format(
                    path=self.source_label
                )
            )

    def note(self, message: str) -> None:
        """Record one skip, refusal, or mismatch naming its offender."""
        self.notes.append(message)

    def report_file(self, unpacked: int) -> None:
        """Count one verified file and deliver progress to the receiver."""
        self.files += 1
        self.unpacked_bytes += unpacked
        if self.progress is not None:
            self.progress(self.files, self.unpacked_bytes)


def _check_deadline(deadline: float, source_label: str) -> None:
    """Fail the run once the wall-clock budget is exhausted."""
    if time.monotonic() >= deadline:
        raise RuntimeError(
            _("packed executable exceeds time budget: {path}").format(path=source_label)
        )


def _u32(image: bytes | bytearray | memoryview, offset: int) -> int | None:
    """Read one little-endian value, or None when out of bounds."""
    if offset < 0 or offset + 4 > len(image):
        return None
    return int.from_bytes(bytes(image[offset : offset + 4]), "little")


@dataclass(frozen=True, slots=True)
class _EntryHeader:
    """One parsed directory entry head: fixed header, name, and kind."""

    length: int
    children: int
    name: str
    name_length: int
    kind: int
    record_end: int


def _extract_attempt(
    image: bytes,
    source_label: str,
    grouping: str,
    trial_dir: Path,
    *,
    progress: EvbProgressCallback | None,
    deadline: float,
    shared: _TrialStats,
) -> tuple[int, int, list[str]]:
    """Parse and write one profile attempt into an isolated trial directory.

    The trial directory must be an empty directory (fresh ``mkdtemp``); only
    the winning attempt is published while losers are removed.
    """
    signature = image.find(_SIGNATURE)
    directory = signature + _PACK_HEADER_SIZE
    root_length = _u32(image, directory)
    top_level = _u32(image, directory + _ROOT_COUNT_OFFSET)
    if root_length is None or top_level is None:
        raise _ProfileMismatch("truncated pack header")
    if top_level == 0:
        raise _EmptyTree()
    if top_level > _MAX_ENTRIES:
        raise RuntimeError(
            _("packed executable exceeds entry count budget: {path}").format(path=source_label)
        )
    trail_size = _MODERN_TRAIL_SIZE if grouping == "modern" else _LEGACY_TRAIL_SIZE
    if grouping == "modern":
        payload_base = directory + root_length + 4
        if payload_base < directory + _ENTRY_HEADER_SIZE or payload_base > len(image):
            raise _ProfileMismatch("payload base out of file")
        # Observed single-file inputs pad the directory end with four zero
        # bytes before the payload base; entry advances stay spec-literal
        # (53 per trailing record) and positions derive from the base, so the
        # slack needs no special handling.
        cursor = directory + _MODERN_FIRST_ENTRY_OFFSET
    else:
        payload_base = 0
        cursor = directory + root_length + 4
    if cursor < 0 or cursor > len(image):
        raise _ProfileMismatch("directory start out of file")
    state = _TrialState(
        image=image,
        source_label=source_label,
        grouping=grouping,
        trail_size=trail_size,
        payload_base=payload_base,
        trial_dir=trial_dir,
        progress=progress,
        deadline=deadline,
        shared=shared,
    )
    state.count_entry()
    try:
        _walk_level(state, cursor, top_level, 1, trial_dir, record=True)
    except _StopLevel as exc:
        if exc.resume is not None:
            cursor = exc.resume
        state.note(
            _("unknown directory entry ends enumeration in {path}").format(path=source_label)
        )
    if state.files == 0:
        raise _EmptyTree()
    return (state.files, state.unpacked_bytes, state.notes)


def _walk_level(
    state: _TrialState,
    cursor: int,
    count: int,
    depth: int,
    out_dir: Path,
    *,
    record: bool,
) -> int:
    """Consume one sibling run, returning the cursor past its last record.

    An unknown kind ends only this branch: further siblings at this level
    are not consumed, but the returned cursor lets unaffected parent
    branches continue.
    """
    if depth > _MAX_DEPTH:
        raise RuntimeError(
            _("packed executable exceeds tree depth budget: {path}").format(path=state.source_label)
        )
    for _sibling in range(count):
        state.check_deadline()
        try:
            cursor = _walk_entry(state, cursor, depth, out_dir, record=record)
        except _StopLevel as exc:
            if exc.resume is not None:
                cursor = exc.resume
            state.note(
                _("unknown directory entry ends enumeration in {path}").format(
                    path=state.source_label
                )
            )
            return cursor
    return cursor


def _walk_entry(state: _TrialState, cursor: int, depth: int, out_dir: Path, *, record: bool) -> int:
    """Consume one entry (advancing past names, payloads, and children)."""
    image = state.image
    header_length = _u32(image, cursor)
    children = _u32(image, cursor + 12)
    if header_length is None or children is None:
        raise _ProfileMismatch("truncated entry header")
    if state.grouping == "legacy" and header_length <= 0:
        raise _ProfileMismatch("non-positive record length")
    state.count_entry()
    if children > _MAX_ENTRIES:
        raise RuntimeError(
            _("packed executable exceeds entry count budget: {path}").format(
                path=state.source_label
            )
        )
    bound = len(image)
    if state.grouping == "legacy":
        bound = min(len(image), cursor + header_length + 4)
    header = _parse_name_and_kind(image, cursor, bound)
    if header is None:
        raise _ProfileMismatch("truncated entry name")
    if not _is_safe_name(header.name):
        state.note(
            _("refusing unsafe virtual name at offset {offset} in {path}").format(
                offset=cursor, path=state.source_label
            )
        )
        return _skip_entry(state, cursor, header, depth)
    if header.kind == _KIND_FILE:
        return _walk_file(state, cursor, header, out_dir, record=record)
    if header.kind == _KIND_FOLDER:
        return _walk_folder(state, cursor, header, depth, out_dir, record=record)
    state.unknown_kinds += 1
    if state.unknown_kinds <= _UNKNOWN_KIND_NOTE_LIMIT:
        state.note(
            _("unknown directory entry at offset {offset} in {path}").format(
                offset=cursor, path=state.source_label
            )
        )
    if state.grouping == "legacy" and header.length > 0:
        skipped = cursor + header.length + 4
        if skipped > cursor and skipped <= len(image):
            return skipped
        raise _ProfileMismatch("record length does not advance")
    resume = header.record_end + state.trail_size
    if resume <= cursor or resume > len(image):
        resume = header.record_end
    raise _StopLevel(resume)


def _parse_name_and_kind(image: bytes, cursor: int, bound: int) -> _EntryHeader | None:
    """Decode one null-terminated UTF-16LE name plus its kind tag."""
    length = _u32(image, cursor)
    children = _u32(image, cursor + 12)
    if length is None or children is None:
        return None
    position = cursor + _NAME_OFFSET
    while position + 1 < bound:
        if image[position] == 0 and image[position + 1] == 0:
            break
        position += 2
    else:
        return None
    kind_offset = position + 2
    if kind_offset >= len(image):
        return None
    raw_name = image[cursor + _NAME_OFFSET : position]
    try:
        name = raw_name.decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    return _EntryHeader(
        length=length,
        children=children,
        name=name,
        name_length=len(raw_name),
        kind=image[kind_offset],
        record_end=kind_offset + 1,
    )


def _is_safe_name(name: str) -> bool:
    """Accept only single-segment relative names without escapes."""
    if not name or name in (".", ".."):
        return False
    return all(character not in ("/", "\\", ":") for character in name)


def _skip_entry(state: _TrialState, cursor: int, header: _EntryHeader, depth: int) -> int:
    """Advance past one refused branch without recording anything."""
    if header.kind == _KIND_FILE:
        return _skip_file(state, cursor, header)
    if header.kind == _KIND_FOLDER:
        children_cursor = _folder_children_cursor(state, cursor, header)
        if header.children:
            return _walk_level(
                state,
                children_cursor,
                header.children,
                depth + 1,
                state.trial_dir,
                record=False,
            )
        return children_cursor
    if state.grouping == "legacy" and header.length > 0:
        skipped = cursor + header.length + 4
        if skipped > cursor and skipped <= len(state.image):
            return skipped
    return header.record_end


def _folder_children_cursor(state: _TrialState, cursor: int, header: _EntryHeader) -> int:
    """Return the offset of the first child record of one folder entry."""
    if state.grouping == "legacy":
        if header.length <= 0:
            raise _ProfileMismatch("non-positive record length")
        folder_end = cursor + header.length + 4
        if folder_end <= cursor or folder_end > len(state.image):
            raise _ProfileMismatch("record length does not advance")
        return folder_end
    children_cursor = header.record_end + _MODERN_FOLDER_FILLER
    if children_cursor > len(state.image):
        raise _ProfileMismatch("truncated folder filler")
    return children_cursor


def _walk_folder(
    state: _TrialState,
    cursor: int,
    header: _EntryHeader,
    depth: int,
    out_dir: Path,
    *,
    record: bool,
) -> int:
    """Materialize one folder and consume its children.

    A refused directory never aborts the profile: the branch is noted and
    its children are still consumed with recording disabled so the cursor
    advances past the whole branch.
    """
    children_cursor = _folder_children_cursor(state, cursor, header)
    if record and header.name == _PLACEHOLDER_FOLDER:
        child_dir = out_dir
    elif record:
        child_dir = out_dir / header.name
        try:
            _ensure_directory(child_dir, state.source_label, state.notes)
        except _BranchRefused as exc:
            message = str(exc)
            if message not in state.notes:
                state.note(message)
            if header.children:
                return _walk_level(
                    state,
                    children_cursor,
                    header.children,
                    depth + 1,
                    state.trial_dir,
                    record=False,
                )
            return children_cursor
    else:
        child_dir = out_dir
    if header.children:
        end = _walk_level(
            state, children_cursor, header.children, depth + 1, child_dir, record=record
        )
        if state.grouping == "legacy" and end < children_cursor:
            raise _ProfileMismatch("child records overlap their folder")
        return end
    return children_cursor


def _walk_file(
    state: _TrialState, cursor: int, header: _EntryHeader, out_dir: Path, *, record: bool
) -> int:
    """Plan one file payload position and return the cursor past it."""
    image = state.image
    trail = header.record_end
    unpacked = _u32(image, trail + 2)
    if unpacked is None:
        raise _ProfileMismatch("truncated file record")
    if state.grouping == "modern":
        stored = _u32(image, trail + _MODERN_TRAIL_SIZE - 4)
        if stored is None:
            raise _ProfileMismatch("truncated file record")
        position = state.payload_base + state.accumulated
        state.accumulated += stored
        if stored == 0 and unpacked != 0:
            state.note(
                _("size mismatch for {name}: stored {stored} bytes in {path}").format(
                    name=header.name, stored=stored, path=state.source_label
                )
            )
            return trail + state.trail_size
        if position < 0 or position + stored > len(image):
            raise _ProfileMismatch("payload out of file")
        next_cursor = trail + state.trail_size
    else:
        stored = _u32(image, trail + _LEGACY_TRAIL_SIZE - 8)
        if stored is None:
            raise _ProfileMismatch("truncated file record")
        if header.length <= 0:
            raise _ProfileMismatch("non-positive record length")
        position = cursor + header.length + 4
        if position <= cursor or position > len(image):
            raise _ProfileMismatch("record length does not advance")
        if stored == 0 and unpacked != 0:
            state.note(
                _("size mismatch for {name}: stored {stored} bytes in {path}").format(
                    name=header.name, stored=stored, path=state.source_label
                )
            )
            return position
        if position + stored > len(image):
            raise _ProfileMismatch("payload out of file")
        next_cursor = position + stored
    if record:
        _write_planned_file(state, out_dir / header.name, position, unpacked, stored)
    else:
        state.reserve(unpacked)
    return next_cursor


def _skip_file(state: _TrialState, cursor: int, header: _EntryHeader) -> int:
    """Advance past one refused file without recording it."""
    image = state.image
    trail = header.record_end
    if state.grouping == "modern":
        stored = _u32(image, trail + _MODERN_TRAIL_SIZE - 4)
        if stored is None:
            raise _ProfileMismatch("truncated file record")
        state.accumulated += stored
        return trail + state.trail_size
    stored = _u32(image, trail + _LEGACY_TRAIL_SIZE - 8)
    if stored is None:
        raise _ProfileMismatch("truncated file record")
    if header.length <= 0:
        raise _ProfileMismatch("non-positive record length")
    position = cursor + header.length + 4
    if position <= cursor or position > len(image):
        raise _ProfileMismatch("record length does not advance")
    return position + stored


def _write_planned_file(
    state: _TrialState, destination: Path, position: int, unpacked: int, stored: int
) -> None:
    """Write one payload as raw bytes or through the chunk decompressor.

    Corrupt payload content never aborts the profile: the file is noted
    and skipped while siblings continue, since modern positions derive
    from base plus accumulated stored lengths and do not desync.
    """
    try:
        _ensure_parent_dirs(state.trial_dir, destination)
    except _BranchRefused as exc:
        state.note(str(exc))
        return
    state.reserve(unpacked)
    if stored < 0 or stored > _MAX_FILE_BYTES:
        raise RuntimeError(
            _("packed file exceeds per-file size budget: {path}").format(path=state.source_label)
        )
    if stored != unpacked:
        state.reserve(stored)
    if position < 0 or stored < 0 or position + stored > len(state.image):
        raise _ProfileMismatch("payload out of file")
    compressed = stored != unpacked
    try:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
    except OSError as exc:
        state.note(
            _("refusing to write {name} in {path}: {error}").format(
                name=destination.name, path=state.source_label, error=exc
            )
        )
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if compressed:
                written = 0
                view = memoryview(state.image)[position : position + stored]
                try:
                    chunks = _decompressed_chunks(view, stored=stored, state=state)
                    for chunk in chunks:
                        handle.write(chunk)
                        written += len(chunk)
                        state.check_deadline()
                except _ProfileMismatch:
                    state.note(
                        _("cannot decode {name} in {path}").format(
                            name=destination.name, path=state.source_label
                        )
                    )
                    return
            else:
                handle.write(memoryview(state.image)[position : position + stored])
                written = stored
    except _BranchRefused as exc:
        state.note(str(exc))
        return
    except OSError as exc:
        state.note(
            _("cannot write {name} in {path}: {error}").format(
                name=destination.name, path=state.source_label, error=exc
            )
        )
        return
    if written != unpacked:
        state.note(
            _("size mismatch for {name}: expected {expected} bytes, got {actual} bytes").format(
                name=destination.name, expected=unpacked, actual=written
            )
        )
        return
    state.report_file(written)


def _decompressed_chunks(
    payload: bytes | memoryview, *, stored: int, state: _TrialState
) -> Iterator[bytes]:
    """Yield decompressed bytes for one dictionary-compressed payload.

    Parses the block header and its every-third-value length table, then
    decodes each chunk as an independent standard dictionary stream while
    holding at most one chunk in memory. The length table is bounded
    before materializing so hostile headers fail with a budget error.
    """
    if len(payload) != stored:
        raise _ProfileMismatch("stored length mismatch")
    own = _u32(payload, 0)
    if own is None or own < 8 or own > stored:
        raise _ProfileMismatch("corrupt compressed block header")
    if own > _MAX_FILE_BYTES:
        raise RuntimeError(
            _("packed file exceeds per-file size budget: {path}").format(path=state.source_label)
        )
    table_length = own - 8
    if table_length > _MAX_FILE_BYTES:
        raise RuntimeError(
            _("packed file exceeds per-file size budget: {path}").format(path=state.source_label)
        )
    value_count = table_length // 4
    chunk_count = (value_count + 2) // 3 if value_count else 0
    if chunk_count > _MAX_ENTRIES:
        raise RuntimeError(
            _("packed executable exceeds entry count budget: {path}").format(
                path=state.source_label
            )
        )
    chunk_lengths: list[int] = []
    running = 0
    for start in range(0, table_length, 12):
        length = _u32(payload, 8 + start)
        if length is None:
            raise _ProfileMismatch("truncated compressed chunk table")
        chunk_lengths.append(length)
        running += length
        if running > stored - own:
            raise _ProfileMismatch("stored length mismatch")
    if own + running != stored:
        raise _ProfileMismatch("stored length mismatch")
    offset = own
    for length in chunk_lengths:
        chunk = payload[offset : offset + length]
        if len(chunk) != length:
            raise _ProfileMismatch("truncated compressed chunk")
        offset += length
        if length == 0:
            yield b""
            continue
        yield _decode_chunk(chunk)
        state.check_deadline()


def _decode_chunk(chunk: bytes | memoryview) -> bytes:
    """Decode one independent chunk, confirming its stream wrapper."""
    data = bytes(chunk)
    try:
        return zlib.decompress(data)
    except zlib.error:
        pass
    try:
        return zlib.decompress(data, -15)
    except zlib.error:
        pass
    try:
        return zlib.decompress(data, 31)
    except zlib.error as exc:
        raise _ProfileMismatch("unsupported chunk encoding") from exc


def _ensure_directory(path: Path, source_label: str, notes: list[str]) -> None:
    """Create one output directory without following symlinks."""
    del notes
    try:
        os.mkdir(path, 0o700)
        return
    except FileExistsError:
        pass
    except OSError as exc:
        message = _("cannot create directory {name} in {path}: {error}").format(
            name=path.name, path=source_label, error=exc
        )
        raise _BranchRefused(message) from exc
    try:
        entry = os.lstat(path)
    except OSError as exc:
        raise _BranchRefused(
            _("cannot inspect directory {name} in {path}: {error}").format(
                name=path.name, path=source_label, error=exc
            )
        ) from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise _BranchRefused(
            _("refusing unsafe directory {name} in {path}").format(
                name=path.name, path=source_label
            )
        )


def _ensure_parent_dirs(root: Path, destination: Path) -> None:
    """Create missing parents below the trial root, refusing symlink escapes."""
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise _BranchRefused(
            _("refusing path outside extraction root: {name}").format(name=destination.name)
        ) from exc
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            try:
                entry = os.lstat(current)
            except OSError as exc:
                raise _BranchRefused(
                    _("cannot inspect directory {name}").format(name=component)
                ) from exc
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                raise _BranchRefused(
                    _("refusing unsafe directory {name}").format(name=component)
                ) from None
        except OSError as exc:
            raise _BranchRefused(
                _("cannot create directory {name}").format(name=component)
            ) from exc


def _restore_requested_executable(
    image: bytes,
    source_label: str,
    destination: Path,
    *,
    source_name: str,
    profile: str | None,
    profiles: tuple[tuple[str, str], ...],
    out_pe: Path | None,
    warnings: list[str],
    notes: list[str],
) -> tuple[Path, str]:
    """Restore the host executable with the winning (or selected) profile."""
    restored_path = out_pe if out_pe is not None else destination / source_name
    layout = _parse_pe(image)
    if layout is None:
        raise RuntimeError(
            _("cannot restore executable from {path}: not a valid executable").format(
                path=source_label
            )
        )
    if layout.section_count < 2:
        raise RuntimeError(
            _("likely not a packed executable: loader sections missing in {path}").format(
                path=source_label
            )
        )
    active = profile if profile is not None else _select_restore_profile(image, layout, profiles)
    restored = _restore_executable(
        image,
        layout,
        source_label=source_label,
        profile=active,
        warnings=warnings,
        notes=notes,
    )
    _write_restored(restored_path, restored)
    return (restored_path, active)


def _select_restore_profile(
    image: bytes,
    layout: _PeLayout,
    profiles: tuple[tuple[str, str], ...],
) -> str:
    """Pick the first profile with plausible preserved loader directories.

    Mirrors the preserved-range check from the tree-trial loader validation
    so restore-only runs cannot pick a degraded profile when a clean one
    exists.
    """
    first_loader = layout.sections[-2]
    preserved = layout.sections[:-2]
    preserved_ranges = _preserved_ranges(layout, preserved)
    for name, _grouping in profiles:
        key = (name, layout.bitness)
        if key not in _PE_EXTENTS or first_loader.raw_size < _PE_EXTENTS[key]:
            continue
        offsets = _PE_FIELD_OFFSETS[key]
        base = first_loader.raw_pointer
        import_address = _u32(image, base + offsets[0])
        import_size = _u32(image, base + offsets[1])
        relocation_address = _u32(image, base + offsets[2])
        relocation_size = _u32(image, base + offsets[3])
        if not import_size or not relocation_size:
            continue
        if import_address is None or relocation_address is None:
            continue
        if not _range_is_preserved(import_address, import_size, preserved_ranges):
            continue
        if not _range_is_preserved(relocation_address, relocation_size, preserved_ranges):
            continue
        return name
    return profiles[0][0]


# --- Executable parsing and restoration (standard library only) ---


@dataclass(frozen=True, slots=True)
class _CoffSection:
    """One section header with its file offset for later patching."""

    name: bytes
    virtual_size: int
    virtual_address: int
    raw_size: int
    raw_pointer: int
    header_offset: int


@dataclass(frozen=True, slots=True)
class _PeLayout:
    """Parsed executable headers with patch offsets for restoration."""

    bitness: int
    section_count: int
    section_count_offset: int
    size_of_image_offset: int
    section_alignment: int
    size_of_headers: int
    directory_offset: int
    directory_count: int
    sections_offset: int
    sections: tuple[_CoffSection, ...]


def _try_u16(image: bytes, offset: int) -> int | None:
    """Read one little-endian half word, or None when out of bounds."""
    if offset < 0 or offset + 2 > len(image):
        return None
    return int.from_bytes(image[offset : offset + 2], "little")


def _try_u32(image: bytes, offset: int) -> int | None:
    """Read one little-endian word, or None when out of bounds."""
    if offset < 0 or offset + 4 > len(image):
        return None
    return int.from_bytes(image[offset : offset + 4], "little")


def _parse_pe(image: bytes) -> _PeLayout | None:
    """Parse executable headers, or return None for non-executable input."""
    if len(image) < 64 or image[0:2] != b"MZ":
        return None
    header_offset = _try_u32(image, 0x3C)
    if header_offset is None or header_offset < 0:
        return None
    if image[header_offset : header_offset + 4] != b"PE\x00\x00":
        return None
    coff = header_offset + 4
    section_count = _try_u16(image, coff + 2)
    optional_size = _try_u16(image, coff + 16)
    if section_count is None or optional_size is None:
        return None
    if section_count == 0 or section_count > 96:
        return None
    optional = coff + 20
    magic = _try_u16(image, optional)
    if magic == 0x10B:
        bitness = 32
    elif magic == 0x20B:
        bitness = 64
    else:
        return None
    section_alignment = _try_u32(image, optional + 32)
    size_of_headers = _try_u32(image, optional + 60)
    if section_alignment is None or size_of_headers is None:
        return None
    if bitness == 32:
        directory_count = _try_u32(image, optional + 92)
        directory_offset = optional + 96
    else:
        directory_count = _try_u32(image, optional + 108)
        directory_offset = optional + 112
    if directory_count is None or directory_count < 10:
        return None
    if directory_offset + directory_count * 8 > len(image):
        return None
    sections_offset = optional + optional_size
    if sections_offset + section_count * 40 > len(image):
        return None
    sections: list[_CoffSection] = []
    for index in range(section_count):
        base = sections_offset + index * 40
        virtual_size = _try_u32(image, base + 8)
        virtual_address = _try_u32(image, base + 12)
        raw_size = _try_u32(image, base + 16)
        raw_pointer = _try_u32(image, base + 20)
        if virtual_size is None or virtual_address is None:
            return None
        if raw_size is None or raw_pointer is None:
            return None
        sections.append(
            _CoffSection(
                name=image[base : base + 8],
                virtual_size=virtual_size,
                virtual_address=virtual_address,
                raw_size=raw_size,
                raw_pointer=raw_pointer,
                header_offset=base,
            )
        )
    return _PeLayout(
        bitness=bitness,
        section_count=section_count,
        section_count_offset=coff + 2,
        size_of_image_offset=optional + 56,
        section_alignment=section_alignment,
        size_of_headers=size_of_headers,
        directory_offset=directory_offset,
        directory_count=directory_count,
        sections_offset=sections_offset,
        sections=tuple(sections),
    )


def _restore_executable(
    image: bytes,
    layout: _PeLayout,
    *,
    source_label: str,
    profile: str,
    warnings: list[str],
    notes: list[str],
) -> bytes:
    """Remove the two trailing loader sections and recover directory values."""
    key = (profile, layout.bitness)
    if key not in _PE_EXTENTS:
        raise RuntimeError(
            _("cannot restore executable from {path}: unsupported profile").format(
                path=source_label
            )
        )
    first_loader = layout.sections[-2]
    second_loader = layout.sections[-1]
    _validate_loader_ranges(image, layout, first_loader, second_loader, source_label)
    extent = _PE_EXTENTS[key]
    if first_loader.raw_size < extent:
        raise RuntimeError(
            _("cannot restore executable from {path}: truncated loader data").format(
                path=source_label
            )
        )
    base = first_loader.raw_pointer
    offsets = _PE_FIELD_OFFSETS[key]
    values = [_u32(image, base + offset) for offset in offsets]
    if any(value is None for value in values):
        raise RuntimeError(
            _("cannot restore executable from {path}: truncated loader data").format(
                path=source_label
            )
        )
    plain = [int(value) for value in values if value is not None]
    import_address, import_size, relocation_address, relocation_size = plain[0:4]
    preserved = layout.sections[:-2]
    preserved_ranges = _preserved_ranges(layout, preserved)
    if import_size == 0:
        warnings.append(
            _(
                "recovered import directory is empty in {path}; another profile may restore it"
            ).format(path=source_label)
        )
    elif not _range_is_preserved(import_address, import_size, preserved_ranges):
        warnings.append(
            _(
                "recovered import address is outside the preserved image in {path}; "
                "another profile may restore it"
            ).format(path=source_label)
        )
    if relocation_size == 0:
        warnings.append(
            _(
                "recovered relocation directory is empty in {path}; another profile may restore it"
            ).format(path=source_label)
        )
    elif not _range_is_preserved(relocation_address, relocation_size, preserved_ranges):
        warnings.append(
            _(
                "recovered relocation address is outside the preserved image in {path}; "
                "another profile may restore it"
            ).format(path=source_label)
        )
    working = bytearray(image)
    exception_address, exception_size = _recover_exception_directory(
        working, layout, preserved, preserved_ranges, first_loader, source_label, warnings
    )
    tls_address, tls_size = _recover_tls_directory(
        working, layout, preserved, first_loader, source_label, notes
    )
    return _splice_executable(
        bytes(working),
        layout,
        preserved,
        source_label=source_label,
        import_address=import_address,
        import_size=import_size,
        relocation_address=relocation_address,
        relocation_size=relocation_size,
        exception_address=exception_address,
        exception_size=exception_size,
        tls_address=tls_address,
        tls_size=tls_size,
    )


def _validate_loader_ranges(
    image: bytes,
    layout: _PeLayout,
    first_loader: _CoffSection,
    second_loader: _CoffSection,
    source_label: str,
) -> None:
    """Refuse loader layouts that cannot be spliced safely."""
    headers_end = layout.sections_offset + layout.section_count * 40
    for section in layout.sections[:-2]:
        if section.raw_size and (
            section.raw_pointer <= 0
            or section.raw_pointer + section.raw_size > first_loader.raw_pointer
        ):
            raise RuntimeError(
                _("cannot restore executable from {path}: corrupt section layout").format(
                    path=source_label
                )
            )
    if first_loader.raw_pointer < headers_end or first_loader.raw_size <= 0:
        raise RuntimeError(
            _("cannot restore executable from {path}: loader data missing").format(
                path=source_label
            )
        )
    if second_loader.raw_pointer < first_loader.raw_pointer:
        raise RuntimeError(
            _("cannot restore executable from {path}: corrupt section layout").format(
                path=source_label
            )
        )
    for section in (first_loader, second_loader):
        if section.raw_size and section.raw_pointer + section.raw_size > len(image):
            raise RuntimeError(
                _("cannot restore executable from {path}: truncated loader data").format(
                    path=source_label
                )
            )


def _preserved_ranges(
    layout: _PeLayout, preserved: tuple[_CoffSection, ...]
) -> list[tuple[int, int]]:
    """Return half-open virtual ranges kept after loader removal."""
    ranges = [(0, layout.size_of_headers)]
    for section in preserved:
        end = section.virtual_address + max(section.virtual_size, section.raw_size)
        ranges.append((section.virtual_address, end))
    return ranges


def _range_is_preserved(address: int, size: int, preserved_ranges: list[tuple[int, int]]) -> bool:
    """Check one recovered directory against the preserved image."""
    if address < 0 or size <= 0:
        return False
    return any(start <= address and address + size <= end for start, end in preserved_ranges)


def _offset_from_rva(
    layout: _PeLayout,
    preserved: tuple[_CoffSection, ...],
    address: int,
) -> int | None:
    """Map a virtual address to a file offset within preserved bytes."""
    for section in preserved:
        size = max(section.virtual_size, section.raw_size)
        if section.virtual_address <= address < section.virtual_address + size:
            offset = section.raw_pointer + (address - section.virtual_address)
            if offset >= section.raw_pointer + section.raw_size:
                return None
            return offset
    if 0 <= address < layout.size_of_headers:
        return address
    return None


def _offset_from_rva_any(layout: _PeLayout, address: int) -> int | None:
    """Map a virtual address to a file offset anywhere in the packed image."""
    for section in layout.sections:
        size = max(section.virtual_size, section.raw_size)
        if section.virtual_address <= address < section.virtual_address + size:
            offset = section.raw_pointer + (address - section.virtual_address)
            if offset >= section.raw_pointer + section.raw_size:
                return None
            return offset
    if 0 <= address < layout.size_of_headers:
        return address
    return None


def _recover_exception_directory(
    working: bytearray,
    layout: _PeLayout,
    preserved: tuple[_CoffSection, ...],
    preserved_ranges: list[tuple[int, int]],
    first_loader: _CoffSection,
    source_label: str,
    warnings: list[str],
) -> tuple[int, int]:
    """Repoint the exception directory at preserved records, else zero it."""
    address = _u32(working, layout.directory_offset + _DIRECTORY_EXCEPTION * 8)
    size = _u32(working, layout.directory_offset + _DIRECTORY_EXCEPTION * 8 + 4)
    if address is None or size is None:
        raise RuntimeError(
            _("cannot restore executable from {path}: truncated headers").format(path=source_label)
        )
    if size == 0:
        return (address, size)
    step = _EXCEPTION_STEP[layout.bitness]
    home_offset = _offset_from_rva(layout, preserved, address)
    home = (
        home_offset is not None
        and home_offset + size <= first_loader.raw_pointer
        and home_offset + size <= len(working)
    )
    prefix = b""
    read_offset = _offset_from_rva_any(layout, address)
    if read_offset is not None and size > 0 and read_offset + size <= len(working):
        prefix = _scan_exception_prefix(bytes(working), read_offset, size, step, preserved_ranges)
    if home and len(prefix) == size:
        return (address, size)
    if not prefix:
        warnings.append(
            _(
                "no usable exception records in {path}; "
                "exception handling in the restored program may be impaired"
            ).format(path=source_label)
        )
        return (0, 0)
    cave = _find_cave(bytes(working), layout, preserved, first_loader, len(prefix))
    if cave is None:
        warnings.append(
            _(
                "no unused space for exception records in {path}; "
                "exception handling in the restored program may be impaired"
            ).format(path=source_label)
        )
        return (0, 0)
    cave_offset, cave_address = cave
    working[cave_offset : cave_offset + len(prefix)] = prefix
    return (cave_address, len(prefix))


def _scan_exception_prefix(
    image: bytes,
    offset: int,
    size: int,
    step: int,
    preserved_ranges: list[tuple[int, int]],
) -> bytes:
    """Keep records before the terminator or the first loader-area record."""
    end = offset
    remaining = size
    zero = bytes(step)
    while remaining >= step and end + step <= len(image):
        record = image[end : end + step]
        if record == zero:
            break
        begin = int.from_bytes(record[0:4], "little")
        finish = int.from_bytes(record[4:8], "little")
        if begin == 0 or begin >= finish:
            break
        if not _range_is_preserved(begin, finish - begin, preserved_ranges):
            break
        end += step
        remaining -= step
    return image[offset:end]


def _find_cave(
    image: bytes,
    layout: _PeLayout,
    preserved: tuple[_CoffSection, ...],
    first_loader: _CoffSection,
    needed: int,
) -> tuple[int, int] | None:
    """Locate a zero-filled unused region large enough for rescued bytes."""
    if needed <= 0:
        return None
    for section in preserved:
        found = _zero_run(image, section.raw_pointer, section.raw_size, needed)
        if found is not None:
            address = section.virtual_address + (found - section.raw_pointer)
            end = section.virtual_address + max(section.virtual_size, section.raw_size)
            if address + needed <= end:
                return (found, address)
    headers_end = layout.sections_offset + layout.section_count * 40
    found = _zero_run(image, headers_end, first_loader.raw_pointer - headers_end, needed)
    if found is not None and found + needed <= layout.size_of_headers:
        return (found, found)
    return None


def _zero_run(image: bytes, start: int, size: int, needed: int) -> int | None:
    """Find one aligned zero run of at least the needed length."""
    if needed <= 0 or size < needed or start < 0:
        return None
    limit = start + size - needed
    offset = start + (-start % 4)
    while offset <= limit and offset + needed <= len(image):
        if image[offset : offset + needed] == bytes(needed):
            return offset
        offset += 4
    return None


def _recover_tls_directory(
    working: bytearray,
    layout: _PeLayout,
    preserved: tuple[_CoffSection, ...],
    first_loader: _CoffSection,
    source_label: str,
    notes: list[str],
) -> tuple[int, int]:
    """Repoint the thread directory at the preserved template, else zero it."""
    address = _u32(working, layout.directory_offset + _DIRECTORY_TLS * 8)
    size = _u32(working, layout.directory_offset + _DIRECTORY_TLS * 8 + 4)
    if address is None or size is None:
        raise RuntimeError(
            _("cannot restore executable from {path}: truncated headers").format(path=source_label)
        )
    if size == 0:
        return (address, size)
    offset = _offset_from_rva(layout, preserved, address)
    if offset is not None and offset + size <= first_loader.raw_pointer:
        return (address, size)
    template_length = _TLS_TEMPLATE_LENGTH[layout.bitness]
    template = bytes(working[first_loader.raw_pointer : first_loader.raw_pointer + template_length])
    if len(template) != template_length:
        raise RuntimeError(
            _("cannot restore executable from {path}: truncated loader data").format(
                path=source_label
            )
        )
    key = template[:_TLS_LOCATOR_KEY_LENGTH]
    found = _find_template(bytes(working), preserved, key, layout.bitness)
    if found is None:
        notes.append(
            _("no thread template match in {path}; thread directory cleared").format(
                path=source_label
            )
        )
        return (0, 0)
    _found_offset, found_address = found
    return (found_address, _TLS_PRESENT_SIZE[layout.bitness])


def _find_template(
    image: bytes,
    preserved: tuple[_CoffSection, ...],
    key: bytes,
    bitness: int,
) -> tuple[int, int] | None:
    """Search preserved bytes for the template prefix at host alignment."""
    alignment = _HOST_ALIGNMENT[bitness]
    present = _TLS_PRESENT_SIZE[bitness]
    for section in preserved:
        start = section.raw_pointer
        end = section.raw_pointer + section.raw_size
        position = start + (-start % alignment)
        while position + len(key) <= end and position + len(key) <= len(image):
            if image[position : position + len(key)] == key:
                address = section.virtual_address + (position - section.raw_pointer)
                limit = section.virtual_address + max(section.virtual_size, section.raw_size)
                if address + present <= limit:
                    return (position, address)
                break
            position += alignment
    return None


def _splice_executable(
    image: bytes,
    layout: _PeLayout,
    preserved: tuple[_CoffSection, ...],
    *,
    source_label: str,
    import_address: int,
    import_size: int,
    relocation_address: int,
    relocation_size: int,
    exception_address: int,
    exception_size: int,
    tls_address: int,
    tls_size: int,
) -> bytes:
    """Splice loader bytes out, patch headers, and preserve any overlay."""
    first_loader = layout.sections[-2]
    second_loader = layout.sections[-1]
    headers_cut = layout.sections_offset + (layout.section_count - 2) * 40
    raw_cut = first_loader.raw_pointer
    raw_end = second_loader.raw_pointer + second_loader.raw_size
    if raw_end < raw_cut or raw_end > len(image):
        raise RuntimeError(
            _("cannot restore executable from {path}: truncated loader data").format(
                path=source_label
            )
        )
    merged = bytearray()
    merged += image[:headers_cut]
    merged += image[headers_cut + 80 : raw_cut]
    merged += image[raw_end:]
    _patch_u16(merged, layout.section_count_offset, layout.section_count - 2)
    for section in preserved:
        if section.raw_size and section.raw_pointer:
            updated = section.raw_pointer - 80
            if updated <= 0:
                raise RuntimeError(
                    _("cannot restore executable from {path}: corrupt section layout").format(
                        path=source_label
                    )
                )
            _patch_u32(merged, section.header_offset + 20, updated)
    image_end = 0
    for section in preserved:
        image_end = max(image_end, section.virtual_address + section.virtual_size)
    size_of_image = _align_up(image_end, layout.section_alignment)
    if size_of_image <= 0:
        raise RuntimeError(
            _("cannot restore executable from {path}: corrupt section layout").format(
                path=source_label
            )
        )
    _patch_u32(merged, layout.size_of_image_offset, size_of_image)
    _patch_u32(merged, layout.directory_offset + _DIRECTORY_IMPORT * 8, import_address)
    _patch_u32(merged, layout.directory_offset + _DIRECTORY_IMPORT * 8 + 4, import_size)
    _patch_u32(merged, layout.directory_offset + _DIRECTORY_RELOCATION * 8, relocation_address)
    _patch_u32(merged, layout.directory_offset + _DIRECTORY_RELOCATION * 8 + 4, relocation_size)
    _patch_u32(merged, layout.directory_offset + _DIRECTORY_EXCEPTION * 8, exception_address)
    _patch_u32(merged, layout.directory_offset + _DIRECTORY_EXCEPTION * 8 + 4, exception_size)
    _patch_u32(merged, layout.directory_offset + _DIRECTORY_TLS * 8, tls_address)
    _patch_u32(merged, layout.directory_offset + _DIRECTORY_TLS * 8 + 4, tls_size)
    sanity = _parse_pe(bytes(merged))
    if (
        sanity is None
        or sanity.section_count != layout.section_count - 2
        or sanity.bitness != layout.bitness
    ):
        raise RuntimeError(
            _("cannot restore executable from {path}: restoration failed").format(path=source_label)
        )
    return bytes(merged)


def _patch_u16(image: bytearray, offset: int, value: int) -> None:
    """Write one half word into a mutable executable image."""
    image[offset : offset + 2] = value.to_bytes(2, "little")


def _patch_u32(image: bytearray, offset: int, value: int) -> None:
    """Write one word into a mutable executable image."""
    image[offset : offset + 4] = value.to_bytes(4, "little")


def _align_up(value: int, alignment: int) -> int:
    """Round one size up to the given alignment."""
    if alignment <= 0:
        return value
    return (value + alignment - 1) // alignment * alignment


def _write_restored(destination: Path, content: bytes) -> None:
    """Write the restored executable without following symlinks."""
    try:
        if destination.parent != Path("."):
            _ensure_restored_parent(destination.parent)
    except OSError as exc:
        raise RuntimeError(
            _("cannot create restored executable directory {path}: {error}").format(
                path=str(destination), error=exc
            )
        ) from exc
    try:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
    except OSError as exc:
        raise RuntimeError(
            _("cannot write restored executable {path}: {error}").format(
                path=str(destination), error=exc
            )
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
    except OSError as exc:
        raise RuntimeError(
            _("cannot write restored executable {path}: {error}").format(
                path=str(destination), error=exc
            )
        ) from exc


def _ensure_restored_parent(parent: Path) -> None:
    """Create restored-executable parents without following symlinks.

    Components are created one level at a time with symlink checks so a
    link planted in the destination ancestry cannot redirect creation.
    Existing cache staging (empty ``mkdtemp`` under the cache lock) takes
    the same path and keeps identical behavior.
    """
    absolute = parent if parent.is_absolute() else Path.cwd() / parent
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        try:
            entry = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, 0o755)
            except FileExistsError:
                entry = os.lstat(current)
            else:
                continue
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
            raise OSError(f"refusing unsafe restored parent: {current}")
