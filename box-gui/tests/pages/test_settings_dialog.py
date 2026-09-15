"""Settings dialog tests driving stubbed backends without network or I/O."""

# pyright: reportMissingImports=false
# pyright: reportPrivateUsage=false

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Any

import pytest

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")

    import box.api.runtime as runtime_module
    from box.api import AppPaths, ConfigRepository
    from box.api.cleanup import CATEGORIES, CleanupItem
    from box.api.runtime import AvailableEasyRPGVersions, AvailableVersions
    from box.errors import RuntimeError as BoxRuntimeError
    from box.models import RuntimeInfo, RuntimeSpec
    from box.runtime.easyrpg import EasyRPGRuntime
    from gi.repository import Adw, GLib, Gtk

    import box_gui.pages.settings_dialog as settings_module
    from box_gui.core.display import abbreviate_display_path
    from box_gui.pages.settings_dialog import (
        CleanupPage,
        EasyrpgPage,
        GeneralPage,
        NwjsPage,
        SettingsDialog,
    )

    _settings_available = True
except Exception:
    runtime_module: Any = None
    AppPaths: Any = None
    ConfigRepository: Any = None
    CATEGORIES: Any = ()  # pyright: ignore[reportConstantRedefinition]
    CleanupItem: Any = None
    AvailableEasyRPGVersions: Any = None
    AvailableVersions: Any = None
    BoxRuntimeError: Any = Exception
    RuntimeInfo: Any = None
    RuntimeSpec: Any = None
    EasyRPGRuntime: Any = None
    Adw: Any = None
    GLib: Any = None
    Gtk: Any = None
    settings_module: Any = None
    abbreviate_display_path: Any = None
    CleanupPage: Any = None
    EasyrpgPage: Any = None
    GeneralPage: Any = None
    NwjsPage: Any = None
    SettingsDialog: Any = None
    _settings_available = False

pytestmark = pytest.mark.skipif(not _settings_available, reason="gi/Adw unavailable")


def _has_display() -> bool:
    """Return True when a Wayland or X11 display looks available."""
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _require_display() -> None:
    """Skip the test when no display is available for real widgets."""
    if not _has_display():
        pytest.skip("no display for settings dialog widgets")


def _pump(ms: int = 300) -> None:
    """Run the main loop briefly so idle_add worker outcomes land."""
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


def _install_auto_answer(
    monkeypatch: pytest.MonkeyPatch, response: str | None, *, close: bool = False
) -> None:
    """Patch AlertDialog.present to schedule a response without clicks."""

    def _fake_present(self: Any, parent: Any | None = None) -> None:
        if response is None and not close:
            return

        def _answer() -> bool:
            if close:
                self.emit("closed")
                return False
            self.emit("response", response)
            return False

        GLib.idle_add(_answer)

    monkeypatch.setattr(Adw.AlertDialog, "present", _fake_present)


def _make_paths(tmp_path: Path) -> Any:
    """Build isolated AppPaths under tmp_path."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environ = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    paths = AppPaths.from_environment(environ)
    paths.ensure()
    return paths


def _stub_runtime_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub default_architecture deterministically, allowing old box-rpg."""
    monkeypatch.setattr(runtime_module, "default_architecture", lambda: "x64", raising=False)


def _make_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    nwjs_versions: tuple[str, ...] = ("1.0.0", "0.9.0"),
    easyrpg_versions: tuple[str, ...] = ("0.8.1", "0.8.0"),
    nwjs_installed: tuple[Any, ...] = (),
    easyrpg_installed: tuple[Any, ...] = (),
    install_hook: Any | None = None,
) -> tuple[Any, Any, dict[str, Any]]:
    """Create NW.js/EasyRPG pages with a stubbed runtime API and drained loads."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _stub_runtime_api(monkeypatch)
    seen: dict[str, Any] = {
        "fetch_nwjs": [],
        "fetch_easyrpg": [],
        "install_nwjs": [],
        "install_easyrpg": [],
        "remove_nwjs": [],
        "remove_easyrpg": [],
    }

    def _fake_list_nwjs(catalog: Any) -> tuple[Any, ...]:
        return tuple(nwjs_installed)

    def _fake_list_easyrpg(catalog: Any) -> tuple[Any, ...]:
        return tuple(easyrpg_installed)

    def _fake_fetch_nwjs(page: int, architecture: str, sdk: bool) -> Any:
        seen["fetch_nwjs"].append((page, architecture, sdk))
        return AvailableVersions(page=page, versions=nwjs_versions)

    def _fake_fetch_easyrpg(page: int) -> Any:
        seen["fetch_easyrpg"].append(page)
        return AvailableEasyRPGVersions(page=page, versions=easyrpg_versions)

    def _fake_install_nwjs(
        paths: Any, version: str, architecture: str, sdk: bool = False, progress: Any = None
    ) -> Any:
        seen["install_nwjs"].append((version, architecture, sdk, progress))
        if install_hook is not None:
            install_hook(progress)
        return RuntimeInfo(
            RuntimeSpec(version=version, architecture=architecture, sdk=sdk),
            tmp_path / "rt",
            tmp_path / "rt" / "nw",
        )

    def _fake_install_easyrpg(paths: Any, version: str, progress: Any = None) -> Any:
        seen["install_easyrpg"].append((version, progress))
        if install_hook is not None:
            install_hook(progress)
        return EasyRPGRuntime(version, tmp_path / "easyrpg")

    def _fake_remove_nwjs(catalog: Any, version: str, architecture: str, sdk: bool = False) -> None:
        seen["remove_nwjs"].append((version, architecture, sdk))

    def _fake_remove_easyrpg(catalog: Any, version: str) -> None:
        seen["remove_easyrpg"].append(version)

    monkeypatch.setattr(runtime_module, "list_nwjs", _fake_list_nwjs)
    monkeypatch.setattr(runtime_module, "list_easyrpg", _fake_list_easyrpg)
    monkeypatch.setattr(runtime_module, "fetch_nwjs_available", _fake_fetch_nwjs)
    monkeypatch.setattr(runtime_module, "fetch_easyrpg_available", _fake_fetch_easyrpg)
    monkeypatch.setattr(runtime_module, "install_nwjs", _fake_install_nwjs)
    monkeypatch.setattr(runtime_module, "install_easyrpg", _fake_install_easyrpg)
    monkeypatch.setattr(runtime_module, "remove_nwjs", _fake_remove_nwjs)
    monkeypatch.setattr(runtime_module, "remove_easyrpg", _fake_remove_easyrpg)
    paths = _make_paths(tmp_path)
    nwjs_page: Any = NwjsPage(paths)
    easyrpg_page: Any = EasyrpgPage(paths)
    _pump()
    return nwjs_page, easyrpg_page, seen


def _make_general(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    roots: tuple[str, ...] = (),
    installed_versions: tuple[str, ...] = (),
    easyrpg_versions: tuple[str, ...] = (),
    interaction: Any | None = None,
) -> tuple[Any, Any, Any]:
    """Create a GeneralPage with real repositories and stubbed installed lists."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    installed = tuple(
        RuntimeInfo(
            RuntimeSpec(version=version, architecture="x64", sdk=False),
            tmp_path / f"rt-{version}",
            tmp_path / f"rt-{version}" / "nw",
        )
        for version in installed_versions
    )
    easyrpg_installed = tuple(
        EasyRPGRuntime(version, tmp_path / f"easy-{version}") for version in easyrpg_versions
    )
    monkeypatch.setattr(runtime_module, "list_nwjs", lambda catalog: installed)
    monkeypatch.setattr(runtime_module, "list_easyrpg", lambda catalog: easyrpg_installed)
    paths = _make_paths(tmp_path)
    repository = ConfigRepository(paths)
    for name in roots:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        repository.add_allowed_root(root)
    page: Any = GeneralPage(paths, repository, interaction)
    return page, paths, repository


def _make_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    items: dict[str, tuple[Any, ...]] | None = None,
    remove_error: BaseException | None = None,
) -> tuple[Any, Any, Any, dict[str, Any], dict[str, Any]]:
    """Create a CleanupPage with a stubbed catalog and drained initial loads."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    seen: dict[str, Any] = {"list": [], "remove": []}
    store: dict[str, tuple[Any, ...]] = dict(items) if items else {}
    state: dict[str, Any] = {"remove_error": remove_error}

    class _FakeCatalog:
        def __init__(self, paths: Any, repository: Any) -> None:
            pass

        def list(self, category: str | None = None) -> tuple[Any, ...]:
            seen["list"].append(category)
            if category is None:
                merged: list[Any] = []
                for name in CATEGORIES:
                    merged.extend(store.get(name, ()))
                return tuple(merged)
            return store.get(category, ())

        def remove(self, item: Any) -> None:
            seen["remove"].append(item)
            error = state["remove_error"]
            if error is not None:
                raise error
            current = store.get(item.category, ())
            store[item.category] = tuple(entry for entry in current if entry is not item)

    monkeypatch.setattr(settings_module, "CleanupCatalog", _FakeCatalog)
    paths = _make_paths(tmp_path)
    repository = ConfigRepository(paths)
    page: Any = CleanupPage(paths, repository)
    _pump()
    return page, paths, repository, seen, store


def _make_item(category: str, label: str, tmp_path: Path) -> Any:
    """Build an item whose label differs from its value, locking title use."""
    return CleanupItem(category, f"{category}:{label}", label, tmp_path / f"value-{label}")


def _shown_categories() -> list[str]:
    """Return the cleanup categories rendered on the Data page."""
    return [category for category in CATEGORIES if category != "roots"]


class _DenyInteraction:
    """Interaction stub refusing every game-root confirmation."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def confirm_add_root(self, path: Path) -> bool:
        """Record the prompt and refuse the root."""
        self.calls.append(path)
        return False


class _AllowInteraction:
    """Interaction stub accepting every game-root confirmation."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def confirm_add_root(self, path: Path) -> bool:
        """Record the prompt and accept the root."""
        self.calls.append(path)
        return True


def _combo_entries(row: Any) -> list[str]:
    """Return one preferred-runtime picker entries in order."""
    model = row.get_model()
    assert isinstance(model, Gtk.StringList)
    return [model.get_string(index) for index in range(model.get_n_items())]


def test_general_lists_roots_with_remove(tmp_path: Path, monkeypatch: Any) -> None:
    """Configured roots render one row each, and Remove drops only its root."""
    page, _paths, repository = _make_general(
        monkeypatch, tmp_path, roots=("games-one", "games-two")
    )

    assert [row.get_title() for row in page._root_rows] == [
        GLib.markup_escape_text(abbreviate_display_path(tmp_path / "games-one"), -1),
        GLib.markup_escape_text(abbreviate_display_path(tmp_path / "games-two"), -1),
    ]
    assert page._roots_expander.get_title() == "2 roots configured."
    assert page._roots_expander.get_expanded() is False

    first_button = page._root_rows[0].get_first_child()
    assert first_button is not None
    page._make_root_remove_handler(tmp_path / "games-one")(Gtk.Button(label="x"))

    assert [root.name for root in repository.load().allowed_game_roots] == ["games-two"]
    assert [row.get_title() for row in page._root_rows] == [
        GLib.markup_escape_text(abbreviate_display_path(tmp_path / "games-two"), -1)
    ]
    assert [row.get_tooltip_text() for row in page._root_rows] == [str(tmp_path / "games-two")]


def test_general_empty_roots_show_empty_state(tmp_path: Path, monkeypatch: Any) -> None:
    """A fresh configuration renders no rows and a collapsed empty summary."""
    page, _paths, _repository = _make_general(monkeypatch, tmp_path)

    assert page._root_rows == []
    assert page._roots_expander.get_title() == "No game roots configured."
    assert page._roots_expander.get_expanded() is False


def test_general_roots_summary_counts_singular(tmp_path: Path, monkeypatch: Any) -> None:
    """One root renders the singular summary while staying nested for search."""
    page, _paths, _repository = _make_general(monkeypatch, tmp_path, roots=("solo",))

    assert page._roots_expander.get_title() == "1 root configured."
    assert len(page._root_rows) == 1
    for row in page._root_rows:
        assert row.get_parent() is not None
        assert row.get_parent() is not page._roots_group


def test_general_add_root_without_interaction(tmp_path: Path, monkeypatch: Any) -> None:
    """Add root stores the folder and appends its row without any prompt."""
    page, _paths, repository = _make_general(monkeypatch, tmp_path)
    root = tmp_path / "added"
    root.mkdir()

    assert page.add_root(root) is True

    assert repository.load().allowed_game_roots == (root,)
    assert [row.get_title() for row in page._root_rows] == [
        GLib.markup_escape_text(abbreviate_display_path(root), -1)
    ]
    assert [row.get_tooltip_text() for row in page._root_rows] == [str(root)]


def test_general_add_root_denied_by_interaction(tmp_path: Path, monkeypatch: Any) -> None:
    """A denied confirmation aborts the add without touching the repository."""
    interaction = _DenyInteraction()
    page, _paths, repository = _make_general(monkeypatch, tmp_path, interaction=interaction)
    root = tmp_path / "denied"
    root.mkdir()

    assert page.add_root(root) is False

    assert interaction.calls == [root]
    assert repository.load().allowed_game_roots == ()
    assert page._root_rows == []


def test_general_add_root_accepted_by_interaction(tmp_path: Path, monkeypatch: Any) -> None:
    """An accepted confirmation stores the root exactly once."""
    interaction = _AllowInteraction()
    page, _paths, repository = _make_general(monkeypatch, tmp_path, interaction=interaction)
    root = tmp_path / "accepted"
    root.mkdir()

    assert page.add_root(root) is True

    assert interaction.calls == [root]
    assert repository.load().allowed_game_roots == (root,)


def test_general_add_root_error_shows_dialog(tmp_path: Path, monkeypatch: Any) -> None:
    """Adding a missing folder fails with an Add Root dialog and no new row."""
    page, _paths, repository = _make_general(monkeypatch, tmp_path)
    headings: list[str] = []
    monkeypatch.setattr(
        Adw.AlertDialog,
        "present",
        lambda dialog, parent=None: headings.append(dialog.get_heading()),
    )

    assert page.add_root(tmp_path / "missing") is False

    assert headings == ["Add Root Failed"]
    assert repository.load().allowed_game_roots == ()
    assert page._root_rows == []


def test_general_set_preferred_runtime(tmp_path: Path, monkeypatch: Any) -> None:
    """Picking an NW.js version persists it; the first entry clears it."""
    page, _paths, repository = _make_general(
        monkeypatch, tmp_path, installed_versions=("2.0.0", "1.0.0")
    )

    assert page._nwjs_runtime_group.get_title() == "Preferred NW.js runtime"
    assert _combo_entries(page._nwjs_runtime_row) == ["Undefined", "2.0.0", "1.0.0"]
    assert page._nwjs_runtime_row.get_selected() == 0

    page._nwjs_runtime_row.set_selected(1)
    assert repository.load().preferred_runtime == "2.0.0"

    page._nwjs_runtime_row.set_selected(0)
    assert repository.load().preferred_runtime is None


def test_general_stale_preferred_runtime_stays_selectable(tmp_path: Path, monkeypatch: Any) -> None:
    """A stored NW.js version with no install left still renders selected."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    monkeypatch.setattr(runtime_module, "list_nwjs", lambda catalog: ())
    monkeypatch.setattr(runtime_module, "list_easyrpg", lambda catalog: ())
    paths = _make_paths(tmp_path)
    repository = ConfigRepository(paths)
    repository.set_preferred_runtime("9.9.9")
    page: Any = GeneralPage(paths, repository, None)

    assert _combo_entries(page._nwjs_runtime_row) == ["Undefined", "9.9.9"]
    assert page._nwjs_runtime_row.get_selected() == 1

    page._nwjs_runtime_row.set_selected(0)
    assert repository.load().preferred_runtime is None


def test_general_set_preferred_easyrpg_runtime(tmp_path: Path, monkeypatch: Any) -> None:
    """Picking an EasyRPG version persists it; the first entry clears it."""
    page, _paths, _repository = _make_general(
        monkeypatch, tmp_path, easyrpg_versions=("0.8.1", "0.8.0")
    )

    assert page._easyrpg_runtime_group.get_title() == "Preferred EasyRPG runtime"
    assert _combo_entries(page._easyrpg_runtime_row) == ["Undefined", "0.8.1", "0.8.0"]
    assert page._easyrpg_runtime_row.get_selected() == 0

    page._easyrpg_runtime_row.set_selected(1)
    assert page._defaults.load().preferred_easyrpg_runtime == "0.8.1"

    page._easyrpg_runtime_row.set_selected(0)
    assert page._defaults.load().preferred_easyrpg_runtime is None


def test_general_stale_easyrpg_preferred_runtime_stays_selectable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A stored EasyRPG version with no install left still renders selected."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    monkeypatch.setattr(runtime_module, "list_nwjs", lambda catalog: ())
    monkeypatch.setattr(runtime_module, "list_easyrpg", lambda catalog: ())
    paths = _make_paths(tmp_path)
    repository = ConfigRepository(paths)
    page: Any = GeneralPage(paths, repository, None)
    page._defaults.set_preferred_easyrpg_runtime("0.7.7")
    page.refresh_easyrpg_runtime()

    assert _combo_entries(page._easyrpg_runtime_row) == ["Undefined", "0.7.7"]
    assert page._easyrpg_runtime_row.get_selected() == 1

    page._easyrpg_runtime_row.set_selected(0)
    assert page._defaults.load().preferred_easyrpg_runtime is None


def test_cleanup_profiles_group_title_and_description(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The profiles cleanup group carries its title and explanation."""
    item = _make_item("profiles", "Profile One", tmp_path)
    page, _paths, _repository, _seen, _store = _make_cleanup(
        monkeypatch, tmp_path, items={"profiles": (item,)}
    )

    assert settings_module._category_title("profiles") == "Game profiles"
    assert page._groups["profiles"].get_title() == "Game profiles"
    assert (
        page._groups["profiles"].get_description()
        == "Additional runtime settings or cache are stored here."
    )


def test_cleanup_profiles_show_matching_game_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Profile rows name the library game whose root hashes to the ID."""
    from box.games.identity import game_id

    from box_gui.core.library import LibraryRepository

    root = tmp_path / "game"
    root.mkdir()
    page, paths, _repository, _seen, _store = _make_cleanup(monkeypatch, tmp_path)
    library = LibraryRepository(paths)
    library.add(root, "Sample Game")
    page._library = library
    gid = game_id(root)
    item = CleanupItem("profiles", gid, gid, tmp_path / gid)
    page._on_list_done("profiles", (item,))

    assert len(page._rows["profiles"]) == 1
    assert _row_texts(page._rows["profiles"][0]) == ["Sample Game", gid]


def test_cleanup_profiles_without_match_show_unknown_game(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Orphaned profile IDs render as unknown games instead of bare hashes."""
    gid = "0123456789abcdef"
    item = CleanupItem("profiles", gid, gid, tmp_path / gid)
    page, _paths, _repository, _seen, _store = _make_cleanup(
        monkeypatch, tmp_path, items={"profiles": (item,)}
    )

    assert len(page._rows["profiles"]) == 1
    assert _row_texts(page._rows["profiles"][0]) == ["Unknown game", gid]


def test_nwjs_pager_next_previous_and_clamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Next advances, previous retreats, and page clamps at one."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _stub_runtime_api(monkeypatch)
    pages = {1: ("3.0.0", "2.0.0"), 2: ("1.0.0",), 3: ()}
    monkeypatch.setattr(runtime_module, "list_nwjs", lambda catalog: ())
    monkeypatch.setattr(runtime_module, "list_easyrpg", lambda catalog: ())
    monkeypatch.setattr(
        runtime_module,
        "fetch_nwjs_available",
        lambda page, arch, sdk: AvailableVersions(page=page, versions=pages.get(page, ())),
    )
    monkeypatch.setattr(
        runtime_module,
        "fetch_easyrpg_available",
        lambda page: AvailableEasyRPGVersions(page=page, versions=()),
    )
    page: Any = NwjsPage(_make_paths(tmp_path))
    _pump()

    assert page._page == 1
    assert page._versions == ("3.0.0", "2.0.0")
    assert page._prev_button.get_sensitive() is False
    page._next_button.emit("clicked")
    _pump()
    assert page._page == 2
    assert page._versions == ("1.0.0",)
    assert page._prev_button.get_sensitive() is True
    page._next_button.emit("clicked")
    _pump()
    assert page._page == 3
    assert page._versions == ()
    assert page._empty_row.get_visible() is True
    page._prev_button.emit("clicked")
    _pump()
    assert page._page == 2
    page.load_page(0)
    _pump()
    assert page._page == 1


def test_easyrpg_pager_next_previous_and_clamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """EasyRPG pager mirrors the NW.js next/previous/clamp behavior."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _stub_runtime_api(monkeypatch)
    pages = {1: ("0.8.1", "0.8.0"), 2: ("0.7.0",)}
    monkeypatch.setattr(runtime_module, "list_nwjs", lambda catalog: ())
    monkeypatch.setattr(runtime_module, "list_easyrpg", lambda catalog: ())
    monkeypatch.setattr(
        runtime_module,
        "fetch_nwjs_available",
        lambda page, arch, sdk: AvailableVersions(page=page, versions=()),
    )
    monkeypatch.setattr(
        runtime_module,
        "fetch_easyrpg_available",
        lambda page: AvailableEasyRPGVersions(page=page, versions=pages.get(page, ())),
    )
    page: Any = EasyrpgPage(_make_paths(tmp_path))
    _pump()

    assert page._page == 1
    assert page._versions == ("0.8.1", "0.8.0")
    page._next_button.emit("clicked")
    _pump()
    assert page._page == 2
    page._prev_button.emit("clicked")
    _pump()
    assert page._page == 1
    page.load_page(0)
    _pump()
    assert page._page == 1
    assert page._prev_button.get_sensitive() is False


def test_nwjs_arch_sdk_feed_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Architecture selector and SDK toggle feed subsequent fetches."""
    nwjs_page, _easyrpg_page, seen = _make_runtime(monkeypatch, tmp_path)

    assert seen["fetch_nwjs"][0] == (1, "x64", False)
    nwjs_page._sdk_toggle.set_active(True)
    _pump()
    assert seen["fetch_nwjs"][-1] == (1, "x64", True)
    assert nwjs_page._sdk is True
    nwjs_page._arch_selector.set_selected(1)
    _pump()
    assert nwjs_page._arch == "ia32"
    assert seen["fetch_nwjs"][-1][1] == "ia32"


def test_easyrpg_has_no_arch_or_sdk_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """EasyRPG stays x64-only with no architecture or SDK widgets."""
    _nwjs_page, easyrpg_page, _seen = _make_runtime(monkeypatch, tmp_path)

    assert not hasattr(easyrpg_page, "_arch_selector")
    assert not hasattr(easyrpg_page, "_sdk_toggle")


def test_nwjs_confirm_cancel_aborts_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Answering cancel in the confirm dialog never calls install."""
    nwjs_page, _easyrpg_page, seen = _make_runtime(monkeypatch, tmp_path)

    _install_auto_answer(monkeypatch, "cancel")
    nwjs_page.request_install("9.9.9")
    _pump()

    assert seen["install_nwjs"] == []


def test_nwjs_confirm_close_aborts_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Closing the confirm dialog without a response aborts the install."""
    nwjs_page, _easyrpg_page, seen = _make_runtime(monkeypatch, tmp_path)

    _install_auto_answer(monkeypatch, None, close=True)
    nwjs_page.request_install("9.9.9")
    _pump()

    assert seen["install_nwjs"] == []


def test_nwjs_confirm_accept_passes_version_arch_sdk_and_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Accepting the confirm dialog forwards version, arch, SDK, and progress."""
    nwjs_page, _easyrpg_page, seen = _make_runtime(monkeypatch, tmp_path)

    nwjs_page._arch_selector.set_selected(1)
    nwjs_page._sdk_toggle.set_active(True)
    _pump()
    seen["install_nwjs"].clear()
    _install_auto_answer(monkeypatch, "install")
    nwjs_page.request_install("7.7.7")
    _pump()
    _pump()

    assert len(seen["install_nwjs"]) == 1
    version, arch, sdk, progress = seen["install_nwjs"][0]
    assert (version, arch, sdk) == ("7.7.7", "ia32", True)
    assert progress is not None


def test_install_button_spins_until_done(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The clicked Install button shows a spinner and restores its label after."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    gate = threading.Event()
    _stub_runtime_api(monkeypatch)

    def _gated_install(
        paths: Any, version: str, architecture: str, sdk: bool = False, progress: Any = None
    ) -> Any:
        assert gate.wait(timeout=10)
        return RuntimeInfo(
            RuntimeSpec(version=version, architecture=architecture, sdk=sdk),
            tmp_path / "rt",
            tmp_path / "rt" / "nw",
        )

    monkeypatch.setattr(runtime_module, "list_nwjs", lambda catalog: ())
    monkeypatch.setattr(runtime_module, "list_easyrpg", lambda catalog: ())
    monkeypatch.setattr(
        runtime_module,
        "fetch_nwjs_available",
        lambda page, arch, sdk: AvailableVersions(page=page, versions=("9.9.9",)),
    )
    monkeypatch.setattr(
        runtime_module,
        "fetch_easyrpg_available",
        lambda page: AvailableEasyRPGVersions(page=page, versions=()),
    )
    monkeypatch.setattr(runtime_module, "install_nwjs", _gated_install)
    nwjs_page: Any = NwjsPage(_make_paths(tmp_path))
    _pump()

    assert len(nwjs_page._browser_rows) == 1
    button = _suffix_widgets(nwjs_page._browser_rows[0], Gtk.Button)[0]
    assert button.get_child().get_label() == "Install"
    nwjs_page._install("9.9.9", "x64", False, button)
    assert isinstance(button.get_child(), Gtk.Spinner)
    assert button.get_sensitive() is False
    gate.set()
    _pump()
    _pump()

    assert button.get_child().get_label() == "Install"
    assert button.get_sensitive() is True


def test_easyrpg_confirm_cancel_and_accept(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """EasyRPG confirm cancel aborts while accept installs the picked version."""
    _nwjs_page, easyrpg_page, seen = _make_runtime(monkeypatch, tmp_path)

    _install_auto_answer(monkeypatch, "cancel")
    easyrpg_page.request_install("0.8.1")
    _pump()
    assert seen["install_easyrpg"] == []
    _install_auto_answer(monkeypatch, "install")
    easyrpg_page.request_install("0.8.1")
    _pump()
    _pump()

    assert len(seen["install_easyrpg"]) == 1
    version, progress = seen["install_easyrpg"][0]
    assert version == "0.8.1"
    assert progress is not None


def test_easyrpg_confirm_body_locks_x64(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The EasyRPG confirm body names the x64-only runtime explicitly."""
    _nwjs_page, easyrpg_page, _seen = _make_runtime(monkeypatch, tmp_path)
    bodies: list[str] = []
    monkeypatch.setattr(
        Adw.AlertDialog,
        "present",
        lambda dialog, parent=None: bodies.append(dialog.get_body()),
    )

    easyrpg_page.request_install("0.8.1")

    assert bodies == ["Install EasyRPG Player 0.8.1 for x64?"]


def test_progress_reporter_lands_on_main_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ProgressReporter defers the callback via idle_add instead of calling inline."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    delivered: list[tuple[int, int | None]] = []

    def _on_progress(completed: int, total: int | None) -> None:
        delivered.append((completed, total))

    reporter = settings_module._ProgressReporter(_on_progress)
    reporter(5, 10)
    assert delivered == []
    _pump(200)
    assert delivered == [(5, 10)]
    reporter.report(10, 10)
    _pump(200)
    assert delivered == [(5, 10), (10, 10)]


def test_nwjs_remove_refreshes_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Removing an NW.js runtime calls remove then refreshes the installed list."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _stub_runtime_api(monkeypatch)
    listed: list[tuple[Any, ...]] = []
    removed: list[tuple[str, str, bool]] = []
    fake_runtime = RuntimeInfo(
        RuntimeSpec(version="1.0.0", architecture="x64", sdk=False),
        tmp_path / "rt",
        tmp_path / "rt" / "nw",
    )

    def _fake_list_nwjs(catalog: Any) -> tuple[Any, ...]:
        listed.append(())
        if removed:
            return ()
        return (fake_runtime,)

    monkeypatch.setattr(runtime_module, "list_nwjs", _fake_list_nwjs)
    monkeypatch.setattr(runtime_module, "list_easyrpg", lambda catalog: ())
    monkeypatch.setattr(
        runtime_module,
        "fetch_nwjs_available",
        lambda page, arch, sdk: AvailableVersions(page=page, versions=()),
    )
    monkeypatch.setattr(
        runtime_module,
        "fetch_easyrpg_available",
        lambda page: AvailableEasyRPGVersions(page=page, versions=()),
    )

    def _fake_remove(catalog: Any, version: str, arch: str, sdk: bool = False) -> None:
        removed.append((version, arch, sdk))

    monkeypatch.setattr(runtime_module, "remove_nwjs", _fake_remove)
    page: Any = NwjsPage(_make_paths(tmp_path))
    _pump()

    assert len(listed) >= 1
    listed_before = len(listed)
    handler = page._make_remove_handler("1.0.0", "x64", False)
    handler(Gtk.Button(label="x"))
    _pump()
    _pump()

    assert removed == [("1.0.0", "x64", False)]
    assert len(listed) > listed_before


def test_runtime_busy_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A busy page ignores remove clicks and disables its controls."""
    nwjs_page, _easyrpg_page, seen = _make_runtime(monkeypatch, tmp_path)
    presented: list[Any] = []
    monkeypatch.setattr(
        Adw.AlertDialog, "present", lambda dialog, parent=None: presented.append(dialog)
    )

    nwjs_page._set_busy(True)
    try:
        assert nwjs_page._busy is True
        assert nwjs_page._prev_button.get_sensitive() is False
        assert nwjs_page._next_button.get_sensitive() is False
        assert nwjs_page._installed_group.get_sensitive() is False
        assert nwjs_page._browser_group.get_sensitive() is False
        nwjs_page._make_remove_handler("1.0.0", "x64", False)(Gtk.Button(label="x"))
        _pump(200)
    finally:
        nwjs_page._set_busy(False)

    assert presented == []
    assert seen["remove_nwjs"] == []
    assert nwjs_page._next_button.get_sensitive() is True


def test_runtime_error_heading_split(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """BoxError maps to '<Op> Failed' while other errors map to Unexpected Error."""
    nwjs_page, _easyrpg_page, _seen = _make_runtime(monkeypatch, tmp_path)
    headings: list[str] = []
    monkeypatch.setattr(
        Adw.AlertDialog,
        "present",
        lambda dialog, parent=None: headings.append(dialog.get_heading()),
    )

    nwjs_page._show_error("Load Runtimes", BoxRuntimeError("known"))
    nwjs_page._show_error("Load Runtimes", ValueError("boom"))
    nwjs_page._show_error("Install Runtime", BoxRuntimeError("known"))
    nwjs_page._show_error("Install Runtime", ValueError("boom"))

    assert headings == [
        "Load Runtimes Failed",
        "Unexpected Error",
        "Install Runtime Failed",
        "Unexpected Error",
    ]


def test_default_architecture_error_keeps_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A RuntimeError from default_architecture shows a dialog and keeps x64."""

    def _boom() -> str:
        raise BoxRuntimeError("unsupported CPU")

    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    monkeypatch.setattr(runtime_module, "default_architecture", _boom, raising=False)
    monkeypatch.setattr(runtime_module, "list_nwjs", lambda catalog: ())
    monkeypatch.setattr(runtime_module, "list_easyrpg", lambda catalog: ())
    monkeypatch.setattr(
        runtime_module,
        "fetch_nwjs_available",
        lambda page, arch, sdk: AvailableVersions(page=page, versions=()),
    )
    monkeypatch.setattr(
        runtime_module,
        "fetch_easyrpg_available",
        lambda page: AvailableEasyRPGVersions(page=page, versions=()),
    )
    _install_auto_answer(monkeypatch, "close")
    page: Any = NwjsPage(_make_paths(tmp_path))
    _pump()

    assert page._arch == "x64"


def test_missing_default_architecture_keeps_fallback_without_dialog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An old backend without default_architecture keeps x64 and shows no dialog."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    monkeypatch.delattr(runtime_module, "default_architecture", raising=False)
    monkeypatch.setattr(runtime_module, "list_nwjs", lambda catalog: ())
    monkeypatch.setattr(runtime_module, "list_easyrpg", lambda catalog: ())
    monkeypatch.setattr(
        runtime_module,
        "fetch_nwjs_available",
        lambda page, arch, sdk: AvailableVersions(page=page, versions=()),
    )
    monkeypatch.setattr(
        runtime_module,
        "fetch_easyrpg_available",
        lambda page: AvailableEasyRPGVersions(page=page, versions=()),
    )
    presented: list[Any] = []
    monkeypatch.setattr(
        Adw.AlertDialog, "present", lambda dialog, parent=None: presented.append(dialog)
    )
    page: Any = NwjsPage(_make_paths(tmp_path))
    _pump()

    assert page._arch == "x64"
    assert presented == []


def test_cleanup_renders_all_categories(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every shown category renders its items with the human-readable label."""
    shown = _shown_categories()
    items = {category: (_make_item(category, f"{category} label", tmp_path),) for category in shown}
    page, _paths, _repository, _seen, _store = _make_cleanup(monkeypatch, tmp_path, items=items)

    assert set(page._groups) == set(shown)
    for category in shown:
        rows = page._rows[category]
        assert len(rows) == 1
        assert rows[0].get_tooltip_text() == f"{category} label"
        assert page._empty_rows[category].get_visible() is False


def test_cleanup_skips_roots_managed_in_general(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Game roots render in General, so the Data page shows no roots group."""
    items = {
        category: (_make_item(category, f"{category} label", tmp_path),) for category in CATEGORIES
    }
    page, _paths, _repository, _seen, _store = _make_cleanup(monkeypatch, tmp_path, items=items)

    assert "roots" not in page._groups
    assert "roots" not in page._expanders
    assert "roots" not in page._rows


def test_cleanup_empty_states_visible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Categories without items show no rows and a visible empty-state row."""
    page, _paths, _repository, _seen, _store = _make_cleanup(monkeypatch, tmp_path)

    for category in _shown_categories():
        assert page._rows[category] == []
        assert page._empty_rows[category].get_visible() is True


def test_cleanup_categories_summarize_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each category shows one collapsed summary instead of a count status line."""
    items = {
        "runtimes": (
            _make_item("runtimes", "Runtime One", tmp_path),
            _make_item("runtimes", "Runtime Two", tmp_path),
        ),
        "downloads": (_make_item("downloads", "archive.tar.gz", tmp_path),),
    }
    page, _paths, _repository, _seen, _store = _make_cleanup(monkeypatch, tmp_path, items=items)

    assert page._expanders["runtimes"].get_title() == "2 items"
    assert page._expanders["downloads"].get_title() == "1 item"
    assert page._expanders["profiles"].get_title() == "No items."
    for category in _shown_categories():
        expander = page._expanders[category]
        assert expander.get_expanded() is False
        for row in page._rows[category]:
            assert row.get_parent() is not page._groups[category]
    assert not hasattr(page, "_status_row")


def test_cleanup_remove_cancel_aborts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Answering cancel in the confirm dialog never calls remove."""
    item = _make_item("runtimes", "Runtime One", tmp_path)
    page, _paths, _repository, seen, _store = _make_cleanup(
        monkeypatch, tmp_path, items={"runtimes": (item,)}
    )

    _install_auto_answer(monkeypatch, "cancel")
    page.request_remove(item)
    _pump()

    assert seen["remove"] == []
    assert len(page._rows["runtimes"]) == 1


def test_cleanup_remove_close_aborts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Closing the confirm dialog without a response aborts the removal."""
    item = _make_item("runtimes", "Runtime One", tmp_path)
    page, _paths, _repository, seen, _store = _make_cleanup(
        monkeypatch, tmp_path, items={"runtimes": (item,)}
    )

    _install_auto_answer(monkeypatch, None, close=True)
    page.request_remove(item)
    _pump()

    assert seen["remove"] == []
    assert len(page._rows["runtimes"]) == 1


def test_cleanup_remove_accept_calls_remove_with_exact_item_and_refreshes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Accepting the confirm dialog removes the exact item and refreshes its list."""
    item = _make_item("downloads", "archive.tar.gz", tmp_path)
    page, _paths, _repository, seen, _store = _make_cleanup(
        monkeypatch, tmp_path, items={"downloads": (item,)}
    )

    assert len(page._rows["downloads"]) == 1
    listed_before = len(seen["list"])
    _install_auto_answer(monkeypatch, "remove")
    page._make_remove_handler(item)(Gtk.Button(label="x"))
    _pump()
    _pump()

    assert len(seen["remove"]) == 1
    assert seen["remove"][0] is item
    assert len(seen["list"]) > listed_before
    assert page._rows["downloads"] == []
    assert page._empty_rows["downloads"].get_visible() is True
    assert page._busy is False


def test_cleanup_remove_handler_busy_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A busy page ignores remove clicks without dialogs or removals."""
    item = _make_item("profiles", "Profile One", tmp_path)
    page, _paths, _repository, seen, _store = _make_cleanup(
        monkeypatch, tmp_path, items={"profiles": (item,)}
    )
    presented: list[Any] = []
    monkeypatch.setattr(
        Adw.AlertDialog, "present", lambda dialog, parent=None: presented.append(dialog)
    )

    page._set_busy(True)
    try:
        page._make_remove_handler(item)(Gtk.Button(label="x"))
        _pump(200)
    finally:
        page._set_busy(False)

    assert presented == []
    assert seen["remove"] == []


def test_cleanup_remove_box_error_shows_then_refreshes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A BoxError removal shows the error, keeps the item, and refreshes."""
    item = _make_item("runtimes", "Runtime One", tmp_path)
    page, _paths, _repository, seen, _store = _make_cleanup(
        monkeypatch,
        tmp_path,
        items={"runtimes": (item,)},
        remove_error=BoxRuntimeError("locked"),
    )
    records: list[tuple[str, BaseException]] = []
    origin = CleanupPage._show_error

    def _recording(self: Any, operation: str, error: BaseException) -> None:
        records.append((operation, error))
        origin(self, operation, error)

    monkeypatch.setattr(CleanupPage, "_show_error", _recording)
    listed_before = len(seen["list"])
    _install_auto_answer(monkeypatch, "remove")
    page.request_remove(item)
    _pump()
    _pump()

    assert seen["remove"] != [] and seen["remove"][0] is item
    assert any(
        operation == "Remove Item" and isinstance(error, BoxRuntimeError)
        for operation, error in records
    )
    assert len(seen["list"]) > listed_before
    assert len(page._rows["runtimes"]) == 1
    assert page._busy is False


def test_cleanup_long_labels_wrap_and_keep_full_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Long labels wrap instead of vanishing and keep the full text available."""
    label = "/home/user/Documents/Games/Linux/" + "VeryLongGameTitle" * 6
    item = _make_item("downloads", label, tmp_path)
    page, _paths, _repository, _seen, _store = _make_cleanup(
        monkeypatch, tmp_path, items={"downloads": (item,)}
    )

    assert len(page._rows["downloads"]) == 1
    row = page._rows["downloads"][0]
    assert row.get_title() == ""
    assert _row_texts(row) == [label]
    assert _primary_label(row).get_lines() == 2
    assert row.get_tooltip_text() == label


def _primary_label(row: Any) -> Any:
    """Return the first content label rendered inside a data row."""
    found: list[Any] = []

    def _walk(widget: Any) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                child = child.get_next_sibling()
                continue
            if isinstance(child, Gtk.Label) and child.get_text():
                found.append(child)
                return
            _walk(child)
            if found:
                return
            child = child.get_next_sibling()

    _walk(row)
    assert found, "data row renders no content label"
    return found[0]


def test_cleanup_markup_chars_in_labels_do_not_break_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Labels with markup chars render as plain text while keeping the raw tooltip."""
    label = "/home/user/Games/Linux/Fear & Hunger <deluxe>"
    item = _make_item("downloads", label, tmp_path)
    page, _paths, _repository, _seen, _store = _make_cleanup(
        monkeypatch, tmp_path, items={"downloads": (item,)}
    )

    assert len(page._rows["downloads"]) == 1
    row = page._rows["downloads"][0]
    assert row.get_title() == ""
    assert _row_texts(row) == [label]
    assert row.get_tooltip_text() == label


def _row_texts(row: Any) -> list[str]:
    """Collect plain content-label texts, skipping buttons and empty labels."""
    texts: list[str] = []

    def _walk(widget: Any) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                child = child.get_next_sibling()
                continue
            if isinstance(child, Gtk.Label) and child.get_text():
                texts.append(child.get_text())
            _walk(child)
            child = child.get_next_sibling()

    _walk(row)
    return texts


def test_search_matches_option_rows_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Only option summaries keep titles; nested and data rows stay untitled."""
    general, _paths, _repository = _make_general(monkeypatch, tmp_path, roots=("games-one",))
    nwjs_page, _easyrpg_page, _seen = _make_runtime(monkeypatch, tmp_path)

    assert general._roots_expander.get_title() != ""
    assert general._nwjs_runtime_row.get_title() != ""
    assert general._easyrpg_runtime_row.get_title() != ""
    for row in general._root_rows:
        assert row.get_parent() is not general._roots_group
    assert nwjs_page._arch_row.get_title() != ""
    assert nwjs_page._sdk_row.get_title() != ""
    for row in (*nwjs_page._installed_rows, *nwjs_page._browser_rows):
        assert row.get_title() == ""
    assert nwjs_page._pager_row.get_title() == ""
    assert nwjs_page._status_row.get_title() == ""
    assert nwjs_page._empty_row.get_title() == ""
    assert [_row_texts(row) for row in nwjs_page._browser_rows] == [["1.0.0"], ["0.9.0"]]


def test_dialog_hosts_four_pages_without_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dialog is a PreferencesDialog holding the four pages with search off."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _stub_runtime_api(monkeypatch)
    monkeypatch.setattr(runtime_module, "list_nwjs", lambda catalog: ())
    monkeypatch.setattr(runtime_module, "list_easyrpg", lambda catalog: ())
    monkeypatch.setattr(
        runtime_module,
        "fetch_nwjs_available",
        lambda page, arch, sdk: AvailableVersions(page=page, versions=()),
    )
    monkeypatch.setattr(
        runtime_module,
        "fetch_easyrpg_available",
        lambda page: AvailableEasyRPGVersions(page=page, versions=()),
    )

    class _FakeCatalog:
        def __init__(self, paths: Any, repository: Any) -> None:
            pass

        def list(self, category: str | None = None) -> tuple[Any, ...]:
            return ()

        def remove(self, item: Any) -> None:
            raise AssertionError("no removal expected")

    monkeypatch.setattr(settings_module, "CleanupCatalog", _FakeCatalog)
    paths = _make_paths(tmp_path)
    repository = ConfigRepository(paths)
    dialog: Any = SettingsDialog(paths, repository, None)
    _pump()

    assert isinstance(dialog, Adw.PreferencesDialog)
    assert dialog.get_title() == "Settings"
    assert dialog.get_search_enabled() is False
    assert [page.get_name() for page in dialog.pages] == [
        "general",
        "nwjs",
        "easyrpg",
        "datos",
    ]
    assert [type(page) for page in dialog.pages] == [
        GeneralPage,
        NwjsPage,
        EasyrpgPage,
        CleanupPage,
    ]
    assert dialog.pages[0].get_title() == "General"
    assert dialog.pages[2].get_title() == "EasyRPG"
    assert dialog.pages[3].get_title() == "Data"
    assert [page.get_icon_name() for page in dialog.pages] == [
        "box-rpg-settings-symbolic",
        "box-rpg-nwjs-symbolic",
        "box-rpg-easyrpg-symbolic",
        "box-rpg-trash-symbolic",
    ]
    for page in dialog.pages:
        assert isinstance(page, Adw.PreferencesPage)
        assert page.get_parent() is not None


def test_general_roots_escape_markup_chars(tmp_path: Path, monkeypatch: Any) -> None:
    """Allowed-root rows with & render escaped instead of breaking markup."""
    root_name = "Fear & Hunger"
    page, _paths, _repository = _make_general(monkeypatch, tmp_path, roots=(root_name,))

    assert len(page._root_rows) == 1
    row = page._root_rows[0]
    full = str(tmp_path / root_name)
    assert row.get_title() == GLib.markup_escape_text(abbreviate_display_path(Path(full)), -1)
    assert row.get_tooltip_text() == full


def test_cleanup_path_items_reuse_abbreviated_display(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Path-valued items show the General-style short path, tooltip keeps full."""
    root = tmp_path / "Fear & Hunger"
    item = CleanupItem("runtimes", str(root), str(root), root)
    page, _paths, _repository, _seen, _store = _make_cleanup(
        monkeypatch, tmp_path, items={"runtimes": (item,)}
    )

    assert len(page._rows["runtimes"]) == 1
    row = page._rows["runtimes"][0]
    assert row.get_title() == ""
    assert _row_texts(row) == [abbreviate_display_path(root)]
    assert row.get_tooltip_text() == str(root)


def _suffix_widgets(row: Any, widget_type: Any) -> list[Any]:
    """Collect suffix controls of one row, skipping widget internals."""
    found: list[Any] = []

    def _walk(widget: Any) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, widget_type):
                found.append(child)
            else:
                _walk(child)
            child = child.get_next_sibling()

    _walk(row)
    return found


def test_row_suffix_controls_are_vertically_centered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Suffix buttons and selectors keep natural height instead of stretching."""
    nwjs_installed = (
        RuntimeInfo(
            RuntimeSpec(version="1.0.0", architecture="x64", sdk=False),
            tmp_path / "rt",
            tmp_path / "rt" / "nw",
        ),
    )
    easyrpg_installed = (EasyRPGRuntime("0.8.1", tmp_path / "easyrpg"),)
    nwjs_page, easyrpg_page, _seen = _make_runtime(
        monkeypatch,
        tmp_path,
        nwjs_installed=nwjs_installed,
        easyrpg_installed=easyrpg_installed,
    )

    assert len(nwjs_page._installed_rows) == 1
    assert len(easyrpg_page._installed_rows) == 1
    assert _row_texts(easyrpg_page._installed_rows[0]) == [
        "0.8.1",
        "EasyRPG Player 0.8.1 for x64",
    ]
    for row in (*nwjs_page._installed_rows, *easyrpg_page._installed_rows):
        buttons = _suffix_widgets(row, Gtk.Button)
        assert len(buttons) == 1
        assert buttons[0].get_valign() == Gtk.Align.CENTER
    for row in (*nwjs_page._browser_rows, *easyrpg_page._browser_rows):
        buttons = _suffix_widgets(row, Gtk.Button)
        assert len(buttons) == 1
        assert buttons[0].get_valign() == Gtk.Align.CENTER
    for button in (nwjs_page._prev_button, nwjs_page._next_button):
        assert button.get_valign() == Gtk.Align.CENTER
    assert nwjs_page._arch_selector.get_valign() == Gtk.Align.CENTER
    assert nwjs_page._sdk_toggle.get_valign() == Gtk.Align.CENTER
