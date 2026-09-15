import os
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from box.errors import RuntimeError
from box.runtime import limits
from box.runtime.archive import extract_runtime, extract_runtime_at


def test_extract_runtime_at_resets_the_destination_directory_offset(tmp_path: Path) -> None:
    source = tmp_path / "source"
    runtime = source / "nwjs-v0.115.0-linux-x64"
    runtime.mkdir(parents=True)
    (runtime / "nw").write_text("runtime", encoding="utf-8")
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(runtime, arcname=runtime.name)
    destination = tmp_path / "destination"
    destination.mkdir()
    archive_descriptor = os.open(archive, os.O_RDONLY)
    destination_descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert extract_runtime_at(archive_descriptor, destination_descriptor) == runtime.name
    finally:
        os.close(destination_descriptor)
        os.close(archive_descriptor)


@pytest.mark.parametrize(
    ("constant", "maximum"),
    (("MAX_MEMBER_BYTES", 2), ("MAX_MEMBERS", 1), ("MAX_DEPTH", 1), ("MAX_UNPACKED_BYTES", 1024)),
)
def test_archive_quotas_leave_no_extracted_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constant: str, maximum: int
) -> None:
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("runtime/first", "runtime/second"):
            member = tarfile.TarInfo(name)
            member.size = 3
            tar.addfile(member, BytesIO(b"abc"))
    destination = tmp_path / "destination"
    destination.mkdir()
    monkeypatch.setattr(limits, constant, maximum)
    with pytest.raises(RuntimeError, match="limit"):
        extract_runtime(archive, destination)
    assert tuple(destination.iterdir()) == ()


def test_archive_deadline_cleans_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.addfile(tarfile.TarInfo("runtime/empty"))
    destination = tmp_path / "destination"
    destination.mkdir()
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(limits.time, "monotonic", lambda: next(ticks, 2.0))
    monkeypatch.setattr(limits, "TOTAL_SECONDS", 1)
    with pytest.raises(RuntimeError, match="deadline"):
        extract_runtime(archive, destination)
    assert tuple(destination.iterdir()) == ()


@pytest.mark.parametrize("name", ("../../escape", "/escape"))
def test_data_filter_still_rejects_archive_escapes(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("runtime/link")
        member.type = tarfile.SYMTYPE
        member.linkname = name
        tar.addfile(member)
        tar.addfile(tarfile.TarInfo("runtime/link/file"))
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(RuntimeError):
        extract_runtime(archive, destination)
    assert tuple(destination.iterdir()) == ()
