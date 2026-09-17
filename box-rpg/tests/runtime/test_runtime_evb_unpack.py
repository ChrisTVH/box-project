"""Tests for in-house packed-executable recovery.

Binary fixtures live in ``tests/runtime/fixtures/evb/`` next to these tests:
ten packed executables (x86/x64 across three packer generations) plus the
two pristine programs and the expected virtual text. Small synthetic packed
blobs are built inline instead so record parsing, profile trials,
compression, and edge cases stay self-contained.

White-box note: assertions inspect the internal executable parser on
purpose, so private usage is silenced for this module only.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
import struct
import zlib
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import NamedTuple

import pytest

from box.errors import RuntimeError
from box.runtime import evb_unpack
from box.runtime.evb_unpack import PROFILES, unpack_packed_executable

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "evb"
EXPECTED_TEXT = b"Reading from EVB!"

PACKED_FIXTURES = (
    ("x86_PackerTestApp_packed_20170713.exe", "7_80"),
    ("x86_PackerTestApp_packed_20210329.exe", "9_70"),
    ("x86_PackerTestApp_packed_20240522.exe", "10_70"),
    ("x86_PackerTestApp_packed_20240613.exe", "10_70"),
    ("x86_PackerTestApp_packed_20240826.exe", "10_70"),
    ("x64_PackerTestApp_packed_20170713.exe", "7_80"),
    ("x64_PackerTestApp_packed_20210329.exe", "9_70"),
    ("x64_PackerTestApp_packed_20240522.exe", "10_70"),
    ("x64_PackerTestApp_packed_20240613.exe", "10_70"),
    ("x64_PackerTestApp_packed_20240826.exe", "10_70"),
)

_KIND_FILE = 2
_KIND_FOLDER = 3
_PLACEHOLDER = "%DEFAULT FOLDER%"


class CompressedPayload(NamedTuple):
    """One prebuilt compressed file payload with its unpacked length."""

    stored: bytes
    unpacked: int


type BlobPayload = bytes | CompressedPayload
type BlobEntry = tuple[str, str, BlobPayload | Sequence[BlobEntry]]


def _require_fixtures() -> Path:
    """Return the vendored fixture directory (fail loudly if absent)."""
    assert FIXTURES.is_dir(), f"vendored fixtures missing: {FIXTURES}"
    return FIXTURES


def _readme_validator(directory: Path) -> bool:
    """Accept trees holding the expected virtual text content."""
    candidate = directory / "README.txt"
    return candidate.is_file() and candidate.read_bytes() == EXPECTED_TEXT


# --- Synthetic blob builders (mirror the observed packed layout) ---


def _header(kind: int, children: int, length: int, name: str) -> bytes:
    """Return one 16-byte entry head with name terminator and kind tag."""
    folder = kind == _KIND_FOLDER
    first, second = (1, 1) if folder else (2, 2)
    return (
        struct.pack("<IIII", length, first, second, children)
        + name.encode("utf-16-le")
        + b"\x00\x00"
        + bytes([kind])
    )


def _legacy_trail(unpacked: int, stored: int) -> bytes:
    """Return one 49-byte legacy file trailing record."""
    return (
        b"\x00" * 2
        + struct.pack("<I", unpacked)
        + b"\x00" * 4
        + b"\x00" * 24
        + b"\x00" * 7
        + struct.pack("<I", stored)
        + b"\x00" * 4
    )


def _modern_trail(unpacked: int, stored: int) -> bytes:
    """Return one 53-byte modern file trailing record."""
    return (
        b"\x00" * 2
        + struct.pack("<I", unpacked)
        + b"\x00" * 4
        + b"\x00" * 24
        + b"\x00" * 15
        + struct.pack("<I", stored)
    )


def _compressed_payload(chunks: list[bytes], *, ragged_tail: bool = False) -> tuple[bytes, int]:
    """Build one compressed payload; return (stored bytes, unpacked length)."""
    compressed = [zlib.compress(chunk) for chunk in chunks]
    if ragged_tail and compressed:
        table = b"".join(struct.pack("<I", len(part)) + b"\x00" * 8 for part in compressed[:-1])
        table += struct.pack("<I", len(compressed[-1]))
    else:
        table = b"".join(struct.pack("<I", len(part)) + b"\x00" * 8 for part in compressed)
    own = 8 + len(table)
    return (struct.pack("<II", own, 0) + table + b"".join(compressed), sum(map(len, chunks)))


def _build_blob(entries: Sequence[BlobEntry], grouping: str) -> bytes:
    """Build one synthetic packed blob from (kind, name, payload) entries.

    Folders carry a list of child entries; files carry raw bytes or a
    compressed payload marker.
    """
    head = bytearray(b"EVB\x00" + b"\x00" * 60)
    directory = len(head)
    records = bytearray()
    payloads: list[bytes] = []

    def emit(level: Sequence[BlobEntry]) -> None:
        for kind, name, payload in level:
            if kind == "folder":
                assert isinstance(payload, list)
                children = payload
                filler = b"\x00" * 25
                if grouping == "legacy":
                    record = 16 + len(name.encode("utf-16-le")) + 2 + 1 + 25
                    records.extend(_header(_KIND_FOLDER, len(children), record - 4, name))
                else:
                    records.extend(_header(_KIND_FOLDER, len(children), 0, name))
                records.extend(filler)
                emit(children)
            elif isinstance(payload, CompressedPayload):
                stored_bytes, unpacked = payload.stored, payload.unpacked
                stored = len(stored_bytes)
                if grouping == "legacy":
                    record = 16 + len(name.encode("utf-16-le")) + 2 + 1 + 49
                    records.extend(
                        _header(_KIND_FILE, 0, record - 4, name) + _legacy_trail(unpacked, stored)
                    )
                    records.extend(stored_bytes)
                else:
                    records.extend(
                        _header(_KIND_FILE, 0, 0, name) + _modern_trail(unpacked, stored)
                    )
                    payloads.append(stored_bytes)
            else:
                assert isinstance(payload, bytes)
                if grouping == "legacy":
                    record = 16 + len(name.encode("utf-16-le")) + 2 + 1 + 49
                    records.extend(
                        _header(_KIND_FILE, 0, record - 4, name)
                        + _legacy_trail(len(payload), len(payload))
                    )
                    records.extend(payload)
                else:
                    records.extend(
                        _header(_KIND_FILE, 0, 0, name) + _modern_trail(len(payload), len(payload))
                    )
                    payloads.append(payload)

    emit(entries)
    if grouping == "modern":
        # The first entry head starts one byte into the pad: its zero length
        # field overlaps the root trailing zero and the pad (all zero here).
        body = bytearray(b"\x00" * 16 + b"\x00" * 3)
        assert body[15:19] == b"\x00" * 4
        body[15 : 15 + len(records)] = records
        body.extend(b"\x00" * 4)
        payload_base = directory + len(body)
        for part in payloads:
            body.extend(part)
        body[0:4] = struct.pack("<I", payload_base - directory - 4)
        body[12:16] = struct.pack("<I", len(entries))
        return bytes(head + body)
    body = bytearray(b"\x00" * 16 + b"\x00" * 3 + bytes(records))
    body[0:4] = struct.pack("<I", 15)
    body[12:16] = struct.pack("<I", len(entries))
    return bytes(head + body)


def _wrap(entries: Sequence[BlobEntry]) -> list[BlobEntry]:
    """Nest entries below the default virtual folder placeholder."""
    return [("folder", _PLACEHOLDER, entries)]


# --- Fixture acceptance: every packed executable yields its tree ---


@pytest.mark.parametrize(("filename", "profile"), PACKED_FIXTURES)
def test_packed_fixture_yields_expected_tree(tmp_path: Path, filename: str, profile: str) -> None:
    """Each packed executable extracts the expected virtual text content."""
    fixtures = _require_fixtures()
    destination = tmp_path / "out"
    report = unpack_packed_executable(fixtures / filename, destination, validator=_readme_validator)
    assert report.profile == profile
    assert report.files == 1
    assert report.unpacked_bytes == len(EXPECTED_TEXT)
    assert (destination / "README.txt").read_bytes() == EXPECTED_TEXT
    assert report.restored_executable is not None
    assert report.restored_executable.is_file()
    assert report.restored_executable.parent == destination
    assert report.restored_executable.name == filename


@pytest.mark.parametrize(("filename", "profile"), PACKED_FIXTURES)
def test_packed_fixture_restores_pristine_directories(
    tmp_path: Path, filename: str, profile: str
) -> None:
    """Restored executables recover pristine directory values and sections."""
    fixtures = _require_fixtures()
    bits = "x64" if filename.startswith("x64") else "x86"
    pristine = (fixtures / f"{bits}_PackerTestApp.exe").read_bytes()
    pristine_layout = evb_unpack._parse_pe(pristine)
    assert pristine_layout is not None
    packed_layout = evb_unpack._parse_pe((fixtures / filename).read_bytes())
    assert packed_layout is not None
    report = unpack_packed_executable(
        fixtures / filename, tmp_path / "out", validator=_readme_validator
    )
    assert report.profile == profile
    assert report.warnings == ()
    restored = report.restored_executable
    assert restored is not None
    restored_bytes = restored.read_bytes()
    restored_layout = evb_unpack._parse_pe(restored_bytes)
    assert restored_layout is not None
    assert restored_layout.bitness == pristine_layout.bitness
    assert restored_layout.section_count == pristine_layout.section_count
    assert restored_layout.section_count == packed_layout.section_count - 2
    for index in (1, 3, 5, 9):
        expected = pristine[
            pristine_layout.directory_offset + index * 8 : pristine_layout.directory_offset
            + index * 8
            + 8
        ]
        actual = restored_bytes[
            restored_layout.directory_offset + index * 8 : restored_layout.directory_offset
            + index * 8
            + 8
        ]
        assert actual == expected, f"directory {index} differs from pristine"
    pristine_image_size = int.from_bytes(
        pristine[pristine_layout.size_of_image_offset : pristine_layout.size_of_image_offset + 4],
        "little",
    )
    restored_image_size = int.from_bytes(
        restored_bytes[
            restored_layout.size_of_image_offset : restored_layout.size_of_image_offset + 4
        ],
        "little",
    )
    assert restored_image_size == pristine_image_size
    assert _has_no_symlinks(tmp_path / "out")


def test_restored_executable_repairs_code_records(tmp_path: Path) -> None:
    """Rescued 64-bit code records match the pristine section bytes."""
    fixtures = _require_fixtures()
    filename = "x64_PackerTestApp_packed_20240522.exe"
    pristine = (fixtures / "x64_PackerTestApp.exe").read_bytes()
    pristine_layout = evb_unpack._parse_pe(pristine)
    assert pristine_layout is not None
    report = unpack_packed_executable(
        fixtures / filename, tmp_path / "out", validator=_readme_validator
    )
    restored = report.restored_executable
    assert restored is not None
    restored_bytes = restored.read_bytes()
    restored_layout = evb_unpack._parse_pe(restored_bytes)
    assert restored_layout is not None
    pristine_code = pristine_layout.sections[3]
    restored_code = restored_layout.sections[3]
    assert restored_code.virtual_address == pristine_code.virtual_address
    assert (
        restored_bytes[
            restored_code.raw_pointer : restored_code.raw_pointer + pristine_code.virtual_size
        ]
        == pristine[
            pristine_code.raw_pointer : pristine_code.raw_pointer + pristine_code.virtual_size
        ]
    )


def _has_no_symlinks(root: Path) -> bool:
    """Return False when any extracted entry is a symbolic link."""
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink():
                    return False
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
    return True


# --- Synthetic parsing, trials, and compression ---


def test_modern_blob_roundtrip_with_nesting(tmp_path: Path) -> None:
    """Modern grouping reconstructs nested trees, empty folders, and files."""
    blob = _build_blob(
        _wrap(
            [
                ("file", "top.txt", b"top"),
                ("folder", "sub", [("file", "inner.txt", b"inner"), ("folder", "empty", [])]),
                ("file", "blank.bin", b""),
            ]
        ),
        "modern",
    )
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    destination = tmp_path / "out"
    seen: list[Path] = []

    def validator(directory: Path) -> bool:
        seen.append(directory)
        return (directory / "top.txt").is_file()

    report = unpack_packed_executable(
        source, destination, validator=validator, restore_executable=False
    )
    assert report.profile == "10_70"
    assert report.files == 3
    assert (destination / "top.txt").read_bytes() == b"top"
    assert (destination / "sub" / "inner.txt").read_bytes() == b"inner"
    assert (destination / "sub" / "empty").is_dir()
    assert (destination / "blank.bin").read_bytes() == b""
    assert seen and seen[0] != destination


def test_modern_blob_without_executable_restoration(tmp_path: Path) -> None:
    """Tree-only runs skip restoration with a warning and no output binary."""
    blob = _build_blob(_wrap([("file", "a.txt", b"a")]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    destination = tmp_path / "out"
    report = unpack_packed_executable(
        source, destination, validator=lambda directory: True, restore_executable=False
    )
    assert (destination / "a.txt").read_bytes() == b"a"
    assert report.restored_executable is None
    assert any("restoration skipped" in warning for warning in report.warnings)


def test_legacy_blob_wins_oldest_trial(tmp_path: Path) -> None:
    """A legacy-grouped blob fails newer trials and reports the oldest."""
    blob = _build_blob(_wrap([("file", "old.txt", b"legacy-bytes")]), "legacy")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    destination = tmp_path / "out"
    report = unpack_packed_executable(
        source, destination, validator=lambda directory: True, restore_executable=False
    )
    assert report.profile == "7_80"
    assert (destination / "old.txt").read_bytes() == b"legacy-bytes"


def test_compressed_payload_with_ragged_table(tmp_path: Path) -> None:
    """Every-third-value tables decode independent chunks, tolerating tails."""
    stored, unpacked = _compressed_payload([b"alpha-" * 10, b"beta-" * 10], ragged_tail=True)
    blob = _build_blob(_wrap([("file", "data.bin", CompressedPayload(stored, unpacked))]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    destination = tmp_path / "out"
    report = unpack_packed_executable(
        source, destination, validator=lambda directory: True, restore_executable=False
    )
    assert report.files == 1
    assert (destination / "data.bin").read_bytes() == b"alpha-" * 10 + b"beta-" * 10
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    destination = tmp_path / "out"
    report = unpack_packed_executable(
        source, destination, validator=lambda directory: True, restore_executable=False
    )
    assert report.files == 1
    assert (destination / "data.bin").read_bytes() == b"alpha-" * 10 + b"beta-" * 10


def test_compressed_legacy_payload_roundtrip(tmp_path: Path) -> None:
    """Legacy grouping streams compressed payloads one chunk at a time."""
    stored, unpacked = _compressed_payload([b"0123456789" * 50])
    blob = _build_blob(_wrap([("file", "data.bin", CompressedPayload(stored, unpacked))]), "legacy")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    destination = tmp_path / "out"
    report = unpack_packed_executable(
        source, destination, validator=lambda directory: True, restore_executable=False
    )
    assert report.files == 1
    assert (destination / "data.bin").read_bytes() == b"0123456789" * 50


def test_rejected_validator_fails_valid_tree(tmp_path: Path) -> None:
    """A non-empty tree is necessary but not sufficient without acceptance."""
    blob = _build_blob(_wrap([("file", "a.txt", b"a")]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    with pytest.raises(RuntimeError, match="no valid file tree"):
        unpack_packed_executable(
            source,
            tmp_path / "out",
            validator=lambda directory: False,
            restore_executable=False,
        )


def test_progress_callback_reports_totals(tmp_path: Path) -> None:
    """Progress arrives through the receiver with a final exact total."""
    blob = _build_blob(_wrap([("file", "a.txt", b"aaa"), ("file", "b.txt", b"bb")]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    events: list[tuple[int, int]] = []
    report = unpack_packed_executable(
        source,
        tmp_path / "out",
        validator=lambda directory: True,
        restore_executable=False,
        progress=lambda files, total: events.append((files, total)),
    )
    assert report.files == 2
    assert events
    assert events[-1] == (2, 5)
    assert all(first <= second for first, second in events)


def test_extraction_stays_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Reusable extraction logic never writes directly to the terminal."""
    blob = _build_blob(_wrap([("file", "a.txt", b"a")]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    unpack_packed_executable(
        source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# --- Edge cases and safety ---


def test_unknown_data_is_not_packed(tmp_path: Path) -> None:
    """Inputs without the signature fail fast and create no output."""
    source = tmp_path / "plain.exe"
    source.write_bytes(b"MZ" + os.urandom(512))
    destination = tmp_path / "out"
    with pytest.raises(RuntimeError, match="not a packed executable"):
        unpack_packed_executable(source, destination, validator=lambda directory: True)
    assert not destination.exists()


def test_project_data_file_is_not_packed(tmp_path: Path) -> None:
    """The example project file fails recognition without extraction."""
    fixtures = _require_fixtures()
    with pytest.raises(RuntimeError, match="not a packed executable"):
        unpack_packed_executable(
            fixtures / "PackerProject.evb",
            tmp_path / "out",
            validator=lambda directory: True,
        )


def test_pristine_executable_is_not_packed(tmp_path: Path) -> None:
    """Unpacked executables are rejected before any restoration attempt."""
    fixtures = _require_fixtures()
    with pytest.raises(RuntimeError, match="not a packed executable"):
        unpack_packed_executable(
            fixtures / "x86_PackerTestApp.exe",
            tmp_path / "out",
            validator=lambda directory: True,
        )


def test_missing_input_writes_nothing(tmp_path: Path) -> None:
    """Missing inputs are hard errors that write nothing."""
    with pytest.raises(RuntimeError, match="cannot read packed executable"):
        unpack_packed_executable(
            tmp_path / "absent.exe", tmp_path / "out", validator=lambda directory: True
        )
    assert not (tmp_path / "out").exists()


def test_truncated_input_exhausts_profiles(tmp_path: Path) -> None:
    """Truncated directories fail every profile with the input named."""
    blob = _build_blob(_wrap([("file", "a.txt", b"a" * 100)]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob[:120])
    with pytest.raises(RuntimeError, match=r"packed\.exe"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )


def test_empty_tree_is_an_error(tmp_path: Path) -> None:
    """A zero top-level count fails even though parsing succeeds."""
    image = bytearray(b"EVB\x00" + b"\x00" * 60)
    image.extend(struct.pack("<I", 12) + b"\x00" * 8 + struct.pack("<I", 0))
    source = tmp_path / "packed.exe"
    source.write_bytes(bytes(image))
    with pytest.raises(RuntimeError, match="empty extraction"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )


@pytest.mark.parametrize("name", ["a/b", "..", ".", "", "C:\\game", "a\\b", "C:x"])
def test_unsafe_names_are_refused(tmp_path: Path, name: str) -> None:
    """Separator, reserved, absolute, and empty names leave zero files."""
    blob = _build_blob(_wrap([("file", name, b"payload")]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    with pytest.raises(RuntimeError, match=r"empty extraction|no valid file tree"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )
    assert not any(entry.is_file() for entry in (tmp_path / "out").rglob("*"))


def test_refused_branch_keeps_siblings(tmp_path: Path) -> None:
    """One refused branch is skipped while valid siblings still extract."""
    blob = _build_blob(
        _wrap([("file", "good.txt", b"good"), ("file", "../evil", b"evil")]), "modern"
    )
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    destination = tmp_path / "out"
    report = unpack_packed_executable(
        source, destination, validator=lambda directory: True, restore_executable=False
    )
    assert report.files == 1
    assert (destination / "good.txt").read_bytes() == b"good"
    assert report.notes


def test_refused_names_stay_confined(tmp_path: Path) -> None:
    """Parent escapes never write outside the output root."""
    blob = _build_blob(_wrap([("file", "..", b"evil")]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    with pytest.raises(RuntimeError, match=r"empty extraction|no valid file tree"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )
    assert (tmp_path / "out").exists()
    assert not (tmp_path / "evil").exists()


def test_unknown_kind_ends_modern_enumeration(tmp_path: Path) -> None:
    """Unknown kind tags stop modern sibling consumption without failing."""
    entries: list[BlobEntry] = [
        ("file", "first.txt", b"first"),
        ("file", "mystery", b"junk"),
        ("file", "second.txt", b"second"),
    ]
    blob = bytearray(_build_blob(_wrap(entries), "modern"))
    needle = "mystery".encode("utf-16-le") + b"\x00\x00" + bytes([_KIND_FILE])
    at = bytes(blob).find(needle)
    assert at > 0
    blob[at + len(needle) - 1] = 9
    source = tmp_path / "packed.exe"
    source.write_bytes(bytes(blob))
    destination = tmp_path / "out"
    report = unpack_packed_executable(
        source, destination, validator=lambda directory: True, restore_executable=False
    )
    assert (destination / "first.txt").read_bytes() == b"first"
    assert not (destination / "second.txt").exists()
    assert any("unknown directory entry" in note for note in report.notes)


def test_unknown_kind_skips_legacy_record(tmp_path: Path) -> None:
    """Length-driven legacy reads skip unknown records and continue."""
    entries: list[BlobEntry] = [
        ("file", "first.txt", b"first"),
        ("folder", "mystery", []),
        ("file", "second.txt", b"second"),
    ]
    blob = bytearray(_build_blob(_wrap(entries), "legacy"))
    needle = "mystery".encode("utf-16-le") + b"\x00\x00" + bytes([_KIND_FOLDER])
    at = bytes(blob).find(needle)
    assert at > 0
    blob[at + len(needle) - 1] = 9
    source = tmp_path / "packed.exe"
    source.write_bytes(bytes(blob))
    destination = tmp_path / "out"
    report = unpack_packed_executable(
        source, destination, validator=lambda directory: True, restore_executable=False
    )
    assert (destination / "first.txt").read_bytes() == b"first"
    assert (destination / "second.txt").read_bytes() == b"second"
    assert any("unknown directory entry" in note for note in report.notes)


def test_oversized_declaration_hits_budget(tmp_path: Path) -> None:
    """Declared lengths are checked against budgets before writing harm."""
    stored, _unpacked = _compressed_payload([b"x"])
    blob = bytearray(
        _build_blob(_wrap([("file", "big.bin", CompressedPayload(stored, 1))]), "modern")
    )
    needle = struct.pack("<I", 1) + b"\x00" * 4
    at = bytes(blob).find(needle)
    assert at > 0
    blob[at : at + 4] = struct.pack("<I", 2**31)
    source = tmp_path / "packed.exe"
    source.write_bytes(bytes(blob))
    with pytest.raises(RuntimeError, match="budget"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )


def test_deep_nesting_hits_budget(tmp_path: Path) -> None:
    """Nesting beyond the depth budget fails the run cleanly."""
    level: list[BlobEntry] = [("file", "deep.txt", b"deep")]
    for depth in range(40):
        level = [("folder", f"dir{depth}", level)]
    blob = _build_blob(level, "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    with pytest.raises(RuntimeError, match="budget"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )


def test_huge_entry_count_hits_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Entry counts beyond budget fail before thousands of files land."""
    monkeypatch.setattr(evb_unpack, "_MAX_ENTRIES", 100)
    entries = [(("file", f"file{i:05d}.txt", b"x")) for i in range(101)]
    blob = _build_blob(_wrap(entries), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    with pytest.raises(RuntimeError, match="budget"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )


def test_corrupt_stored_length_exhausts_profiles(tmp_path: Path) -> None:
    """Stored-length accounting mismatches reject every profile trial."""
    stored, unpacked = _compressed_payload([b"data-blob"])
    blob = bytearray(
        _build_blob(_wrap([("file", "data.bin", CompressedPayload(stored, unpacked))]), "modern")
    )
    at = bytes(blob).find(stored[:8])
    assert at > 0
    own = int.from_bytes(stored[0:4], "little")
    blob[at : at + 4] = struct.pack("<I", own + 4)
    source = tmp_path / "packed.exe"
    source.write_bytes(bytes(blob))
    with pytest.raises(RuntimeError, match=r"empty extraction|no valid file tree"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )


def test_undecodable_chunk_exhausts_profiles(tmp_path: Path) -> None:
    """Chunks outside every stream wrapper fail the active profile trial."""
    stored, unpacked = _compressed_payload([b"data-blob"])
    blob = bytearray(
        _build_blob(_wrap([("file", "data.bin", CompressedPayload(stored, unpacked))]), "modern")
    )
    at = bytes(blob).find(stored)
    assert at > 0
    own = int.from_bytes(stored[0:4], "little")
    chunk_start = at + own
    blob[chunk_start : at + len(stored)] = os.urandom(len(stored) - own)
    source = tmp_path / "packed.exe"
    source.write_bytes(bytes(blob))
    with pytest.raises(RuntimeError, match=r"empty extraction|no valid file tree"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )


def test_length_mismatch_fails_file(tmp_path: Path) -> None:
    """Decompressed size mismatches fail the file and then the empty run."""
    stored, _unpacked = _compressed_payload([b"partial-content"])
    blob = _build_blob(_wrap([("file", "data.bin", CompressedPayload(stored, 9999))]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    with pytest.raises(RuntimeError, match=r"empty extraction|no valid file tree"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )


# --- Restoration modes ---


def test_restore_only_skips_tree_with_warning(tmp_path: Path) -> None:
    """Restoration-only runs still write the executable beside no tree."""
    fixtures = _require_fixtures()
    filename = "x86_PackerTestApp_packed_20210329.exe"
    destination = tmp_path / "out"
    report = unpack_packed_executable(
        fixtures / filename, destination, validator=_readme_validator, extract_tree=False
    )
    assert report.profile == "9_70"
    assert report.files == 0
    assert not (destination / "README.txt").exists()
    assert report.restored_executable is not None
    assert report.restored_executable.is_file()
    assert any("tree reconstruction skipped" in warning for warning in report.warnings)


def test_restore_only_selects_newest_plausible_profile(tmp_path: Path) -> None:
    """Without a tree trial the loader header alone orders profile choice."""
    fixtures = _require_fixtures()
    for filename, profile in (
        ("x64_PackerTestApp_packed_20240522.exe", "10_70"),
        ("x64_PackerTestApp_packed_20170713.exe", "7_80"),
    ):
        report = unpack_packed_executable(
            fixtures / filename,
            tmp_path / f"out-{profile}",
            validator=_readme_validator,
            extract_tree=False,
        )
        assert report.profile == profile


def test_explicit_output_path_for_restored_executable(tmp_path: Path) -> None:
    """An explicit restored-executable path is honored with parents made."""
    fixtures = _require_fixtures()
    out_pe = tmp_path / "nested" / "restored.exe"
    report = unpack_packed_executable(
        fixtures / "x86_PackerTestApp_packed_20210329.exe",
        tmp_path / "out",
        validator=_readme_validator,
        out_pe=out_pe,
    )
    assert report.restored_executable == out_pe
    assert out_pe.is_file()


def test_non_executable_restore_is_refused(tmp_path: Path) -> None:
    """Signature-bearing non-executables fail restoration, not extraction."""
    blob = _build_blob(_wrap([("file", "a.txt", b"a")]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    with pytest.raises(RuntimeError, match="not a valid executable"):
        unpack_packed_executable(source, tmp_path / "out", validator=lambda directory: True)


def test_unknown_profile_is_rejected(tmp_path: Path) -> None:
    """Unknown profile selectors fail before any work starts."""
    with pytest.raises(ValueError, match="unknown compatibility profile"):
        unpack_packed_executable(
            tmp_path / "packed.exe",
            tmp_path / "out",
            profiles=(("unknown", "modern"),),
        )


def test_empty_profile_list_is_rejected(tmp_path: Path) -> None:
    """An empty trial list is a programming error, not a quiet no-op."""
    with pytest.raises(ValueError, match="at least one compatibility profile"):
        unpack_packed_executable(tmp_path / "packed.exe", tmp_path / "out", profiles=())


def test_disabling_everything_is_rejected(tmp_path: Path) -> None:
    """Disabling both tree and executable recovery is a programming error."""
    with pytest.raises(ValueError, match="nothing to do"):
        unpack_packed_executable(
            tmp_path / "packed.exe",
            tmp_path / "out",
            extract_tree=False,
            restore_executable=False,
        )


def test_profile_names_cover_trial_order() -> None:
    """The newest-first trial order stays explicit and stable."""
    assert [name for name, _grouping in PROFILES] == ["10_70", "9_70", "7_80"]


def test_duplicate_sibling_names_do_not_leak_internal_errors(tmp_path: Path) -> None:
    """A file and folder sharing one name is noted while siblings continue."""
    blob = _build_blob(
        _wrap(
            [
                ("file", "a", b"content"),
                ("folder", "a", [("file", "inner.txt", b"inner")]),
            ]
        ),
        "modern",
    )
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    destination = tmp_path / "out"
    try:
        report = unpack_packed_executable(
            source, destination, validator=lambda directory: True, restore_executable=False
        )
    except (
        evb_unpack._BranchRefused,
        evb_unpack._StopLevel,
        evb_unpack._ProfileMismatch,
        evb_unpack._EmptyTree,
    ) as exc:
        pytest.fail(f"internal exception leaked: {type(exc).__name__}")
    assert report.files >= 1
    assert (destination / "a").read_bytes() == b"content"
    assert report.notes


def test_source_open_uses_nonblocking_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Source opens avoid blocking on pipes or FIFOs."""
    blob = _build_blob(_wrap([("file", "a.txt", b"a")]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    seen: list[int] = []
    real_open = os.open

    def recording_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(str(path)).name == "packed.exe" and (flags & os.O_ACCMODE) == os.O_RDONLY:
            seen.append(flags)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", recording_open)
    unpack_packed_executable(
        source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
    )
    assert seen, "source open was not observed"
    assert all(flags & os.O_NONBLOCK for flags in seen)


def test_shared_total_budget_across_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cumulative extraction across profiles honors one total budget."""
    monkeypatch.setattr(evb_unpack, "_MAX_TOTAL_UNPACKED_BYTES", 10)
    blob = _build_blob(_wrap([("file", "a.txt", b"123456")]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    with pytest.raises(RuntimeError, match="budget"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )


def test_chunk_table_bounded_before_materializing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many chunks fail with a budget error before a huge table builds."""
    monkeypatch.setattr(evb_unpack, "_MAX_ENTRIES", 100)
    stored, unpacked = _compressed_payload([b"x"] * 101)
    blob = _build_blob(_wrap([("file", "data.bin", CompressedPayload(stored, unpacked))]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    with pytest.raises(RuntimeError, match="budget"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )


def test_stored_length_charged_before_slicing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compressed stored bytes count against the per-file budget."""
    monkeypatch.setattr(evb_unpack, "_MAX_FILE_BYTES", 10)
    stored, unpacked = _compressed_payload([b"hello"])
    assert len(stored) > 10
    assert unpacked == len(b"hello")
    blob = _build_blob(_wrap([("file", "data.bin", CompressedPayload(stored, unpacked))]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    with pytest.raises(RuntimeError, match="budget"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=lambda directory: True, restore_executable=False
        )


def test_modern_corrupt_file_skipped_siblings_continue(tmp_path: Path) -> None:
    """One undecodable modern file does not abort its siblings."""
    bad_stored, bad_unpacked = _compressed_payload([b"bad-content"])
    blob = bytearray(
        _build_blob(
            _wrap(
                [
                    ("file", "good1.txt", b"good-one"),
                    ("file", "bad.bin", CompressedPayload(bad_stored, bad_unpacked)),
                    ("file", "good2.txt", b"good-two"),
                ]
            ),
            "modern",
        )
    )
    at = bytes(blob).find(bad_stored)
    assert at > 0
    own = int.from_bytes(bad_stored[0:4], "little")
    chunk_start = at + own
    blob[chunk_start : at + len(bad_stored)] = os.urandom(len(bad_stored) - own)
    source = tmp_path / "packed.exe"
    source.write_bytes(bytes(blob))
    destination = tmp_path / "out"
    report = unpack_packed_executable(
        source, destination, validator=lambda directory: True, restore_executable=False
    )
    assert report.files == 2
    assert (destination / "good1.txt").read_bytes() == b"good-one"
    assert (destination / "good2.txt").read_bytes() == b"good-two"
    assert any("cannot decode" in note for note in report.notes)


def test_nested_unknown_kind_keeps_parent_siblings(tmp_path: Path) -> None:
    """Unknown inside a folder ends only that branch."""
    entries: list[BlobEntry] = [
        (
            "folder",
            "sub",
            [("file", "good_inside.txt", b"good-inside"), ("file", "mystery", b"")],
        ),
        ("file", "after.txt", b"after"),
    ]
    blob = bytearray(_build_blob(_wrap(entries), "modern"))
    needle = "mystery".encode("utf-16-le") + b"\x00\x00" + bytes([_KIND_FILE])
    at = bytes(blob).find(needle)
    assert at > 0
    blob[at + len(needle) - 1] = 9
    source = tmp_path / "packed.exe"
    source.write_bytes(bytes(blob))
    destination = tmp_path / "out"
    report = unpack_packed_executable(
        source, destination, validator=lambda directory: True, restore_executable=False
    )
    assert (destination / "sub" / "good_inside.txt").read_bytes() == b"good-inside"
    assert (destination / "after.txt").read_bytes() == b"after"
    assert any("unknown directory entry" in note for note in report.notes)


def test_raising_validator_rejects_profile_cleanly(tmp_path: Path) -> None:
    """A raising validator never leaks its own exception type."""
    blob = _build_blob(_wrap([("file", "a.txt", b"a")]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)

    def bad_validator(directory: Path) -> bool:
        del directory
        raise ValueError("validator boom")

    with pytest.raises(RuntimeError, match=r"empty extraction|no valid file tree"):
        unpack_packed_executable(
            source, tmp_path / "out", validator=bad_validator, restore_executable=False
        )


def test_invalid_profile_grouping_combo_rejected(tmp_path: Path) -> None:
    """Swapped groupings are rejected before any work starts."""
    with pytest.raises(ValueError, match="unknown compatibility profile"):
        unpack_packed_executable(
            tmp_path / "packed.exe",
            tmp_path / "out",
            profiles=(("10_70", "legacy"),),
        )


def test_progress_replays_only_winner_monotonic(tmp_path: Path) -> None:
    """Losing attempts stay silent; winner events stay monotonic."""
    blob = _build_blob(_wrap([("file", "a.txt", b"aaa"), ("file", "b.txt", b"bb")]), "modern")
    source = tmp_path / "packed.exe"
    source.write_bytes(blob)
    calls = 0

    def counting_validator(directory: Path) -> bool:
        del directory
        nonlocal calls
        calls += 1
        return calls > 1

    events: list[tuple[int, int]] = []
    report = unpack_packed_executable(
        source,
        tmp_path / "out",
        validator=counting_validator,
        restore_executable=False,
        progress=lambda files, total: events.append((files, total)),
    )
    assert report.files == 2
    assert events, "no progress events replayed"
    for (previous_files, previous_bytes), (current_files, current_bytes) in pairwise(events):
        assert current_files >= previous_files
        assert current_bytes >= previous_bytes
    assert events[-1] == (report.files, report.unpacked_bytes)
    assert len(events) <= 3


def test_restored_parent_refuses_symlink(tmp_path: Path) -> None:
    """Restored-executable parents never follow symlinks."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    destination = link / "nested" / "restored.exe"
    with pytest.raises(RuntimeError, match="cannot create restored executable directory"):
        evb_unpack._write_restored(destination, b"fake")
    assert not (real / "nested" / "restored.exe").exists()


def _build_minimal_pe(loader_words: dict[int, int]) -> bytes:
    """Build a minimal 32-bit PE with one preserved section and two loaders."""
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    dos[0x3C : 0x3C + 4] = (64).to_bytes(4, "little")
    coff = struct.pack("<HHIIIHH", 0x14C, 3, 0, 0, 0, 224, 0x010F)
    optional = bytearray(224)
    optional[0:2] = (0x10B).to_bytes(2, "little")
    optional[32:36] = (0x1000).to_bytes(4, "little")
    optional[56:60] = (0x4000).to_bytes(4, "little")
    optional[60:64] = (0x200).to_bytes(4, "little")
    optional[92:96] = (16).to_bytes(4, "little")
    sections = bytearray()
    for name, virtual_size, virtual_address, raw_size, raw_pointer in (
        (b".text\x00\x00\x00", 0x1000, 0x1000, 0x1000, 0x200),
        (b".load1\x00\x00", 0x100, 0x2000, 0x100, 0x1200),
        (b".load2\x00\x00", 0x100, 0x3000, 0x100, 0x1300),
    ):
        sections += name
        sections += struct.pack("<IIII", virtual_size, virtual_address, raw_size, raw_pointer)
        sections += b"\x00" * 16
    image = bytearray(0x1400)
    image[0:64] = dos
    image[64:68] = b"PE\x00\x00"
    image[68:88] = coff
    image[88 : 88 + 224] = optional
    image[312 : 312 + len(sections)] = sections
    base = 0x1200
    for offset, value in loader_words.items():
        image[base + offset : base + offset + 4] = value.to_bytes(4, "little")
    return bytes(image)


def test_synthetic_pe_restore_32bit_10_70() -> None:
    """A hand-built PE restores without external fixtures."""
    image = _build_minimal_pe({84: 0x1000, 88: 0x20, 92: 0x1100, 96: 0x30})
    layout = evb_unpack._parse_pe(image)
    assert layout is not None
    assert layout.bitness == 32
    assert layout.section_count == 3
    assert evb_unpack._loader_values_validate(image, "10_70", "synthetic.exe")
    warnings: list[str] = []
    notes: list[str] = []
    restored = evb_unpack._restore_executable(
        image,
        layout,
        source_label="synthetic.exe",
        profile="10_70",
        warnings=warnings,
        notes=notes,
    )
    restored_layout = evb_unpack._parse_pe(restored)
    assert restored_layout is not None
    assert restored_layout.bitness == 32
    assert restored_layout.section_count == layout.section_count - 2
    directory = restored_layout.directory_offset
    import_address = int.from_bytes(restored[directory + 8 : directory + 12], "little")
    import_size = int.from_bytes(restored[directory + 12 : directory + 16], "little")
    relocation_address = int.from_bytes(restored[directory + 40 : directory + 44], "little")
    relocation_size = int.from_bytes(restored[directory + 44 : directory + 48], "little")
    assert (import_address, import_size) == (0x1000, 0x20)
    assert (relocation_address, relocation_size) == (0x1100, 0x30)
    assert warnings == []


def test_restore_profile_prefers_preserved_range() -> None:
    """Restore-only selection skips a degraded profile for a clean one."""
    image = _build_minimal_pe({80: 0x1000, 84: 0x20, 88: 0x1100, 92: 0x30, 96: 0x40})
    layout = evb_unpack._parse_pe(image)
    assert layout is not None
    assert not evb_unpack._loader_values_validate(image, "10_70", "synthetic.exe")
    assert evb_unpack._loader_values_validate(image, "9_70", "synthetic.exe")
    assert evb_unpack._select_restore_profile(image, layout, PROFILES) == "9_70"
