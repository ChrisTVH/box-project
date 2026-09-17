"""Tests for packed-executable candidate detection and profile-backed unpacking."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from box.errors import ConfigurationError, RuntimeError
from box.games.identity import game_id
from box.launch.profiles import ProfileCatalog
from box.paths import AppPaths
from box.runtime import evb as evb_module
from box.runtime import evb_unpack
from box.runtime.evb_unpack import UnpackReport
from box.runtime.security import cache_lock


def _paths(tmp_path: Path) -> AppPaths:
    """Return isolated launcher paths below the test directory."""
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


def _source(root: Path, name: str = "Game.exe") -> Path:
    """Create a source directory holding one executable candidate."""
    root.mkdir(parents=True, exist_ok=True)
    executable = root / name
    executable.write_bytes(b"fake packed executable")
    return executable


def _profile_game(paths: AppPaths, executable: Path) -> Path:
    """Return the expected profile game tree for one packed source."""
    return paths.profiles_root.resolve(strict=False) / game_id(executable.parent) / "game"


def _marker_for(game_path: Path) -> Path:
    """Return the staleness marker beside one profile game tree."""
    return game_path.parent / ".evb.json"


def _mv_tree(root: Path) -> Path:
    """Create a minimal detectable RPG Maker MV tree."""
    (root / "www" / "js").mkdir(parents=True)
    (root / "www" / "index.html").write_text("fixture", encoding="utf-8")
    (root / "www" / "js" / "plugins.js").write_text("fixture", encoding="utf-8")
    (root / "package.json").write_text('{"name": "fixture"}', encoding="utf-8")
    return root


def _rpg_rt_tree(root: Path) -> Path:
    """Create a minimal detectable RPG Maker 2000/2003 tree."""
    root.mkdir(parents=True, exist_ok=True)
    for filename in ("RPG_RT.ini", "RPG_RT.ldb", "RPG_RT.lmt"):
        (root / filename).write_text("fixture", encoding="utf-8")
    return root


# --- Candidate rules ---


def test_single_custom_executable_is_a_candidate(tmp_path: Path) -> None:
    """Any single basename qualifies; names are never hardcoded."""
    executable = _source(tmp_path / "game", "My Custom Game.exe")
    assert evb_module.find_packed_executable(tmp_path / "game") == executable


def test_uppercase_suffix_is_a_candidate(tmp_path: Path) -> None:
    """Suffix matching ignores case like the host file system would not."""
    executable = _source(tmp_path / "game", "GAME.EXE")
    assert evb_module.find_packed_executable(tmp_path / "game") == executable


def test_multiple_executables_are_rejected(tmp_path: Path) -> None:
    """Two executables never unpack: the source is ambiguous."""
    _source(tmp_path / "game", "first.exe")
    _source(tmp_path / "game", "second.exe")
    assert evb_module.find_packed_executable(tmp_path / "game") is None


def test_no_executable_is_rejected(tmp_path: Path) -> None:
    """Executable-free directories are not candidates."""
    (tmp_path / "game").mkdir()
    assert evb_module.find_packed_executable(tmp_path / "game") is None


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    """Missing paths defer to game detection instead of failing here."""
    assert evb_module.find_packed_executable(tmp_path / "absent") is None


def test_file_path_is_rejected(tmp_path: Path) -> None:
    """A direct executable path is not a candidate directory."""
    executable = _source(tmp_path / "game")
    assert evb_module.find_packed_executable(executable) is None


@pytest.mark.parametrize("layout", ["www", "WWW", "Data", "data", "DATA"])
def test_unpacked_layouts_are_rejected(tmp_path: Path, layout: str) -> None:
    """Exported web and data layouts never route through unpacking."""
    _source(tmp_path / "game")
    (tmp_path / "game" / layout).mkdir()
    assert evb_module.find_packed_executable(tmp_path / "game") is None


def test_detectable_easyrpg_tree_is_rejected(tmp_path: Path) -> None:
    """Already detectable projects launch directly without unpacking."""
    _rpg_rt_tree(tmp_path / "game")
    (tmp_path / "game" / "extra.exe").write_bytes(b"fake")
    assert evb_module.find_packed_executable(tmp_path / "game") is None


def test_detectable_mv_tree_is_rejected(tmp_path: Path) -> None:
    """Detectable exports with a stray executable still launch directly."""
    _mv_tree(tmp_path / "game")
    (tmp_path / "game" / "extra.exe").write_bytes(b"fake")
    assert evb_module.find_packed_executable(tmp_path / "game") is None


def test_symlinked_executable_is_rejected(tmp_path: Path) -> None:
    """Links never qualify: only regular files are candidates."""
    game = tmp_path / "game"
    game.mkdir()
    target = tmp_path / "real.exe"
    target.write_bytes(b"fake")
    os.symlink(target, game / "link.exe")
    assert evb_module.find_packed_executable(game) is None


def test_directory_suffixed_exe_is_rejected(tmp_path: Path) -> None:
    """Directories named like executables do not count as files."""
    game = tmp_path / "game"
    (game / "folder.exe").mkdir(parents=True)
    assert evb_module.find_packed_executable(game) is None


# --- Tree validator ---


def test_default_validator_accepts_supported_engines(tmp_path: Path) -> None:
    """Only MV, MZ, and 2000/2003 trees pass the launch-time validator."""
    assert evb_module.default_tree_validator(_mv_tree(tmp_path / "mv"))
    mz = tmp_path / "mz"
    (mz / "js").mkdir(parents=True)
    (mz / "index.html").write_text("fixture", encoding="utf-8")
    (mz / "js" / "plugins.js").write_text("fixture", encoding="utf-8")
    (mz / "js" / "rmmz_core.js").write_text("fixture", encoding="utf-8")
    (mz / "package.json").write_text('{"name": "fixture"}', encoding="utf-8")
    (mz / "data").mkdir()
    assert evb_module.default_tree_validator(mz)
    assert evb_module.default_tree_validator(_rpg_rt_tree(tmp_path / "rt"))


def test_default_validator_rejects_unknown_trees(tmp_path: Path) -> None:
    """Fixture trees without engine markers fail launch-time validation."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "README.txt").write_text("Reading from EVB!", encoding="utf-8")
    assert not evb_module.default_tree_validator(other)
    assert not evb_module.default_tree_validator(tmp_path / "absent")


# --- Profile-backed unpacking ---


def _stub_unpack(monkeypatch: pytest.MonkeyPatch, calls: list[Path], tree: Path) -> None:
    """Replace extraction with a fake tree publication, recording calls."""

    def fake_unpack(
        source: Path,
        destination: Path,
        *,
        validator: evb_module.TreeValidator | None = None,
        profiles: tuple[tuple[str, str], ...] = evb_unpack.PROFILES,
        progress: evb_unpack.EvbProgressCallback | None = None,
        extract_tree: bool = True,
        restore_executable: bool = True,
        out_pe: Path | None = None,
    ) -> UnpackReport:
        calls.append(source)
        destination.mkdir(parents=True, exist_ok=True)
        for entry in tree.iterdir():
            target = destination / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target, dirs_exist_ok=True)
            else:
                target.write_bytes(entry.read_bytes())
        if validator is not None and not validator(destination):
            raise RuntimeError(f"no valid file tree in {source} after trying stub")
        return UnpackReport(
            profile="10_70",
            grouping="modern",
            files=1,
            unpacked_bytes=1,
            restored_executable=None,
            warnings=(),
            notes=(),
        )

    monkeypatch.setattr(evb_unpack, "unpack_packed_executable", fake_unpack)


def test_ensure_unpacked_publishes_into_source_keyed_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First unpack stages and publishes; the second call is a cache hit."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    first = evb_module.ensure_unpacked(paths, executable)
    assert first == _profile_game(paths, executable)
    assert first.parent.name == game_id(executable.parent)
    assert (first / "package.json").is_file()
    assert _marker_for(first).is_file()
    assert len(calls) == 1
    second = evb_module.ensure_unpacked(paths, executable)
    assert second == first
    assert len(calls) == 1


def test_distinct_sources_use_distinct_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Profile keying follows the source directory, never the content."""
    paths = _paths(tmp_path)
    first_exe = _source(tmp_path / "first")
    second_exe = _source(tmp_path / "second")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    first = evb_module.ensure_unpacked(paths, first_exe)
    second = evb_module.ensure_unpacked(paths, second_exe)
    assert first != second
    assert first.parent.name == game_id(first_exe.parent)
    assert second.parent.name == game_id(second_exe.parent)
    assert len(calls) == 2


def test_ensure_unpacked_rejects_unsupported_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engine validation gates the profile: unknown trees never publish."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    other = tmp_path / "other"
    other.mkdir()
    (other / "README.txt").write_text("Reading from EVB!", encoding="utf-8")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, other)
    with pytest.raises(RuntimeError, match="no valid file tree"):
        evb_module.ensure_unpacked(paths, executable)
    assert len(calls) == 1
    assert not _profile_game(paths, executable).exists()
    assert not _marker_for(_profile_game(paths, executable)).exists()


def test_ensure_unpacked_reunpacks_stale_source_preserving_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replaced executable re-unpacks over the same tree; saves survive."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    tree = _mv_tree(tmp_path / "tree")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, tree)
    first = evb_module.ensure_unpacked(paths, executable)
    saves = first / "www" / "save"
    saves.mkdir(parents=True)
    (saves / "Save01.lsd").write_bytes(b"player progress")
    (tree / "new.txt").write_text("replacement content", encoding="utf-8")
    executable.write_bytes(b"changed packed executable!")
    second = evb_module.ensure_unpacked(paths, executable)
    assert second == first
    assert len(calls) == 2
    assert (second / "new.txt").read_text(encoding="utf-8") == "replacement content"
    assert (second / "www" / "save" / "Save01.lsd").read_bytes() == b"player progress"


def test_ensure_unpacked_skips_symlinked_saves_on_reunpack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save carry-forward copies real saves but never symlinks."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    first = evb_module.ensure_unpacked(paths, executable)
    saves = first / "www" / "save"
    saves.mkdir(parents=True)
    (saves / "Save01.lsd").write_bytes(b"player progress")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"host data")
    os.symlink(outside, saves / "sneaky")
    executable.write_bytes(b"changed packed executable!")
    second = evb_module.ensure_unpacked(paths, executable)
    assert (second / "www" / "save" / "Save01.lsd").read_bytes() == b"player progress"
    assert not os.path.lexists(second / "www" / "save" / "sneaky")
    assert outside.read_bytes() == b"host data"


def test_ensure_unpacked_reunpacks_missing_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A matching marker without a tree still unpacks again."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    first = evb_module.ensure_unpacked(paths, executable)
    shutil.rmtree(first)
    second = evb_module.ensure_unpacked(paths, executable)
    assert second == first
    assert (second / "package.json").is_file()
    assert len(calls) == 2


def test_ensure_unpacked_reunpacks_corrupt_marker_preserving_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable marker is staleness, not a failure; saves survive."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    first = evb_module.ensure_unpacked(paths, executable)
    saves = first / "www" / "save"
    saves.mkdir(parents=True)
    (saves / "Save01.lsd").write_bytes(b"player progress")
    _marker_for(first).write_bytes(b"{not json")
    second = evb_module.ensure_unpacked(paths, executable)
    assert second == first
    assert len(calls) == 2
    assert (second / "www" / "save" / "Save01.lsd").read_bytes() == b"player progress"


def test_ensure_unpacked_cleans_failed_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed extractions leave no staging litter beside the game tree."""

    def failing_unpack(source: Path, destination: Path, **kwargs: object) -> UnpackReport:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("cannot decode data.bin")

    monkeypatch.setattr(evb_unpack, "unpack_packed_executable", failing_unpack)
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    with pytest.raises(RuntimeError, match="cannot decode"):
        evb_module.ensure_unpacked(paths, executable)
    profile = paths.profiles_root / game_id(executable.parent)
    leftovers = [entry for entry in profile.iterdir() if entry.name.startswith(".game-unpack-")]
    assert leftovers == []
    assert not _profile_game(paths, executable).exists()
    assert not _marker_for(_profile_game(paths, executable)).exists()


def test_failed_reunpack_preserves_previous_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing re-unpack keeps the old package and saves with no backup litter."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    first = evb_module.ensure_unpacked(paths, executable)
    saves = first / "www" / "save"
    saves.mkdir(parents=True)
    (saves / "Save01.lsd").write_bytes(b"player progress")
    old_package = (first / "package.json").read_bytes()
    old_marker = _marker_for(first).read_bytes()
    executable.write_bytes(b"changed packed executable for failed republish!")
    assert len(old_package) > 0

    def failing_unpack(source: Path, destination: Path, **kwargs: object) -> UnpackReport:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("cannot decode data.bin")

    monkeypatch.setattr(evb_unpack, "unpack_packed_executable", failing_unpack)
    with pytest.raises(RuntimeError, match="cannot decode"):
        evb_module.ensure_unpacked(paths, executable)
    assert (first / "package.json").read_bytes() == old_package
    assert (first / "www" / "save" / "Save01.lsd").read_bytes() == b"player progress"
    assert _marker_for(first).read_bytes() == old_marker
    assert not (first / "partial.txt").exists()
    profile = first.parent
    leftovers = [
        entry
        for entry in profile.iterdir()
        if entry.name.startswith(".game-unpack-") or entry.name == ".game-backup"
    ]
    assert leftovers == []
    assert len(calls) == 1


def test_digest_swap_same_size_mtime_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-size and same-mtime content swap never publishes under the old digest."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    first = evb_module.ensure_unpacked(paths, executable)
    saves = first / "www" / "save"
    saves.mkdir(parents=True)
    (saves / "Save01.lsd").write_bytes(b"player progress")
    old_package = (first / "package.json").read_bytes()
    old_marker = _marker_for(first).read_bytes()
    executable.write_bytes(b"second revision bytes for toctou test!!")
    assert len(calls) == 1

    def swapping_unpack(source: Path, destination: Path, **kwargs: object) -> UnpackReport:
        entry = os.stat(source)
        size = entry.st_size
        atime_ns = entry.st_atime_ns
        mtime_ns = entry.st_mtime_ns
        source.write_bytes(b"X" * size)
        os.utime(source, ns=(atime_ns, mtime_ns))
        destination.mkdir(parents=True, exist_ok=True)
        tree = _mv_tree(tmp_path / "swapped-tree")
        for child in tree.iterdir():
            target = destination / child.name
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True)
            else:
                target.write_bytes(child.read_bytes())
        (destination / "new.txt").write_text("swapped content", encoding="utf-8")
        return UnpackReport(
            profile="10_70",
            grouping="modern",
            files=1,
            unpacked_bytes=1,
            restored_executable=None,
            warnings=(),
            notes=(),
        )

    monkeypatch.setattr(evb_unpack, "unpack_packed_executable", swapping_unpack)
    with pytest.raises(RuntimeError, match="packed source changed"):
        evb_module.ensure_unpacked(paths, executable)
    assert (first / "package.json").read_bytes() == old_package
    assert (first / "www" / "save" / "Save01.lsd").read_bytes() == b"player progress"
    assert not (first / "new.txt").exists()
    assert _marker_for(first).read_bytes() == old_marker


def test_reunpack_skips_fifo_saves_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planted FIFO inside saves never blocks carry; regular saves survive."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    first = evb_module.ensure_unpacked(paths, executable)
    saves = first / "www" / "save"
    saves.mkdir(parents=True)
    (saves / "Save01.lsd").write_bytes(b"player progress")
    os.mkfifo(saves / "fifo")
    executable.write_bytes(b"changed packed executable for fifo test!!")
    second = evb_module.ensure_unpacked(paths, executable)
    assert second == first
    assert len(calls) == 2
    assert (second / "www" / "save" / "Save01.lsd").read_bytes() == b"player progress"
    assert not os.path.lexists(second / "www" / "save" / "fifo")


def test_ensure_unpacked_rejects_nonregular_source(tmp_path: Path) -> None:
    """Directories and missing paths never enter the unpacking flow."""
    paths = _paths(tmp_path)
    game = tmp_path / "game"
    game.mkdir()
    with pytest.raises(RuntimeError, match="not a regular file"):
        evb_module.ensure_unpacked(paths, game)
    with pytest.raises(RuntimeError, match="cannot read packed executable"):
        evb_module.ensure_unpacked(paths, game / "absent.exe")


def test_ensure_unpacked_reports_busy_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked profile game entry fails fast instead of unpacking twice."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    first = evb_module.ensure_unpacked(paths, executable)
    profile_descriptor = paths.open_or_create_private_cache_directory("profiles", first.parent.name)
    entered = threading.Event()
    release = threading.Event()
    try:

        def hold_lock() -> None:
            with cache_lock(profile_descriptor, "game"):
                entered.set()
                assert release.wait(timeout=30)

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(hold_lock)
            assert entered.wait(timeout=30)
            try:
                with pytest.raises(RuntimeError, match="busy"):
                    evb_module.ensure_unpacked(paths, executable)
            finally:
                release.set()
                pending.result(timeout=30)
    finally:
        os.close(profile_descriptor)
    assert len(calls) == 1


def test_ensure_unpacked_end_to_end_with_fixture(tmp_path: Path) -> None:
    """A real packed executable flows through hashing, staging, and publish."""
    fixtures = Path(__file__).resolve().parent / "fixtures" / "evb"
    assert fixtures.is_dir(), f"vendored fixtures missing: {fixtures}"
    paths = _paths(tmp_path)
    game = tmp_path / "game"
    game.mkdir()
    packed = fixtures / "x86_PackerTestApp_packed_20210329.exe"
    executable = game / "Custom Game Name.exe"
    executable.write_bytes(packed.read_bytes())
    assert evb_module.find_packed_executable(game) == executable
    entry = evb_module.ensure_unpacked(
        paths, executable, validator=lambda tree: (tree / "README.txt").is_file()
    )
    assert entry == _profile_game(paths, executable)
    assert (entry / "README.txt").read_bytes() == b"Reading from EVB!"
    assert (entry / "Custom Game Name.exe").is_file()


def test_profile_game_path_ensured_and_managed(tmp_path: Path) -> None:
    """The profile game tree is a validated direct game/ child."""
    paths = _paths(tmp_path)
    paths.ensure()
    profile = paths.profiles_root / "0123456789abcdef"
    entry = paths.ensure_managed_profile_game_path(profile / "game")
    assert entry.parent == paths.profiles_root.resolve(strict=True) / "0123456789abcdef"
    assert entry.name == "game"
    with pytest.raises(ConfigurationError):
        paths.ensure_managed_profile_game_path(profile / "game" / "nested")
    with pytest.raises(ConfigurationError):
        paths.ensure_managed_profile_game_path(profile / "sandbox")
    with pytest.raises(ConfigurationError):
        paths.ensure_managed_profile_game_path(profile / "Game")
    with pytest.raises(ConfigurationError):
        paths.ensure_managed_profile_game_path(tmp_path / "0123456789abcdef" / "game")


def test_ensure_unpacked_checks_source_on_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache hits still verify the source is unchanged."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    first = evb_module.ensure_unpacked(paths, executable)
    original_check = evb_module._check_source_unchanged
    seen: list[tuple[Path, int, int]] = []

    def recording_check(source_exe: Path, size: int, modified: int) -> None:
        seen.append((source_exe, size, modified))
        original_check(source_exe, size, modified)

    monkeypatch.setattr(evb_module, "_check_source_unchanged", recording_check)
    second = evb_module.ensure_unpacked(paths, executable)
    assert second == first
    assert seen, "cache hit skipped source validation"


def test_identify_source_uses_nonblocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hashing opens avoid blocking on pipes or FIFOs."""
    executable = _source(tmp_path / "game")
    seen: list[int] = []
    real_open = os.open

    def recording_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(str(path)).name == "Game.exe" and (flags & os.O_ACCMODE) == os.O_RDONLY:
            seen.append(flags)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", recording_open)
    evb_module._identify_source(executable)
    assert seen, "source open was not observed"
    assert all(flags & os.O_NONBLOCK for flags in seen)


# --- Legacy migration ---


def _legacy_entry(paths: AppPaths, executable: Path) -> Path:
    """Return the previous-release cache entry name for one packed source."""
    digest, size, modified = evb_module._identify_source(executable)
    return paths.cache_root / "evb" / f"{digest}-{size}-{modified}"


def _write_legacy_marker(legacy: Path, executable: Path) -> None:
    """Record the current source fingerprint inside one legacy entry."""
    digest, size, modified = evb_module._identify_source(executable)
    payload = {"digest": digest, "size": size, "mtime_ns": modified}
    (legacy / ".evb.json").write_text(json.dumps(payload), encoding="utf-8")


def test_legacy_entry_migrates_with_saves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A matching previous-release entry is adopted; saves travel inside."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    legacy = _legacy_entry(paths, executable)
    _mv_tree(legacy)
    (legacy / "save").mkdir(parents=True)
    (legacy / "save" / "Save01.lsd").write_bytes(b"player progress")
    _write_legacy_marker(legacy, executable)
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    game_path = evb_module.ensure_unpacked(paths, executable)
    assert calls == [], "migration must not unpack again"
    assert game_path == _profile_game(paths, executable)
    assert (game_path / "package.json").is_file()
    assert (game_path / "save" / "Save01.lsd").read_bytes() == b"player progress"
    marker = json.loads(_marker_for(game_path).read_text(encoding="utf-8"))
    digest, size, modified = evb_module._identify_source(executable)
    assert marker == {"digest": digest, "size": size, "mtime_ns": modified}
    assert not legacy.exists()
    assert not (paths.cache_root / "evb").exists()
    again = evb_module.ensure_unpacked(paths, executable)
    assert again == game_path
    assert calls == []


def test_legacy_invalid_tree_is_rejected_for_fresh_unpack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A matching marker with an invalid tree falls through to fresh unpack."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    legacy = _legacy_entry(paths, executable)
    (legacy / "save").mkdir(parents=True)
    (legacy / "save" / "Save01.lsd").write_bytes(b"player progress")
    (legacy / "package.json").write_text('{"name": "legacy"}', encoding="utf-8")
    _write_legacy_marker(legacy, executable)
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    game_path = evb_module.ensure_unpacked(paths, executable)
    assert len(calls) == 1
    assert game_path == _profile_game(paths, executable)
    assert (game_path / "package.json").read_text(encoding="utf-8") != '{"name": "legacy"}'
    assert (game_path / "www" / "index.html").is_file()
    assert legacy.is_dir()


def test_legacy_name_only_match_without_marker_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pure directory-name match without a marker never migrates."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    legacy = _legacy_entry(paths, executable)
    _mv_tree(legacy)
    (legacy / "save").mkdir(parents=True)
    (legacy / "save" / "Save01.lsd").write_bytes(b"player progress")
    assert not (legacy / ".evb.json").exists()
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    game_path = evb_module.ensure_unpacked(paths, executable)
    assert len(calls) == 1
    assert game_path == _profile_game(paths, executable)
    assert legacy.is_dir()


def test_legacy_race_missing_entry_falls_through_to_fresh_unpack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost race for the same legacy entry unpacks fresh instead of failing."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    legacy = _legacy_entry(paths, executable)
    _mv_tree(legacy)
    _write_legacy_marker(legacy, executable)
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    real_rename = os.rename

    def _racing_rename(source: object, destination: object, **kwargs: object) -> None:
        if str(source) == str(legacy):
            raise FileNotFoundError(str(source))
        real_rename(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", _racing_rename)
    game_path = evb_module.ensure_unpacked(paths, executable)
    assert len(calls) == 1
    assert game_path == _profile_game(paths, executable)
    assert (game_path / "www" / "index.html").is_file()


def test_legacy_unrelated_entries_are_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entries from other sources never migrate; they stay for their own game."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    other = paths.cache_root / "evb" / ("0" * 64 + "-1-2")
    other.mkdir(parents=True)
    (other / "package.json").write_text("{}", encoding="utf-8")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    game_path = evb_module.ensure_unpacked(paths, executable)
    assert len(calls) == 1
    assert (game_path / "package.json").is_file()
    assert other.is_dir()
    assert (paths.cache_root / "evb").is_dir()


def test_profile_removal_deletes_unpacked_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No unpack category is needed: removing the profile removes the tree."""
    paths = _paths(tmp_path)
    executable = _source(tmp_path / "game")
    calls: list[Path] = []
    _stub_unpack(monkeypatch, calls, _mv_tree(tmp_path / "tree"))
    game_path = evb_module.ensure_unpacked(paths, executable)
    assert game_path.is_dir()
    ProfileCatalog(paths).remove(game_path.parent)
    assert not game_path.parent.exists()
    assert not game_path.exists()
