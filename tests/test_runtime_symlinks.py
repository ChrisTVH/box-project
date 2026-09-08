import os
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from box.errors import RuntimeError
from box.paths import AppPaths
from box.runtime import archive, downloader, easyrpg
from box.runtime.security import validate_runtime_links


def _extract(engine: str, source: Path, destination: Path) -> Path:
    if engine == "easyrpg":
        return easyrpg.extract_runtime(source, destination)
    if engine == "nwjs":
        return archive.extract_runtime(source, destination)
    source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    destination_descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return destination / archive.extract_runtime_at(source_descriptor, destination_descriptor)
    finally:
        os.close(destination_descriptor)
        os.close(source_descriptor)


def _archive(source: Path, links: dict[str, str], *, flat: bool = False) -> None:
    prefix = "" if flat else "runtime/"
    with tarfile.open(source, "w:gz") as tar:
        for name in ("nw", "easyrpg-player", "assets/data"):
            member = tarfile.TarInfo(prefix + name)
            member.size = 4
            member.mode = 0o700
            tar.addfile(member, BytesIO(b"safe"))
        for name, target in links.items():
            member = tarfile.TarInfo(prefix + name)
            member.type = tarfile.SYMTYPE
            member.linkname = target
            tar.addfile(member)


@pytest.mark.parametrize("engine", ("nwjs", "nwjs-fd", "easyrpg"))
@pytest.mark.parametrize(
    "links",
    (
        {"link": "../victim"},
        {"link": "../victim/missing"},
        {"link": "alias/missing", "alias": "../victim"},
        {"link": "missing/../../victim"},
        {"assets/link": "../../victim"},
        {"link": "../runtime/nw"},
    ),
)
def test_runtime_links_cannot_escape_the_final_root(
    tmp_path: Path, engine: str, links: dict[str, str]
) -> None:
    source = tmp_path / "runtime.tar.gz"
    _archive(source, links)
    destination = tmp_path / "staging"
    destination.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")

    with pytest.raises(RuntimeError, match="final runtime root"):
        _extract(engine, source, destination)

    assert tuple(destination.iterdir()) == ()
    assert victim.read_bytes() == b"untouched"


@pytest.mark.parametrize("engine", ("nwjs", "nwjs-fd", "easyrpg"))
def test_internal_links_and_dangling_chains_survive_relocation(tmp_path: Path, engine: str) -> None:
    source = tmp_path / "runtime.tar.gz"
    _archive(
        source,
        {
            "alias": "assets",
            "link": "alias/data",
            "dangling": "alias/missing",
            "assets/back": "../nw",
        },
    )
    destination = tmp_path / "staging"
    destination.mkdir()
    runtime = _extract(engine, source, destination)
    published = tmp_path / "installed"
    runtime.rename(published)

    assert (published / "link").read_bytes() == b"safe"
    assert (published / "assets/back").read_bytes() == b"safe"
    assert (published / "dangling").is_symlink()
    assert (published / "dangling").resolve() == published / "assets/missing"


def test_easyrpg_flat_layout_validates_links_after_normalization(tmp_path: Path) -> None:
    source = tmp_path / "runtime.tar.gz"
    _archive(
        source, {"alias": "assets", "link": "alias/data", "dangling": "alias/missing"}, flat=True
    )
    destination = tmp_path / "staging"
    destination.mkdir()
    runtime = easyrpg.extract_runtime(source, destination)
    published = tmp_path / "installed"
    runtime.rename(published)

    assert (published / "link").read_bytes() == b"safe"
    assert (published / "dangling").resolve() == published / "assets/missing"


@pytest.mark.parametrize("engine", ("nwjs", "nwjs-fd", "easyrpg"))
def test_cyclic_links_fail_closed_without_publication(tmp_path: Path, engine: str) -> None:
    source = tmp_path / "runtime.tar.gz"
    _archive(source, {"first": "second", "second": "first"})
    destination = tmp_path / "staging"
    destination.mkdir()

    with pytest.raises(RuntimeError):
        _extract(engine, source, destination)

    assert tuple(destination.iterdir()) == ()


def test_link_resolution_checks_parent_components_after_following_aliases(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "nw").write_bytes(b"safe")
    (runtime / "alias").symlink_to(".")
    (runtime / "link").symlink_to("alias/../runtime/nw")

    # A final realpath check alone would accept this staging-dependent chain.
    assert (runtime / "link").resolve() == runtime / "nw"
    with pytest.raises(RuntimeError, match="final runtime root"):
        validate_runtime_links(runtime)


@pytest.mark.parametrize("engine", ("nwjs", "easyrpg"))
def test_install_rejects_links_to_catalog_neighbors_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    if engine == "nwjs":
        source = paths.downloads_root / "standard-v0.90.0-linux-x64.tar.gz"
        parent = paths.runtimes_root / "linux-x64"
        target = parent / "standard-v0.90.0"
    else:
        source = paths.easyrpg_downloads_root / "easyrpg-player-0.8.1-linux.tar.gz"
        parent = paths.easyrpg_runtimes_root
        target = parent / "0.8.1"
    parent.mkdir(exist_ok=True)
    victim = parent / "victim"
    victim.write_bytes(b"untouched")
    _archive(source, {"link": "../victim"})
    monkeypatch.setattr(easyrpg, "current_architecture", lambda: "x64")

    with pytest.raises(RuntimeError, match="final runtime root"):
        if engine == "nwjs":
            downloader.install_runtime(paths, "0.90.0", "x64")
        else:
            easyrpg.install_runtime(paths, "0.8.1")

    assert not target.exists()
    assert not tuple(parent.glob(".install-*"))
    assert victim.read_bytes() == b"untouched"
    assert source.is_file()
