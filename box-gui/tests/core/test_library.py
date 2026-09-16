"""LibraryRepository unit tests without any GTK dependency."""

# pyright: reportMissingImports=false

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from box.paths import AppPaths

from box_gui.core.library import (
    GHOST_THRESHOLD,
    LibraryEntry,
    LibraryError,
    LibraryRepository,
    is_ghost,
)


def _make_repository(tmp_path: Path) -> LibraryRepository:
    """Build a repository rooted in an isolated temporary directory."""
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    return LibraryRepository(paths)


def test_missing_library_returns_empty_tuple(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)

    assert repository.load() == ()


def test_round_trip_preserves_all_fields(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    first = repository.add(tmp_path / "alpha", "Alpha")
    second = repository.add(tmp_path / "beta", "Beta")
    edited = LibraryEntry(
        path=second.path,
        display_name="Beta Renamed",
        order=second.order,
        preferred_runtime="0.91.0",
        preferred_sdk=True,
        copy_root_files=("package.json", "icon.png"),
        engine="rpg-maker-mv",
    )
    repository.update(edited)

    loaded = repository.load()

    assert [entry.path for entry in loaded] == [first.path, second.path]
    assert loaded[0].display_name == "Alpha"
    assert loaded[0].preferred_runtime is None
    assert loaded[0].preferred_sdk is False
    assert loaded[0].copy_root_files == ()
    assert loaded[0].engine is None
    assert loaded[1].display_name == "Beta Renamed"
    assert loaded[1].preferred_runtime == "0.91.0"
    assert loaded[1].preferred_sdk is True
    assert loaded[1].copy_root_files == ("package.json", "icon.png")
    assert loaded[1].engine == "rpg-maker-mv"
    assert [entry.order for entry in loaded] == sorted(entry.order for entry in loaded)


def test_corrupt_json_raises_library_error(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    repository.library_file.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(LibraryError):
        repository.load()


def test_corrupt_schema_raises_library_error(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    repository.library_file.write_text('{"version": 999, "entries": []}', encoding="utf-8")

    with pytest.raises(LibraryError):
        repository.load()

    assert issubclass(LibraryError, ValueError)


def test_add_assigns_sequential_orders(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)

    first = repository.add(Path("/games/one"), "One")
    second = repository.add(Path("/games/two"), "Two")

    assert (first.order, second.order) == (0, 1)
    assert [entry.order for entry in repository.load()] == [0, 1]


def test_reorder_semantics(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    first = repository.add(Path("/games/a"), "A")
    second = repository.add(Path("/games/b"), "B")
    third = repository.add(Path("/games/c"), "C")

    moved_up = repository.reorder(second, "up")
    assert [entry.path for entry in moved_up] == [second.path, first.path, third.path]

    moved_down = repository.reorder(moved_up[0], "down")
    assert [entry.path for entry in moved_down] == [first.path, second.path, third.path]

    moved_top = repository.reorder(third, "top")
    assert [entry.path for entry in moved_top] == [third.path, first.path, second.path]

    moved_bottom = repository.reorder(moved_top[0], "bottom")
    assert [entry.path for entry in moved_bottom] == [first.path, second.path, third.path]

    top_edge = repository.reorder(first, "up")
    assert [entry.path for entry in top_edge] == [first.path, second.path, third.path]

    bottom_edge = repository.reorder(third, "down")
    assert [entry.path for entry in bottom_edge] == [first.path, second.path, third.path]

    assert [entry.order for entry in repository.load()] == [0, 1, 2]


def test_remove_drops_only_matching_path(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    first = repository.add(Path("/games/a"), "A")
    second = repository.add(Path("/games/b"), "B")

    repository.remove(first)

    assert [entry.path for entry in repository.load()] == [second.path]


def test_update_persists_edits_without_reordering(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    first = repository.add(Path("/games/a"), "A")
    second = repository.add(Path("/games/b"), "B")
    edited = LibraryEntry(
        path=second.path,
        display_name="B Edited",
        order=999,
        preferred_runtime="0.90.0",
        preferred_sdk=True,
        copy_root_files=("save.dat",),
        engine="rpg-maker-mz",
    )

    updated = repository.update(edited)
    loaded = repository.load()

    assert updated.order == second.order
    assert updated.display_name == "B Edited"
    assert updated.engine == "rpg-maker-mz"
    assert loaded[0].path == first.path
    assert loaded[1].display_name == "B Edited"
    assert loaded[1].preferred_runtime == "0.90.0"
    assert loaded[1].preferred_sdk is True
    assert loaded[1].copy_root_files == ("save.dat",)
    assert loaded[1].engine == "rpg-maker-mz"


def test_atomic_write_leaves_no_partial_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repository(tmp_path)
    repository.add(Path("/games/a"), "A")
    before = repository.library_file.read_text(encoding="utf-8")

    def _fail_replace(_source: object, _target: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _fail_replace)
    with pytest.raises(OSError):
        repository.save(repository.load())

    assert repository.library_file.read_text(encoding="utf-8") == before
    leftovers = [
        child
        for child in repository.library_file.parent.iterdir()
        if child.name.startswith(".library-")
    ]
    assert leftovers == []


def test_save_writes_versioned_schema(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.add(Path("/games/a"), "A")

    payload = json.loads(repository.library_file.read_text(encoding="utf-8"))

    assert payload["version"] == 5
    assert isinstance(payload["entries"], list)
    assert payload["entries"][0]["path"] == "/games/a"
    assert payload["entries"][0]["engine"] is None
    assert payload["entries"][0]["allow_network"] is False
    assert payload["entries"][0]["allow_game_writes"] is False
    assert payload["entries"][0]["allow_x11"] is False
    assert payload["entries"][0]["use_gamemode"] is False
    assert payload["entries"][0]["missing_streak"] == 0


def test_migrates_v1_without_engine_to_none(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "version": 1,
        "entries": [
            {
                "path": "/games/legacy",
                "display_name": "Legacy",
                "order": 0,
                "preferred_runtime": None,
                "preferred_sdk": False,
                "copy_root_files": [],
            }
        ],
    }
    repository.library_file.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = repository.load()

    assert len(loaded) == 1
    assert loaded[0].engine is None

    repository.save(loaded)
    migrated = json.loads(repository.library_file.read_text(encoding="utf-8"))

    assert migrated["version"] == 5
    assert migrated["entries"][0]["engine"] is None
    assert migrated["entries"][0]["allow_network"] is False
    assert migrated["entries"][0]["allow_game_writes"] is False
    assert migrated["entries"][0]["allow_x11"] is False
    assert migrated["entries"][0]["use_gamemode"] is False


def test_loads_v2_with_engine_string(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "entries": [
            {
                "path": "/games/mv",
                "display_name": "MV Game",
                "order": 0,
                "preferred_runtime": None,
                "preferred_sdk": False,
                "copy_root_files": [],
                "engine": "rpg-maker-mv",
            }
        ],
    }
    repository.library_file.write_text(json.dumps(payload), encoding="utf-8")

    loaded = repository.load()

    assert loaded[0].engine == "rpg-maker-mv"


def test_decode_normalizes_empty_engine_to_none(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "entries": [
            {
                "path": "/games/empty",
                "display_name": "Empty",
                "order": 0,
                "preferred_runtime": None,
                "preferred_sdk": False,
                "copy_root_files": [],
                "engine": "",
            }
        ],
    }
    repository.library_file.write_text(json.dumps(payload), encoding="utf-8")

    loaded = repository.load()

    assert loaded[0].engine is None


def test_decode_rejects_bad_engine(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "entries": [
            {
                "path": "/games/bad",
                "display_name": "Bad",
                "order": 0,
                "preferred_runtime": None,
                "preferred_sdk": False,
                "copy_root_files": [],
                "engine": 42,
            }
        ],
    }
    repository.library_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LibraryError):
        repository.load()


def test_add_accepts_engine(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)

    created = repository.add(Path("/games/mz"), "MZ Game", "rpg-maker-mz")

    assert created.engine == "rpg-maker-mz"
    assert repository.load()[0].engine == "rpg-maker-mz"
    payload = json.loads(repository.library_file.read_text(encoding="utf-8"))
    assert payload["entries"][0]["engine"] == "rpg-maker-mz"


def test_update_backfills_engine_only_when_incoming_not_none(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    created = repository.add(Path("/games/a"), "A")

    backfilled = repository.update(
        LibraryEntry(
            path=created.path,
            display_name=created.display_name,
            order=created.order,
            preferred_runtime=created.preferred_runtime,
            preferred_sdk=created.preferred_sdk,
            copy_root_files=created.copy_root_files,
            engine="rpg-maker-2000-2003",
        )
    )
    assert backfilled.engine == "rpg-maker-2000-2003"

    incoming_none = LibraryEntry(
        path=created.path,
        display_name="A Renamed",
        order=999,
        preferred_runtime=None,
        preferred_sdk=False,
        copy_root_files=(),
        engine=None,
    )
    kept = repository.update(incoming_none)

    assert kept.display_name == "A Renamed"
    assert kept.engine == "rpg-maker-2000-2003"
    assert repository.load()[0].engine == "rpg-maker-2000-2003"


def test_reorder_preserves_engine(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.add(Path("/games/a"), "A", "rpg-maker-mv")
    second = repository.add(Path("/games/b"), "B", "rpg-maker-mz")

    reordered = repository.reorder(second, "up")

    assert [entry.engine for entry in reordered] == ["rpg-maker-mz", "rpg-maker-mv"]
    assert [entry.engine for entry in repository.load()] == ["rpg-maker-mz", "rpg-maker-mv"]


def test_add_accepts_preferred_runtime(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)

    created = repository.add(
        Path("/games/easy"),
        "Easy",
        "rpg-maker-2000-2003",
        preferred_runtime="0.8.1",
    )

    assert created.engine == "rpg-maker-2000-2003"
    assert created.preferred_runtime == "0.8.1"
    assert repository.load()[0].preferred_runtime == "0.8.1"


def test_add_defaults_preferred_runtime_to_none(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)

    created = repository.add(Path("/games/plain"), "Plain", "rpg-maker-mv")

    assert created.preferred_runtime is None
    assert repository.load()[0].preferred_runtime is None


def test_add_accepts_preferred_runtime_positionally(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)

    created = repository.add(Path("/games/pos"), "Pos", "rpg-maker-mv", "1.2.3")

    assert created.engine == "rpg-maker-mv"
    assert created.preferred_runtime == "1.2.3"
    assert repository.load()[0].preferred_runtime == "1.2.3"


def test_migrates_v2_without_permissions_to_false(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "version": 2,
        "entries": [
            {
                "path": "/games/legacy",
                "display_name": "Legacy",
                "order": 0,
                "preferred_runtime": None,
                "preferred_sdk": False,
                "copy_root_files": [],
                "engine": "rpg-maker-mv",
            }
        ],
    }
    repository.library_file.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = repository.load()

    assert len(loaded) == 1
    assert loaded[0].allow_network is False
    assert loaded[0].allow_game_writes is False
    assert loaded[0].allow_x11 is False

    repository.save(loaded)
    migrated = json.loads(repository.library_file.read_text(encoding="utf-8"))

    assert migrated["version"] == 5
    assert migrated["entries"][0]["allow_network"] is False
    assert migrated["entries"][0]["allow_game_writes"] is False
    assert migrated["entries"][0]["allow_x11"] is False
    assert migrated["entries"][0]["use_gamemode"] is False


def test_decode_rejects_bad_permissions(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 3,
        "entries": [
            {
                "path": "/games/bad",
                "display_name": "Bad",
                "order": 0,
                "preferred_runtime": None,
                "preferred_sdk": False,
                "copy_root_files": [],
                "engine": None,
                "allow_network": 1,
                "allow_game_writes": False,
                "allow_x11": False,
            }
        ],
    }
    repository.library_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LibraryError):
        repository.load()


def test_update_merges_permissions(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    created = repository.add(Path("/games/a"), "A")

    assert created.allow_network is False

    updated = repository.update(
        LibraryEntry(
            path=created.path,
            display_name=created.display_name,
            order=created.order,
            preferred_runtime=created.preferred_runtime,
            preferred_sdk=created.preferred_sdk,
            copy_root_files=created.copy_root_files,
            engine=created.engine,
            allow_network=True,
            allow_game_writes=True,
            allow_x11=True,
        )
    )

    assert updated.allow_network is True
    assert updated.allow_game_writes is True
    assert updated.allow_x11 is True
    stored = repository.load()[0]
    assert stored.allow_network is True
    assert stored.allow_game_writes is True
    assert stored.allow_x11 is True


def test_icon_path_round_trip(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    created = repository.add(Path("/games/a"), "A")
    icon = tmp_path / "icons" / "abc123.png"

    assert created.icon_path is None

    updated = repository.update(
        LibraryEntry(
            path=created.path,
            display_name=created.display_name,
            order=created.order,
            preferred_runtime=created.preferred_runtime,
            preferred_sdk=created.preferred_sdk,
            copy_root_files=created.copy_root_files,
            engine=created.engine,
            icon_path=icon,
        )
    )

    assert updated.icon_path == icon
    loaded = repository.load()
    assert loaded[0].icon_path == icon
    payload = json.loads(repository.library_file.read_text(encoding="utf-8"))
    assert payload["entries"][0]["icon_path"] == str(icon)


def test_icon_path_clears_to_none(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    created = repository.add(Path("/games/a"), "A")
    repository.update(
        LibraryEntry(
            path=created.path,
            display_name=created.display_name,
            order=created.order,
            preferred_runtime=created.preferred_runtime,
            preferred_sdk=created.preferred_sdk,
            copy_root_files=created.copy_root_files,
            engine=created.engine,
            icon_path=tmp_path / "icons" / "abc123.png",
        )
    )
    cleared = repository.update(
        LibraryEntry(
            path=created.path,
            display_name=created.display_name,
            order=created.order,
            preferred_runtime=created.preferred_runtime,
            preferred_sdk=created.preferred_sdk,
            copy_root_files=created.copy_root_files,
            engine=created.engine,
            icon_path=None,
        )
    )

    assert cleared.icon_path is None
    assert repository.load()[0].icon_path is None


def test_reorder_preserves_icon_path(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.add(Path("/games/a"), "A")
    second = repository.add(Path("/games/b"), "B")
    repository.update(
        LibraryEntry(
            path=second.path,
            display_name=second.display_name,
            order=second.order,
            preferred_runtime=second.preferred_runtime,
            preferred_sdk=second.preferred_sdk,
            copy_root_files=second.copy_root_files,
            engine=second.engine,
            icon_path=tmp_path / "icons" / "abc123.png",
        )
    )

    reordered = repository.reorder(second, "up")

    assert reordered[0].icon_path == tmp_path / "icons" / "abc123.png"
    assert repository.load()[0].icon_path == tmp_path / "icons" / "abc123.png"


def test_loads_older_file_without_icon_path_as_none(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "version": 3,
        "entries": [
            {
                "path": "/games/legacy",
                "display_name": "Legacy",
                "order": 0,
                "preferred_runtime": None,
                "preferred_sdk": False,
                "copy_root_files": [],
                "engine": "rpg-maker-mv",
                "allow_network": False,
                "allow_game_writes": False,
                "allow_x11": False,
            }
        ],
    }
    repository.library_file.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = repository.load()

    assert len(loaded) == 1
    assert loaded[0].icon_path is None


def test_decode_rejects_bad_icon_path(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 3,
        "entries": [
            {
                "path": "/games/bad",
                "display_name": "Bad",
                "order": 0,
                "preferred_runtime": None,
                "preferred_sdk": False,
                "copy_root_files": [],
                "engine": None,
                "allow_network": False,
                "allow_game_writes": False,
                "allow_x11": False,
                "icon_path": 42,
            }
        ],
    }
    repository.library_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LibraryError):
        repository.load()


def _legacy_entry(path: str) -> dict[str, object]:
    """Build a version-3 style entry without the gamemode key."""
    return {
        "path": path,
        "display_name": Path(path).name,
        "order": 0,
        "preferred_runtime": None,
        "preferred_sdk": False,
        "copy_root_files": [],
        "engine": None,
        "allow_network": False,
        "allow_game_writes": False,
        "allow_x11": False,
    }


def test_old_payload_without_gamemode_defaults_to_false(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    entry = _legacy_entry("/games/legacy")
    repository.library_file.write_text(
        json.dumps({"version": 3, "entries": [entry]}), encoding="utf-8"
    )

    loaded = repository.load()

    assert len(loaded) == 1
    assert loaded[0].use_gamemode is False


def test_gamemode_round_trip_with_true(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    created = repository.add(tmp_path / "game", "Game")
    updated = repository.update(
        LibraryEntry(
            path=created.path,
            display_name=created.display_name,
            order=created.order,
            preferred_runtime=created.preferred_runtime,
            preferred_sdk=created.preferred_sdk,
            copy_root_files=created.copy_root_files,
            engine=created.engine,
            allow_network=created.allow_network,
            allow_game_writes=created.allow_game_writes,
            allow_x11=created.allow_x11,
            use_gamemode=True,
            icon_path=created.icon_path,
        )
    )

    assert updated.use_gamemode is True

    loaded = repository.load()
    assert loaded[0].use_gamemode is True
    payload = json.loads(repository.library_file.read_text(encoding="utf-8"))
    assert payload["entries"][0]["use_gamemode"] is True


def test_version_4_accepted_and_99_rejected(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    entry = _legacy_entry("/games/modern")
    entry["use_gamemode"] = True
    repository.library_file.write_text(
        json.dumps({"version": 4, "entries": [entry]}), encoding="utf-8"
    )

    loaded = repository.load()
    assert len(loaded) == 1
    assert loaded[0].use_gamemode is True

    repository.library_file.write_text(json.dumps({"version": 99, "entries": []}), encoding="utf-8")
    with pytest.raises(LibraryError):
        repository.load()


def test_decode_rejects_bad_gamemode(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    entry = _legacy_entry("/games/bad")
    entry["use_gamemode"] = 1
    repository.library_file.write_text(
        json.dumps({"version": 4, "entries": [entry]}), encoding="utf-8"
    )

    with pytest.raises(LibraryError):
        repository.load()


def test_add_reorder_update_preserve_gamemode(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    first = repository.add(tmp_path / "one", "One")
    assert first.use_gamemode is False

    enabled = repository.update(
        LibraryEntry(
            path=first.path,
            display_name=first.display_name,
            order=first.order,
            preferred_runtime=first.preferred_runtime,
            preferred_sdk=first.preferred_sdk,
            copy_root_files=first.copy_root_files,
            engine=first.engine,
            allow_network=first.allow_network,
            allow_game_writes=first.allow_game_writes,
            allow_x11=first.allow_x11,
            use_gamemode=True,
            icon_path=first.icon_path,
        )
    )
    assert enabled.use_gamemode is True

    second = repository.add(tmp_path / "two", "Two")
    reordered = repository.reorder(second, "up")

    assert [entry.use_gamemode for entry in reordered] == [False, True]
    assert [entry.use_gamemode for entry in repository.load()] == [False, True]

    renamed = repository.update(
        LibraryEntry(
            path=first.path,
            display_name="One Renamed",
            order=999,
            preferred_runtime=None,
            preferred_sdk=False,
            copy_root_files=(),
            engine=None,
            allow_network=False,
            allow_game_writes=False,
            allow_x11=False,
            use_gamemode=True,
            icon_path=None,
        )
    )
    assert renamed.use_gamemode is True
    assert renamed.display_name == "One Renamed"


def test_prune_missing_removes_only_missing_dirs(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    existing = tmp_path / "games" / "existing"
    existing.mkdir(parents=True)
    missing = tmp_path / "games" / "missing"
    regular_file = tmp_path / "games" / "file-game"
    regular_file.parent.mkdir(parents=True, exist_ok=True)
    regular_file.write_text("data", encoding="utf-8")
    dangling = tmp_path / "games" / "dangling"
    if dangling.exists() or dangling.is_symlink():
        dangling.unlink()
    dangling.symlink_to(tmp_path / "games" / "no-target", target_is_directory=True)
    repository.add(existing, "Existing")
    repository.add(missing, "Missing")
    repository.add(regular_file, "File")
    repository.add(dangling, "Dangling")

    removed = repository.prune_missing()

    assert {entry.path for entry in removed} == {missing, regular_file, dangling}
    assert [entry.path for entry in repository.load()] == [existing]
    assert existing.is_dir()
    assert regular_file.is_file()


def test_prune_missing_no_save_when_nothing_removed(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    existing = tmp_path / "games" / "existing"
    existing.mkdir(parents=True)
    repository.add(existing, "Existing")
    before = repository.library_file.read_text(encoding="utf-8")
    before_mtime = repository.library_file.stat().st_mtime_ns

    removed = repository.prune_missing()

    assert removed == ()
    assert repository.library_file.read_text(encoding="utf-8") == before
    assert repository.library_file.stat().st_mtime_ns == before_mtime
    assert [entry.path for entry in repository.load()] == [existing]


def test_prune_missing_other_oserror_raises_library_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repository(tmp_path)
    existing = tmp_path / "games" / "existing"
    existing.mkdir(parents=True)
    repository.add(existing, "Existing")

    original_resolve = Path.resolve

    def _boom(self: Path, *args: object, **kwargs: object) -> Path:
        raise PermissionError("simulated permission error")

    monkeypatch.setattr(Path, "resolve", _boom)
    try:
        with pytest.raises(LibraryError):
            repository.prune_missing()
    finally:
        monkeypatch.setattr(Path, "resolve", original_resolve)

    assert [entry.path for entry in repository.load()] == [existing]


def test_missing_streak_round_trip(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    created = repository.add(tmp_path / "game", "Game")

    assert created.missing_streak == 0

    streaked = repository.update(replace(created, missing_streak=2))

    assert streaked.missing_streak == 2
    loaded = repository.load()
    assert loaded[0].missing_streak == 2
    payload = json.loads(repository.library_file.read_text(encoding="utf-8"))
    assert payload["entries"][0]["missing_streak"] == 2


def test_old_payload_without_streak_defaults_to_zero(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    entry = _legacy_entry("/games/legacy")
    entry["use_gamemode"] = False
    repository.library_file.write_text(
        json.dumps({"version": 4, "entries": [entry]}), encoding="utf-8"
    )

    loaded = repository.load()

    assert len(loaded) == 1
    assert loaded[0].missing_streak == 0
    assert is_ghost(loaded[0]) is False


def test_version_5_accepted_and_99_rejected(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    entry = _legacy_entry("/games/modern")
    entry["use_gamemode"] = False
    entry["missing_streak"] = 1
    repository.library_file.write_text(
        json.dumps({"version": 5, "entries": [entry]}), encoding="utf-8"
    )

    loaded = repository.load()
    assert len(loaded) == 1
    assert loaded[0].missing_streak == 1

    repository.library_file.write_text(json.dumps({"version": 99, "entries": []}), encoding="utf-8")
    with pytest.raises(LibraryError):
        repository.load()


def test_decode_rejects_bad_missing_streak(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)

    for bad in (-1, True, False, "3", 1.5, None, [0]):
        entry = _legacy_entry("/games/bad")
        entry["use_gamemode"] = False
        entry["missing_streak"] = bad
        repository.library_file.write_text(
            json.dumps({"version": 5, "entries": [entry]}), encoding="utf-8"
        )
        with pytest.raises(LibraryError):
            repository.load()


def test_note_missing_state_increments_then_resets(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    present = tmp_path / "games" / "present"
    present.mkdir(parents=True)
    missing = tmp_path / "games" / "missing"
    repository.add(present, "Present")
    repository.add(missing, "Missing")

    first = repository.note_missing_presentation_state()
    by_name = {entry.display_name: entry for entry in first}

    assert by_name["Present"].missing_streak == 0
    assert by_name["Missing"].missing_streak == 1
    assert is_ghost(by_name["Missing"]) is False

    second = repository.note_missing_presentation_state()
    assert {entry.display_name: entry for entry in second}["Missing"].missing_streak == 2

    missing.mkdir(parents=True)
    third = repository.update_streak()
    by_name = {entry.display_name: entry for entry in third}

    assert by_name["Missing"].missing_streak == 0
    assert by_name["Present"].missing_streak == 0
    assert all(is_ghost(entry) is False for entry in third)


def test_ghost_threshold_marks_ghosts(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    missing = tmp_path / "games" / "missing"
    repository.add(missing, "Missing")

    for _ in range(GHOST_THRESHOLD - 1):
        repository.note_missing_presentation_state()
    below = repository.load()[0]

    assert below.missing_streak == GHOST_THRESHOLD - 1
    assert is_ghost(below) is False

    repository.note_missing_presentation_state()
    ghost = repository.load()[0]

    assert ghost.missing_streak == GHOST_THRESHOLD
    assert is_ghost(ghost) is True


def test_note_missing_state_skips_save_when_unchanged(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    existing = tmp_path / "games" / "existing"
    existing.mkdir(parents=True)
    repository.add(existing, "Existing")
    before = repository.library_file.read_text(encoding="utf-8")
    before_mtime = repository.library_file.stat().st_mtime_ns

    refreshed = repository.note_missing_presentation_state()

    assert [entry.display_name for entry in refreshed] == ["Existing"]
    assert repository.library_file.read_text(encoding="utf-8") == before
    assert repository.library_file.stat().st_mtime_ns == before_mtime


def test_add_reorder_update_preserve_missing_streak(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    first = repository.add(tmp_path / "one", "One")
    assert first.missing_streak == 0

    streaked = repository.update(replace(first, missing_streak=2))
    assert streaked.missing_streak == 2

    second = repository.add(tmp_path / "two", "Two")
    reordered = repository.reorder(second, "up")

    assert [entry.missing_streak for entry in reordered] == [0, 2]
    assert [entry.missing_streak for entry in repository.load()] == [0, 2]

    renamed = repository.update(replace(streaked, display_name="One Renamed", order=999))
    assert renamed.missing_streak == 2
    assert renamed.display_name == "One Renamed"


def test_update_relocates_by_order_and_clears_streak(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    old_root = tmp_path / "old"
    old_root.mkdir()
    new_root = tmp_path / "new"
    new_root.mkdir()
    created = repository.add(old_root, "Game", "rpg-maker-mv")
    streaked = repository.update(
        replace(created, preferred_runtime="0.83.0", use_gamemode=True, missing_streak=3)
    )
    assert is_ghost(streaked) is True

    relocated = repository.update(replace(streaked, path=new_root, missing_streak=0))
    loaded = repository.load()

    assert relocated.path == new_root
    assert relocated.missing_streak == 0
    assert relocated.order == streaked.order
    assert relocated.display_name == "Game"
    assert relocated.engine == "rpg-maker-mv"
    assert relocated.preferred_runtime == "0.83.0"
    assert relocated.use_gamemode is True
    assert is_ghost(loaded[0]) is False


def test_update_relocate_onto_taken_path_raises(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    first = repository.add(tmp_path / "one", "One")
    second = repository.add(tmp_path / "two", "Two")

    with pytest.raises(LibraryError):
        repository.update(replace(first, path=second.path, missing_streak=0))


def test_note_missing_entry_increments_and_reports_absence(tmp_path: Path) -> None:
    """One sighting for a single entry bumps its streak and reports missing."""
    repository = _make_repository(tmp_path)
    created = repository.add(tmp_path / "gone", "Gone")

    updated, present = repository.note_missing_entry(created)

    assert present is False
    assert updated.missing_streak == 1
    assert repository.load()[0].missing_streak == 1


def test_note_missing_entry_resets_on_success(tmp_path: Path) -> None:
    """A present folder clears the streak and returns the stored entry."""
    repository = _make_repository(tmp_path)
    root = tmp_path / "back"
    root.mkdir()
    created = repository.add(root, "Back")
    streaked = repository.update(replace(created, missing_streak=2))

    updated, present = repository.note_missing_entry(streaked)

    assert present is True
    assert updated.missing_streak == 0
    assert repository.load()[0].missing_streak == 0


def test_note_missing_entry_skips_save_when_unchanged(tmp_path: Path) -> None:
    """A present streak-free entry performs no write at all."""
    repository = _make_repository(tmp_path)
    root = tmp_path / "steady"
    root.mkdir()
    created = repository.add(root, "Steady")
    before = repository.library_file.read_text(encoding="utf-8")
    before_mtime = repository.library_file.stat().st_mtime_ns

    updated, present = repository.note_missing_entry(created)

    assert present is True
    assert updated == created
    assert repository.library_file.read_text(encoding="utf-8") == before
    assert repository.library_file.stat().st_mtime_ns == before_mtime


def test_note_missing_entry_ignores_unknown_entries(tmp_path: Path) -> None:
    """Entries absent from the file are returned untouched without saving."""
    repository = _make_repository(tmp_path)
    root = tmp_path / "kept"
    root.mkdir()
    kept = repository.add(root, "Kept")
    before = repository.library_file.read_text(encoding="utf-8")
    outsider = replace(kept, path=tmp_path / "elsewhere")

    updated, present = repository.note_missing_entry(outsider)

    assert updated == outsider
    assert present is False
    assert repository.load()[0] == kept
    assert repository.library_file.read_text(encoding="utf-8") == before
