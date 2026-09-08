import os
import tarfile
from pathlib import Path

from box.runtime.archive import extract_runtime_at


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
