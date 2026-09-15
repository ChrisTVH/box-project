"""Library navigation push/pop tests with display-gated widgets."""

# pyright: reportMissingImports=false
# pyright: reportPrivateUsage=false

from __future__ import annotations

import contextlib
import os
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")

    from box.api import AppPaths, ConfigRepository
    from box.api.inspect import Inspection
    from box.errors import BoxError
    from box.models import EngineName, GameInfo
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk

    import box_gui.gtk
    import box_gui.pages.library_page as library_page_module
    from box_gui.core.library import LibraryEntry, LibraryRepository
    from box_gui.pages.game_detail_page import GameDetailPage
    from box_gui.pages.library_page import LibraryPage, default_display_name

    _navigation_available = True
except Exception:
    box_gui: Any = None
    library_page_module: Any = None
    AppPaths: Any = None
    ConfigRepository: Any = None
    Inspection: Any = None
    BoxError: Any = Exception
    EngineName: Any = None
    GameInfo: Any = None
    Adw: Any = None
    Gdk: Any = None
    Gio: Any = None
    GLib: Any = None
    Gtk: Any = None
    GameDetailPage: Any = None
    LibraryEntry: Any = None
    LibraryRepository: Any = None
    LibraryPage: Any = None
    default_display_name: Any = None
    _navigation_available = False

pytestmark = pytest.mark.skipif(not _navigation_available, reason="gi/Adw unavailable")

# Display helpers mirror docs/box-rpg-next/tests/conftest.py so widget tests
# only run where a display looks available while pure tests stay headless.


def _has_display() -> bool:
    """Return True when a Wayland or X11 display looks available."""
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _require_display() -> None:
    """Skip the test when no display is available for real widgets."""
    if not _has_display():
        pytest.skip("no display for library widgets")


def _capture_alerts(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record AlertDialog presents without showing real dialogs."""
    presented: list[Any] = []

    def _fake_present(self: Any, parent: Any | None = None) -> None:
        presented.append(self)

    monkeypatch.setattr(Adw.AlertDialog, "present", _fake_present)
    return presented


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


def _make_inspection(root: Path, title: str | None = "Demo") -> Any:
    """Build a minimal inspection pointing at root."""
    game = GameInfo(
        engine=EngineName.RPG_MAKER_MV,
        root=root,
        entrypoint=root / "www" / "index.html",
        manifest=root / "package.json",
    )
    return Inspection(game=game, title=title, plugin_count=1)


def _install_fake_inspect(monkeypatch: pytest.MonkeyPatch, factory: Any) -> Any:
    """Serve synchronous fake inspections for worker run_inspect calls."""
    calls: list[Any] = []
    module = types.ModuleType("box_gui.gtk.workers")

    def _run_inspect(path: Any, on_done: Any, on_error: Any) -> None:
        calls.append(path)
        on_done(factory(path))
        return None

    def _run_in_thread(fn: Any, on_done: Any, on_error: Any) -> None:
        try:
            result = fn()
        except BaseException as exc:
            on_error(exc)
        else:
            on_done(result)
        return None

    module.run_inspect = _run_inspect  # type: ignore[attr-defined]
    module.run_in_thread = _run_in_thread  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "box_gui.gtk.workers", module)
    monkeypatch.setattr(box_gui.gtk, "workers", module, raising=False)
    return calls


def _install_fake_launch_workers(
    monkeypatch: pytest.MonkeyPatch,
    factory: Any,
    launch_calls: list[Any],
    inspect_error: Any | None = None,
    launch_error: Any | None = None,
) -> list[Any]:
    """Serve synchronous fake inspect/launch workers for quick-launch tests."""
    inspect_calls: list[Any] = []
    module = types.ModuleType("box_gui.gtk.workers")

    def _run_inspect(path: Any, on_done: Any, on_error: Any) -> None:
        inspect_calls.append(path)
        if inspect_error is not None:
            on_error(inspect_error)
        else:
            on_done(factory(path))
        return None

    def _run_launch(
        paths: Any,
        repository: Any,
        game_path: Any,
        interaction: Any,
        on_done: Any,
        on_error: Any,
        *,
        version: Any = None,
        sdk: Any = False,
        copy_root_files: Any = (),
        allow_network: bool = False,
        allow_game_writes: bool = False,
        x11: bool = False,
    ) -> None:
        launch_calls.append(
            {
                "paths": paths,
                "repository": repository,
                "game_path": game_path,
                "interaction": interaction,
                "version": version,
                "sdk": sdk,
                "copy_root_files": copy_root_files,
                "allow_network": allow_network,
                "allow_game_writes": allow_game_writes,
                "x11": x11,
            }
        )
        if launch_error is not None:
            on_error(launch_error)
        else:
            on_done(0)
        return None

    def _run_in_thread(fn: Any, on_done: Any, on_error: Any) -> None:
        try:
            result = fn()
        except BaseException as exc:
            on_error(exc)
        else:
            on_done(result)
        return None

    module.run_inspect = _run_inspect  # type: ignore[attr-defined]
    module.run_launch = _run_launch  # type: ignore[attr-defined]
    module.run_in_thread = _run_in_thread  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "box_gui.gtk.workers", module)
    monkeypatch.setattr(box_gui.gtk, "workers", module, raising=False)
    return inspect_calls


def _row_launch_button(row: Any) -> Any:
    """Return the quick-launch button inside a library row."""
    for widget in _row_widgets(row):
        if isinstance(widget, Gtk.Button) and widget.get_tooltip_text() == "Launch":
            return widget
    raise AssertionError("library row has no launch button")


def _make_repository(tmp_path: Path) -> Any:
    """Build a library repository rooted in an isolated temporary directory."""
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    return LibraryRepository(paths)


def _row_titles(page: Any) -> list[str]:
    """Collect visible row titles from the library list box."""
    titles: list[str] = []
    child = page._list_box.get_first_child()
    while child is not None:
        if isinstance(child, Adw.ActionRow):
            titles.append(str(child.get_title()))
        child = child.get_next_sibling()
    return titles


def test_default_display_name_uses_title(tmp_path: Path) -> None:
    """A detected title becomes the display-name default."""
    assert default_display_name(_make_inspection(tmp_path / "game", "My Game")) == "My Game"


def test_default_display_name_falls_back_to_folder(tmp_path: Path) -> None:
    """A missing or empty title falls back to the game folder name."""
    assert default_display_name(_make_inspection(tmp_path / "cool-game", None)) == "cool-game"
    assert default_display_name(_make_inspection(tmp_path / "cool-game", "")) == "cool-game"


def test_pages_are_navigation_pages() -> None:
    """Both pages subclass Adw.NavigationPage without instantiating widgets."""
    assert issubclass(LibraryPage, Adw.NavigationPage)
    assert issubclass(GameDetailPage, Adw.NavigationPage)
    assert library_page_module.inspection_error_heading(ValueError("boom")) == "Unexpected Error"


def test_push_pop_keeps_list_intact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Opening a row pushes its detail page; back keeps the list intact."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    repository = _make_repository(tmp_path)
    first = repository.add(tmp_path / "alpha", "Alpha")
    repository.add(tmp_path / "beta", "Beta")
    _install_fake_inspect(monkeypatch, lambda path: _make_inspection(path, path.name))
    paths = _make_paths(tmp_path)
    pushed: list[Any] = []
    navigation = Adw.NavigationView()
    page = LibraryPage(library=repository, on_open_game=pushed.append)
    navigation.push(page)

    assert navigation.get_visible_page() is page
    assert _row_titles(page) == ["Alpha", "Beta"]

    rows = page._list_box.get_first_child()
    assert isinstance(rows, Adw.ActionRow)
    rows.emit("activated")

    assert len(pushed) == 1
    assert pushed[0].path == first.path
    detail = GameDetailPage(
        entry=pushed[0],
        paths=paths,
        repository=ConfigRepository(paths),
        library=repository,
    )
    navigation.push(detail)
    try:
        assert navigation.get_visible_page() is detail
        assert detail.get_tag() == "game-detail"

        assert navigation.pop() is True
        assert navigation.get_visible_page() is page
        assert page.get_tag() == "library"
        assert _row_titles(page) == ["Alpha", "Beta"]
        assert [entry.display_name for entry in repository.load()] == ["Alpha", "Beta"]
    finally:
        with contextlib.suppress(Exception):
            detail.destroy()


def test_back_navigation_refreshes_renamed_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Detail edits appear in the list after navigating back to it."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    from box_gui.app import BoxRpgApplication

    repository = _make_repository(tmp_path)
    repository.add(tmp_path / "alpha", "Alpha")
    navigation = Adw.NavigationView()
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)
    navigation.push(page)
    navigation.connect("popped", BoxRpgApplication._refresh_library_on_return)
    other = Adw.NavigationPage(child=Gtk.Label(label="Other"), title="Other")
    navigation.push(other)

    stored = repository.load()[0]
    repository.update(replace(stored, display_name="Renamed"))

    assert navigation.pop() is True
    assert navigation.get_visible_page() is page
    assert _row_titles(page) == ["Renamed"]


def test_inspect_and_add_pushes_detail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A successful inspect adds the game and opens it straight away."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    repository = _make_repository(tmp_path)
    root = tmp_path / "picked"
    root.mkdir()
    _install_fake_inspect(monkeypatch, lambda path: _make_inspection(path, "Picked Game"))
    opened: list[Any] = []
    page = LibraryPage(library=repository, on_open_game=opened.append)

    page.inspect_and_add(root)

    assert [entry.display_name for entry in repository.load()] == ["Picked Game"]
    assert _row_titles(page) == ["Picked Game"]
    assert len(opened) == 1
    assert opened[0].path == root


def test_folder_chosen_starts_add(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The folder picker callback resolves the path into inspect-and-add."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    repository = _make_repository(tmp_path)
    calls = _install_fake_inspect(monkeypatch, lambda path: _make_inspection(path, "Chosen Game"))
    opened: list[Any] = []
    page = LibraryPage(library=repository, on_open_game=opened.append)
    root = tmp_path / "chosen"
    root.mkdir()

    class _Source:
        def select_folder_finish(self, _result: Any) -> Any:
            return Gio.File.new_for_path(str(root))

    page._on_folder_chosen(_Source(), None)

    assert [str(path) for path in calls] == [str(root)]
    assert [entry.display_name for entry in repository.load()] == ["Chosen Game"]
    assert len(opened) == 1


def test_move_action_reorders_and_refreshes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Context menu moves reorder the repository and refresh the rows."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    repository = _make_repository(tmp_path)
    repository.add(tmp_path / "a", "A")
    second = repository.add(tmp_path / "b", "B")
    page = LibraryPage(library=repository)

    page._on_move_action(None, None, second, "up")

    assert [entry.display_name for entry in repository.load()] == ["B", "A"]
    assert _row_titles(page) == ["B", "A"]

    page._on_remove_action(None, None, second)

    assert [entry.display_name for entry in repository.load()] == ["A"]
    assert _row_titles(page) == ["A"]


def test_corrupt_library_shows_alert(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A corrupt library file surfaces as an alert instead of a crash."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    repository = _make_repository(tmp_path)
    repository.library_file.parent.mkdir(parents=True, exist_ok=True)
    repository.library_file.write_text("{not valid json", encoding="utf-8")

    page = LibraryPage(library=repository)

    assert len(presented) == 1
    assert presented[0].get_heading() == "Library Error"
    assert page._entries == ()


def test_settings_button_uses_callback(tmp_path: Path) -> None:
    """The gear button presents Settings through its callback."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    shown: list[Any] = []
    page = LibraryPage(library=repository, on_open_settings=shown.append)

    page._settings_button.emit("clicked")

    assert shown == [page]


def test_duplicate_add_shows_library_alert(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Adding an already-listed game alerts without opening a page."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    repository = _make_repository(tmp_path)
    root = tmp_path / "same"
    root.mkdir()
    repository.add(root, "Same")
    _install_fake_inspect(monkeypatch, lambda path: _make_inspection(path, "Same"))
    opened: list[Any] = []
    page = LibraryPage(library=repository, on_open_game=opened.append)

    page.inspect_and_add(root)

    assert len(presented) == 1
    assert presented[0].get_heading() == "Library Error"
    assert opened == []
    assert [entry.display_name for entry in repository.load()] == ["Same"]


def test_library_row_escapes_markup_chars(tmp_path: Path) -> None:
    """Rows with & in names render an escaped title without tooltip."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    root = tmp_path / "Fear & Hunger"
    root.mkdir()
    entry = repository.add(root, "Fear & Hunger")
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)

    row = page._build_row(entry)

    assert row.get_title() == GLib.markup_escape_text("Fear & Hunger", -1)
    assert not row.get_subtitle()
    assert row.get_tooltip_text() is None


def test_empty_library_shows_hint(tmp_path: Path) -> None:
    """An empty library keeps the empty-state hint visible."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)

    assert page._hint.get_visible()


def test_library_with_entries_hides_hint(tmp_path: Path) -> None:
    """A library with entries hides the empty-state hint."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    repository.add(tmp_path / "game", "Game")
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)

    assert not page._hint.get_visible()


def _row_widgets(widget: Any) -> list[Any]:
    """Collect a widget and all its descendants."""
    collected = [widget]
    child = widget.get_first_child()
    while child is not None:
        collected.extend(_row_widgets(child))
        child = child.get_next_sibling()
    return collected


def _row_pill_text(row: Any) -> str:
    """Return the runtime pill label inside a library row."""
    for widget in _row_widgets(row):
        if isinstance(widget, Gtk.Label) and widget.has_css_class("caption"):
            return str(widget.get_text())
    raise AssertionError("library row has no runtime pill")


def _make_entry(tmp_path: Path, engine: str | None, preferred: str | None) -> Any:
    """Build a library entry with the given engine and preferred runtime."""
    return LibraryEntry(
        path=tmp_path / "game",
        display_name="Game",
        order=0,
        preferred_runtime=preferred,
        preferred_sdk=False,
        copy_root_files=(),
        engine=engine,
    )


def test_library_row_pill_shows_engine_and_version(tmp_path: Path) -> None:
    """A known engine with a preferred runtime shows both in the pill."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)

    nwjs_row = page._build_row(_make_entry(tmp_path, "rpg-maker-mv", "0.83.0"))
    easyrpg_row = page._build_row(_make_entry(tmp_path, "rpg-maker-2000-2003", "0.8.1"))

    assert _row_pill_text(nwjs_row) == "NW.js 0.83.0"
    assert _row_pill_text(easyrpg_row) == "EasyRPG 0.8.1"


def test_library_row_pill_shows_engine_without_preferred(tmp_path: Path) -> None:
    """A known engine without a preferred runtime shows only the engine."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)

    row = page._build_row(_make_entry(tmp_path, "rpg-maker-mz", None))

    assert _row_pill_text(row) == "NW.js"


def test_library_row_pill_falls_back_without_engine(tmp_path: Path) -> None:
    """A legacy entry without an engine keeps its version or Latest."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)

    versioned = page._build_row(_make_entry(tmp_path, None, "0.83.0"))
    legacy = page._build_row(_make_entry(tmp_path, None, None))

    assert _row_pill_text(versioned) == "0.83.0"
    assert _row_pill_text(legacy) == "Latest"


def test_library_row_only_pill_has_runtime_pill_class(tmp_path: Path) -> None:
    """Only the runtime pill carries the pill background class."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)

    row = page._build_row(_make_entry(tmp_path, "rpg-maker-mv", "0.83.0"))
    widgets = _row_widgets(row)
    pills = [
        widget
        for widget in widgets
        if isinstance(widget, Gtk.Label) and widget.has_css_class("runtime-pill")
    ]

    assert len(pills) == 1
    pill = pills[0]
    assert pill.get_valign() == Gtk.Align.CENTER
    assert pill.has_css_class("caption")
    assert pill.has_css_class("dim")
    assert not row.has_css_class("runtime-pill")
    menu_buttons = [widget for widget in widgets if isinstance(widget, Gtk.MenuButton)]
    assert len(menu_buttons) == 1
    assert not menu_buttons[0].has_css_class("runtime-pill")
    assert not _row_launch_button(row).has_css_class("runtime-pill")


def test_library_row_has_centered_menu_button(tmp_path: Path) -> None:
    """Rows expose a centered menu button instead of a passive marker."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    entry = repository.add(tmp_path / "game", "Game")
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)

    row = page._build_row(entry)
    widgets = _row_widgets(row)
    buttons = [widget for widget in widgets if isinstance(widget, Gtk.MenuButton)]

    assert len(buttons) == 1
    assert buttons[0].get_valign() == Gtk.Align.CENTER
    assert buttons[0].has_css_class("flat")
    assert buttons[0].get_visible() is True
    assert buttons[0].get_icon_name() == "box-rpg-dots-symbolic"
    assert buttons[0].get_tooltip_text() == "Reorder or remove"
    assert buttons[0].get_popover() is not None
    labels = [widget.get_text() for widget in widgets if isinstance(widget, Gtk.Label)]
    assert "::" not in labels

    controllers = row.observe_controllers()
    right_click = False
    for index in range(controllers.get_n_items()):
        controller = controllers.get_item(index)
        if isinstance(controller, Gtk.GestureClick) and controller.get_button() == 3:
            right_click = True
    assert right_click


def test_single_row_hides_reorder_menu(tmp_path: Path) -> None:
    """A lone row cannot be reordered, so its menu stays hidden."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    repository.add(tmp_path / "solo", "Solo")
    first = LibraryPage(library=repository, on_open_game=lambda entry: None)
    repository.add(tmp_path / "second", "Second")
    second = LibraryPage(library=repository, on_open_game=lambda entry: None)

    def _menu_button(page: Any) -> Any:
        row = page._list_box.get_first_child()
        assert isinstance(row, Adw.ActionRow)
        buttons = [widget for widget in _row_widgets(row) if isinstance(widget, Gtk.MenuButton)]
        assert len(buttons) == 1
        return buttons[0]

    assert _menu_button(first).get_visible() is False
    assert _menu_button(second).get_visible() is True


def test_bundled_tabler_icons_resolve(tmp_path: Path) -> None:
    """Vendored box-rpg-* icons resolve through the registered search path."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    from box_gui.app import _register_bundled_icons

    _register_bundled_icons()
    display = Gdk.Display.get_default()
    assert display is not None
    theme = Gtk.IconTheme.get_for_display(display)

    for name in (
        "box-rpg-plus-symbolic",
        "box-rpg-settings-symbolic",
        "box-rpg-dots-symbolic",
        "box-rpg-rocket-symbolic",
        "box-rpg-x-symbolic",
        "box-rpg-trash-symbolic",
        "box-rpg-nwjs-symbolic",
        "box-rpg-easyrpg-symbolic",
        "box-rpg-box-symbolic",
    ):
        assert theme.has_icon(name), f"unresolved bundled icon {name}"


def test_add_button_sits_on_the_left(tmp_path: Path) -> None:
    """The + action packs at the header start, settings stays at the end."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)

    seen: list[str] = []

    def _walk(widget: Any) -> None:
        child = widget.get_first_child()
        while child is not None:
            if child is page._add_button:
                seen.append("add")
            elif child is page._settings_button:
                seen.append("settings")
            _walk(child)
            child = child.get_next_sibling()

    _walk(page._header_bar)

    assert seen == ["add", "settings"]


def test_library_row_launch_button_layout(tmp_path: Path) -> None:
    """Rows expose a centered launch button instead of a passive chevron."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    entry = repository.add(tmp_path / "game", "Game")
    paths = _make_paths(tmp_path)
    page = LibraryPage(
        library=repository,
        on_open_game=lambda entry: None,
        paths=paths,
        repository=ConfigRepository(paths),
    )

    row = page._build_row(entry)
    button = _row_launch_button(row)

    assert button.get_valign() == Gtk.Align.CENTER
    assert button.has_css_class("flat")
    assert button.get_tooltip_text() == "Launch"
    assert button.get_sensitive() is True
    icon = button.get_first_child()
    assert isinstance(icon, Gtk.Image)
    assert icon.get_icon_name() == "box-rpg-rocket-symbolic"
    standalone = [
        widget
        for widget in _row_widgets(row)
        if isinstance(widget, Gtk.Image) and widget.get_parent() is row
    ]
    assert standalone == []


def test_library_row_launch_button_needs_backend(tmp_path: Path) -> None:
    """The launch button stays insensitive without backend wiring."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    entry = repository.add(tmp_path / "game", "Game")
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)

    button = _row_launch_button(page._build_row(entry))

    assert button.get_sensitive() is False


def test_launch_button_uses_saved_options(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The launch button starts the game with the entry prefs, not the row."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    repository = _make_repository(tmp_path)
    created = repository.add(tmp_path / "game", "Game")
    entry = repository.update(
        LibraryEntry(
            path=created.path,
            display_name=created.display_name,
            order=created.order,
            preferred_runtime="0.99.0",
            preferred_sdk=True,
            copy_root_files=("extra.txt",),
            engine="rpg-maker-mv",
            allow_network=True,
            allow_game_writes=False,
            allow_x11=True,
        )
    )
    paths = _make_paths(tmp_path)
    config_repository = ConfigRepository(paths)
    launch_calls: list[Any] = []
    _install_fake_launch_workers(
        monkeypatch, lambda path: _make_inspection(path, "Game"), launch_calls
    )
    opened: list[Any] = []
    page = LibraryPage(
        library=repository,
        on_open_game=opened.append,
        paths=paths,
        repository=config_repository,
    )

    row = page._list_box.get_first_child()
    assert isinstance(row, Adw.ActionRow)
    button = _row_launch_button(row)
    assert button.get_sensitive() is True

    button.emit("clicked")

    assert len(launch_calls) == 1
    assert launch_calls[0]["version"] == "0.99.0"
    assert launch_calls[0]["sdk"] is True
    assert launch_calls[0]["copy_root_files"] == ("extra.txt",)
    assert launch_calls[0]["allow_network"] is True
    assert launch_calls[0]["allow_game_writes"] is False
    assert launch_calls[0]["x11"] is True
    assert launch_calls[0]["game_path"] == entry.path
    assert opened == []
    assert presented == []
    assert button.get_sensitive() is True


def test_launch_button_drops_sdk_and_files_for_easyrpg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Quick-launch forces SDK/files off for EasyRPG like the detail page."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    repository = _make_repository(tmp_path)
    created = repository.add(tmp_path / "game", "Game")
    repository.update(
        LibraryEntry(
            path=created.path,
            display_name=created.display_name,
            order=created.order,
            preferred_runtime="0.8.1",
            preferred_sdk=True,
            copy_root_files=("extra.txt",),
            engine="rpg-maker-2000-2003",
        )
    )
    paths = _make_paths(tmp_path)
    config_repository = ConfigRepository(paths)
    launch_calls: list[Any] = []

    def _easyrpg_inspection(path: Path) -> Any:
        game = GameInfo(
            engine=EngineName.RPG_MAKER_2000_2003,
            root=path,
            entrypoint=path / "RPG_RT.exe",
            manifest=path / "RPG_RT.ini",
        )
        return Inspection(game=game, title="Game", plugin_count=0)

    _install_fake_launch_workers(monkeypatch, _easyrpg_inspection, launch_calls)
    page = LibraryPage(
        library=repository,
        on_open_game=lambda entry: None,
        paths=paths,
        repository=config_repository,
    )

    button = _row_launch_button(page._build_row(repository.load()[0]))
    button.emit("clicked")

    assert len(launch_calls) == 1
    assert launch_calls[0]["version"] == "0.8.1"
    assert launch_calls[0]["sdk"] is False
    assert launch_calls[0]["copy_root_files"] == ()
    assert presented == []
    assert button.get_sensitive() is True


def test_launch_button_uses_options_edited_in_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rows built before a detail edit still launch with the fresh options."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    repository = _make_repository(tmp_path)
    created = repository.add(tmp_path / "game", "Game")
    paths = _make_paths(tmp_path)
    config_repository = ConfigRepository(paths)
    launch_calls: list[Any] = []
    _install_fake_launch_workers(
        monkeypatch, lambda path: _make_inspection(path, "Game"), launch_calls
    )
    page = LibraryPage(
        library=repository,
        on_open_game=lambda entry: None,
        paths=paths,
        repository=config_repository,
    )
    row = page._build_row(created)
    repository.update(
        LibraryEntry(
            path=created.path,
            display_name=created.display_name,
            order=created.order,
            preferred_runtime="1.2.3",
            preferred_sdk=False,
            copy_root_files=(),
            engine="rpg-maker-mv",
        )
    )

    _row_launch_button(row).emit("clicked")

    assert len(launch_calls) == 1
    assert launch_calls[0]["version"] == "1.2.3"


def test_launch_button_inspect_error_alerts_without_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed pre-launch inspection alerts without starting a launch."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    repository = _make_repository(tmp_path)
    repository.add(tmp_path / "game", "Game")
    paths = _make_paths(tmp_path)
    launch_calls: list[Any] = []
    _install_fake_launch_workers(
        monkeypatch,
        lambda path: _make_inspection(path, "Game"),
        launch_calls,
        inspect_error=BoxError("missing game"),
    )
    page = LibraryPage(
        library=repository,
        on_open_game=lambda entry: None,
        paths=paths,
        repository=ConfigRepository(paths),
    )

    row = page._list_box.get_first_child()
    assert isinstance(row, Adw.ActionRow)
    button = _row_launch_button(row)

    button.emit("clicked")

    assert launch_calls == []
    assert len(presented) == 1
    assert presented[0].get_heading() == "Inspection Failed"
    assert button.get_sensitive() is True


def test_launch_button_error_alerts_and_reenables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed quick launch alerts and reactivates the button."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    presented = _capture_alerts(monkeypatch)
    repository = _make_repository(tmp_path)
    repository.add(tmp_path / "game", "Game")
    paths = _make_paths(tmp_path)
    launch_calls: list[Any] = []
    _install_fake_launch_workers(
        monkeypatch,
        lambda path: _make_inspection(path, "Game"),
        launch_calls,
        launch_error=BoxError("no runtime"),
    )
    page = LibraryPage(
        library=repository,
        on_open_game=lambda entry: None,
        paths=paths,
        repository=ConfigRepository(paths),
    )

    row = page._list_box.get_first_child()
    assert isinstance(row, Adw.ActionRow)
    button = _row_launch_button(row)

    button.emit("clicked")

    assert len(launch_calls) == 1
    assert len(presented) == 1
    assert presented[0].get_heading() == "Launch Failed"
    assert button.get_sensitive() is True


def _make_easyrpg_inspection(root: Path, title: str | None = "Demo") -> Any:
    """Build a minimal EasyRPG inspection pointing at root."""
    game = GameInfo(
        engine=EngineName.RPG_MAKER_2000_2003,
        root=root,
        entrypoint=root / "RPG_RT.exe",
        manifest=root / "RPG_RT.ini",
    )
    return Inspection(game=game, title=title, plugin_count=0)


def _make_mz_inspection(root: Path, title: str | None = "Demo") -> Any:
    """Build a minimal RPG Maker MZ inspection pointing at root."""
    game = GameInfo(
        engine=EngineName.RPG_MAKER_MZ,
        root=root,
        entrypoint=root / "www" / "index.html",
        manifest=root / "package.json",
    )
    return Inspection(game=game, title=title, plugin_count=1)


def test_inspect_and_add_prefills_runtime_per_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """New entries start from the global runtime matching their engine."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    from box_gui.core.defaults import DefaultsRepository

    paths = _make_paths(tmp_path)
    config_repository = ConfigRepository(paths)
    config_repository.set_preferred_runtime("0.83.0")
    defaults_repository = DefaultsRepository(paths)
    defaults_repository.set_preferred_easyrpg_runtime("0.8.1")

    repository = _make_repository(tmp_path)
    opened: list[Any] = []
    page = LibraryPage(
        library=repository,
        on_open_game=opened.append,
        paths=paths,
        repository=config_repository,
        defaults_repository=defaults_repository,
    )

    page._on_inspect_done(_make_inspection(tmp_path / "mv-game", "MV Game"))
    page._on_inspect_done(_make_mz_inspection(tmp_path / "mz-game", "MZ Game"))
    page._on_inspect_done(_make_easyrpg_inspection(tmp_path / "easy-game", "Easy Game"))

    loaded = {entry.display_name: entry for entry in repository.load()}
    assert loaded["MV Game"].preferred_runtime == "0.83.0"
    assert loaded["MV Game"].engine == EngineName.RPG_MAKER_MV.value
    assert loaded["MZ Game"].preferred_runtime == "0.83.0"
    assert loaded["MZ Game"].engine == EngineName.RPG_MAKER_MZ.value
    assert loaded["Easy Game"].preferred_runtime == "0.8.1"
    assert loaded["Easy Game"].engine == EngineName.RPG_MAKER_2000_2003.value
    assert len(opened) == 3


def test_inspect_and_add_keeps_none_without_globals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without globals, new entries keep the previous None behavior."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    from box_gui.core.defaults import DefaultsRepository

    paths = _make_paths(tmp_path)
    repository = _make_repository(tmp_path)
    page = LibraryPage(
        library=repository,
        on_open_game=lambda entry: None,
        paths=paths,
        repository=ConfigRepository(paths),
        defaults_repository=DefaultsRepository(paths),
    )

    page._on_inspect_done(_make_inspection(tmp_path / "mv-game", "MV Game"))
    page._on_inspect_done(_make_easyrpg_inspection(tmp_path / "easy-game", "Easy Game"))

    loaded = {entry.display_name: entry for entry in repository.load()}
    assert loaded["MV Game"].preferred_runtime is None
    assert loaded["Easy Game"].preferred_runtime is None


def test_inspect_and_add_tolerates_broken_globals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unreadable globals fall back to None instead of blocking the add."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    from box_gui.core.defaults import DefaultsRepository

    paths = _make_paths(tmp_path)
    defaults_repository = DefaultsRepository(paths)
    defaults_repository.defaults_file.parent.mkdir(parents=True, exist_ok=True)
    defaults_repository.defaults_file.write_text("{not valid json", encoding="utf-8")

    class _FailingConfig:
        def load(self) -> Any:
            raise BoxError("broken config")

    repository = _make_repository(tmp_path)
    page = LibraryPage(
        library=repository,
        on_open_game=lambda entry: None,
        paths=paths,
        repository=_FailingConfig(),  # type: ignore[arg-type]
        defaults_repository=defaults_repository,
    )

    page._on_inspect_done(_make_inspection(tmp_path / "mv-game", "MV Game"))
    page._on_inspect_done(_make_easyrpg_inspection(tmp_path / "easy-game", "Easy Game"))

    loaded = {entry.display_name: entry for entry in repository.load()}
    assert loaded["MV Game"].preferred_runtime is None
    assert loaded["Easy Game"].preferred_runtime is None


def test_launch_button_falls_back_to_easyrpg_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Quick-launch uses the EasyRPG global when the entry has no runtime."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    from box_gui.core.defaults import DefaultsRepository

    repository = _make_repository(tmp_path)
    created = repository.add(tmp_path / "game", "Game", "rpg-maker-2000-2003")
    assert created.preferred_runtime is None
    paths = _make_paths(tmp_path)
    DefaultsRepository(paths).set_preferred_easyrpg_runtime("0.8.1")
    launch_calls: list[Any] = []
    _install_fake_launch_workers(monkeypatch, _make_easyrpg_inspection, launch_calls)
    page = LibraryPage(
        library=repository,
        on_open_game=lambda entry: None,
        paths=paths,
        repository=ConfigRepository(paths),
    )

    button = _row_launch_button(page._build_row(repository.load()[0]))
    button.emit("clicked")

    assert len(launch_calls) == 1
    assert launch_calls[0]["version"] == "0.8.1"
    assert launch_calls[0]["sdk"] is False
    assert launch_calls[0]["copy_root_files"] == ()


def test_launch_button_keeps_none_without_easyrpg_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without an EasyRPG global, quick-launch keeps the None behavior."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)

    repository = _make_repository(tmp_path)
    repository.add(tmp_path / "game", "Game", "rpg-maker-2000-2003")
    paths = _make_paths(tmp_path)
    launch_calls: list[Any] = []
    _install_fake_launch_workers(monkeypatch, _make_easyrpg_inspection, launch_calls)
    page = LibraryPage(
        library=repository,
        on_open_game=lambda entry: None,
        paths=paths,
        repository=ConfigRepository(paths),
    )

    button = _row_launch_button(page._build_row(repository.load()[0]))
    button.emit("clicked")

    assert len(launch_calls) == 1
    assert launch_calls[0]["version"] is None


def test_launch_button_keeps_none_for_nwjs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """NW.js quick-launch with None stays None instead of using the global."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)

    repository = _make_repository(tmp_path)
    repository.add(tmp_path / "game", "Game", "rpg-maker-mv")
    paths = _make_paths(tmp_path)
    ConfigRepository(paths).set_preferred_runtime("0.83.0")
    launch_calls: list[Any] = []
    _install_fake_launch_workers(
        monkeypatch, lambda path: _make_inspection(path, "Game"), launch_calls
    )
    page = LibraryPage(
        library=repository,
        on_open_game=lambda entry: None,
        paths=paths,
        repository=ConfigRepository(paths),
    )

    button = _row_launch_button(page._build_row(repository.load()[0]))
    button.emit("clicked")

    assert len(launch_calls) == 1
    assert launch_calls[0]["version"] is None


def _write_fixture_icon(path: Path, width: int = 32, height: int = 32) -> Path:
    """Write a solid PNG fixture through the same loader the rows use."""
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    path.parent.mkdir(parents=True, exist_ok=True)
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, width, height)
    pixbuf.fill(0x112233FF)
    pixbuf.savev(str(path), "png", [], [])
    return path


def _row_file_images(row: Any) -> list[Any]:
    """Collect file-backed row images, excluding named symbolic icons."""
    return [
        widget
        for widget in _row_widgets(row)
        if isinstance(widget, Gtk.Image) and widget.get_storage_type() == Gtk.ImageType.PAINTABLE
    ]


def _row_named_icons(row: Any, name: str) -> list[Any]:
    """Collect row images using one themed icon name."""
    return [
        widget
        for widget in _row_widgets(row)
        if isinstance(widget, Gtk.Image) and widget.get_icon_name() == name
    ]


def test_library_row_shows_extracted_icon(tmp_path: Path) -> None:
    """Rows with a cached icon_path lead with the file image."""
    from dataclasses import replace

    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    game = tmp_path / "game"
    repository = _make_repository(tmp_path)
    created = repository.add(game, "Game", "rpg-maker-mv")
    repository.update(
        replace(created, icon_path=_write_fixture_icon(tmp_path / "cache" / "icon.png"))
    )
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)

    images = _row_file_images(page._build_row(repository.load()[0]))

    assert len(images) == 1


def test_library_row_falls_back_per_engine(tmp_path: Path) -> None:
    """Rows without a cached icon lead with the engine Tabler icon."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    repository = _make_repository(tmp_path)
    repository.add(tmp_path / "mv", "MV", "rpg-maker-mv")
    repository.add(tmp_path / "2k3", "2k3", "rpg-maker-2000-2003")
    repository.add(tmp_path / "mystery", "Mystery")
    page = LibraryPage(library=repository, on_open_game=lambda entry: None)
    rows = {entry.display_name: page._build_row(entry) for entry in repository.load()}

    assert len(_row_named_icons(rows["MV"], "box-rpg-nwjs-symbolic")) == 1
    assert len(_row_named_icons(rows["2k3"], "box-rpg-easyrpg-symbolic")) == 1
    assert len(_row_named_icons(rows["Mystery"], "box-rpg-box-symbolic")) == 1
    assert _row_file_images(rows["MV"]) == []


def test_exe_picker_resolve(tmp_path: Path) -> None:
    """The picker lists executables and maps responses to paths."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    from box_gui.widgets.exe_picker import build_exe_picker, resolve_exe_choice

    exes = (tmp_path / "a.exe", tmp_path / "b.exe")
    dialog, selector = build_exe_picker(exes)

    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_heading() == "Select the game executable."
    assert selector.get_selected() == 0
    assert resolve_exe_choice(selector, exes, "select") == "default"
    selector.set_selected(2)
    assert resolve_exe_choice(selector, exes, "select") == exes[1]
    assert resolve_exe_choice(selector, exes, "cancel") is None
    _empty_dialog, empty_selector = build_exe_picker(())
    assert resolve_exe_choice(empty_selector, (), "select") == "default"


def test_file_icon_bakes_rounded_corners(tmp_path: Path) -> None:
    """File icons keep their size but lose the opaque square corners."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    from box_gui.widgets import icon_widget

    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 48, 48)
    pixbuf.fill(0x112233FF)
    rounded = icon_widget._rounded_pixbuf(pixbuf)

    assert rounded is not None
    assert (rounded.get_width(), rounded.get_height()) == (48, 48)
    assert rounded.get_has_alpha() is True
    pixels = bytes(rounded.get_pixels())
    stride = rounded.get_rowstride()

    def _alpha(x: int, y: int) -> int:
        return pixels[y * stride + x * 4 + 3]

    assert _alpha(0, 0) == 0
    assert _alpha(47, 47) == 0
    assert _alpha(24, 0) == 255
    assert _alpha(24, 24) == 255
