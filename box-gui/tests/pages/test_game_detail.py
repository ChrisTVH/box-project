"""Game detail persisted/non-persisted field tests with display gating."""

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
    from box.errors import RuntimeError as BoxRuntimeError
    from box.models import EngineName, GameInfo
    from box.runtime.catalog import RuntimeCatalog, RuntimeInfo, RuntimeSpec
    from gi.repository import Adw

    import box_gui.gtk
    from box_gui.core.library import LibraryEntry, LibraryRepository
    from box_gui.pages.game_detail_page import (
        AllowX11Interaction,
        GameDetailPage,
        inspection_error_heading,
        launch_error_heading,
    )

    _detail_available = True
except Exception:
    box_gui: Any = None
    AppPaths: Any = None
    ConfigRepository: Any = None
    Inspection: Any = None
    BoxError: Any = Exception
    BoxRuntimeError: Any = Exception
    EngineName: Any = None
    GameInfo: Any = None
    RuntimeCatalog: Any = None
    RuntimeInfo: Any = None
    RuntimeSpec: Any = None
    Adw: Any = None
    AllowX11Interaction: Any = None
    GameDetailPage: Any = None
    inspection_error_heading: Any = None
    launch_error_heading: Any = None
    LibraryEntry: Any = None
    LibraryRepository: Any = None
    _detail_available = False

pytestmark = pytest.mark.skipif(not _detail_available, reason="gi/Adw unavailable")


def _has_display() -> bool:
    """Return True when a Wayland or X11 display looks available."""
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _require_display() -> None:
    """Skip the test when no display is available for real widgets."""
    if not _has_display():
        pytest.skip("no display for game detail widgets")


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


def _make_inspection(root: Path, engine: Any = None, title: str | None = "Demo") -> Any:
    """Build a minimal inspection pointing at root."""
    game = GameInfo(
        engine=engine or EngineName.RPG_MAKER_MV,
        root=root,
        entrypoint=root / "www" / "index.html",
        manifest=root / "package.json",
    )
    return Inspection(game=game, title=title, plugin_count=3)


def _install_fake_workers(monkeypatch: pytest.MonkeyPatch, factory: Any) -> dict[str, Any]:
    """Serve synchronous fake inspections plus a synchronous thread runner."""
    state: dict[str, Any] = {"inspects": []}
    module = types.ModuleType("box_gui.gtk.workers")

    def _run_inspect(path: Any, on_done: Any, on_error: Any) -> None:
        state["inspects"].append(path)
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
    return state


def _stub_runtime_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pretend one NW.js runtime version is installed for dropdown tests."""

    def _fake_list(self: Any) -> Any:
        return (RuntimeInfo(RuntimeSpec("0.99.0", "x64"), tmp_path, tmp_path / "nw"),)

    monkeypatch.setattr(RuntimeCatalog, "list", _fake_list)


def _make_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, factory: Any | None = None
) -> tuple[Any, Any, Any]:
    """Build an inspected detail page with isolated paths and repositories."""
    _require_display()
    with contextlib.suppress(Exception):
        Adw.init()
    _capture_alerts(monkeypatch)
    _install_fake_workers(monkeypatch, factory or _make_inspection)
    _stub_runtime_catalog(monkeypatch, tmp_path)
    paths = _make_paths(tmp_path)
    repository = ConfigRepository(paths)
    library_paths = AppPaths(
        config_root=tmp_path / "library-config", cache_root=tmp_path / "library-cache"
    )
    library = LibraryRepository(library_paths)
    entry = library.add(tmp_path / "game", "Demo")
    page = GameDetailPage(
        entry=entry,
        paths=paths,
        repository=repository,
        library=library,
    )
    return page, library, entry


def test_launch_error_headings_without_display() -> None:
    """Backend errors and unexpected errors map to distinct headings."""
    assert launch_error_heading(BoxError("bad")) == "Launch Failed"
    assert launch_error_heading(ValueError("boom")) == "Unexpected Error"
    assert inspection_error_heading(BoxError("bad")) == "Inspection Failed"
    assert inspection_error_heading(ValueError("boom")) == "Unexpected Error"


def test_x11_wrapper_allows_x11_and_delegates_without_display() -> None:
    """The X11 wrapper pre-answers consent while delegating other prompts."""
    assert AllowX11Interaction(None).confirm_x11("any") is True
    assert AllowX11Interaction(None).confirm_add_root(Path("/game")) is False
    assert AllowX11Interaction(None).choose_runtime("nwjs", ("1.0",), "Pick") is None

    class _Recording:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def confirm_x11(self, display: str) -> bool:
            self.calls.append(("x11", display))
            return False

        def confirm_add_root(self, path: Path) -> bool:
            self.calls.append(("root", path))
            return True

        def choose_runtime(self, kind: str, candidates: tuple[str, ...], title: str) -> int | None:
            self.calls.append(("runtime", kind, candidates, title))
            return 1

    recording = _Recording()
    wrapper = AllowX11Interaction(recording)
    assert wrapper.confirm_x11(":0") is True
    assert recording.calls == []
    assert wrapper.confirm_add_root(Path("/game")) is True
    assert wrapper.choose_runtime("nwjs", ("1.0", "2.0"), "Pick") == 1
    assert [call[0] for call in recording.calls] == ["root", "runtime"]


def test_persisted_fields_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Display name, runtime version, and SDK edits persist to the library."""
    page, library, _entry = _make_page(monkeypatch, tmp_path)

    page._display_row.set_text("Renamed")
    page._runtime_row.set_selected(1)
    page._sdk_row.set_active(True)

    loaded = library.load()
    assert [entry.display_name for entry in loaded] == ["Renamed"]
    assert [entry.preferred_runtime for entry in loaded] == ["0.99.0"]
    assert [entry.preferred_sdk for entry in loaded] == [True]
    assert page._entry.display_name == "Renamed"


def test_stale_preferred_runtime_stays_selected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stored version with no install left stays selected instead of Undefined."""
    page, _library, entry = _make_page(monkeypatch, tmp_path)
    stale = replace(entry, preferred_runtime="9.9.9")
    page._persist(stale)
    page._sync_runtime_selection()

    model = page._runtime_row.get_model()
    assert model is not None
    names = [model.get_string(index) for index in range(model.get_n_items())]

    assert names == ["Undefined", "0.99.0", "9.9.9"]
    assert names[page._runtime_row.get_selected()] == "9.9.9"
    assert page._selected_runtime_version() == "9.9.9"


def test_display_name_is_capped_at_64_chars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Overlong display names truncate to 64 characters before persisting."""
    page, library, _entry = _make_page(monkeypatch, tmp_path)

    assert page._display_row.get_max_length() == 64
    page._display_row.set_text("n" * 100)

    assert page._display_row.get_text() == "n" * 64
    assert [entry.display_name for entry in library.load()] == ["n" * 64]


def test_root_file_options_skip_binary_and_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Binary, executable, and media files are never offered as copies."""
    page, _library, _entry = _make_page(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "box_gui.pages.game_detail_page.list_root_files",
        lambda game: (
            "config.ini",
            "lang.json",
            "data.pak",
            "game.bin",
            "game.exe",
            "lib.dll",
            "save.dat",
            "index.html",
            "icon.png",
            "cover.jpg",
            "shot.webp",
            "README",
        ),
    )

    assert page._root_file_options(object()) == ("config.ini", "lang.json", "README")


def test_add_files_dialog_empty_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An empty candidate list shows an empty row and disables Add."""
    from gi.repository import Gtk

    page, _library, _entry = _make_page(monkeypatch, tmp_path)
    page._file_options = ()
    presented: list[Any] = []
    monkeypatch.setattr(Adw.Dialog, "present", lambda dialog, parent=None: presented.append(dialog))

    page._on_add_files_clicked(page._add_file_button)

    assert len(presented) == 1
    rows: list[Any] = []
    buttons: list[Any] = []

    def _walk(widget: Any) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Adw.ActionRow):
                rows.append(child)
            if isinstance(child, Gtk.Button):
                buttons.append(child)
            else:
                _walk(child)
            child = child.get_next_sibling()

    _walk(presented[0].get_child())
    assert [row.get_title() for row in rows] == ["No eligible files."]
    assert all(row.get_sensitive() is False for row in rows)
    confirm = [button for button in buttons if button.get_label() == "Add"]
    assert len(confirm) == 1
    assert confirm[0].get_sensitive() is False


def test_add_files_dialog_enables_add_with_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Candidates render switches instead of the empty row."""
    from gi.repository import Gtk

    page, _library, _entry = _make_page(monkeypatch, tmp_path)
    page._file_options = ("extra.txt",)
    presented: list[Any] = []
    monkeypatch.setattr(Adw.Dialog, "present", lambda dialog, parent=None: presented.append(dialog))

    page._on_add_files_clicked(page._add_file_button)

    assert len(presented) == 1
    titles: list[str] = []
    buttons: list[Any] = []

    def _walk(widget: Any) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Adw.ActionRow):
                titles.append(str(child.get_title()))
            if isinstance(child, Gtk.Button):
                buttons.append(child)
            else:
                _walk(child)
            child = child.get_next_sibling()

    _walk(presented[0].get_child())
    assert "No eligible files." not in titles
    confirm = [button for button in buttons if button.get_label() == "Add"]
    assert len(confirm) == 1
    assert confirm[0].get_sensitive() is True


def test_default_option_shows_when_global_defined(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A defined NW.js global shows Por defecto instead of No definido."""
    page, _library, _entry = _make_page(monkeypatch, tmp_path)
    repository = ConfigRepository(_make_paths(tmp_path))
    repository.set_preferred_runtime("0.99.0")
    page._repository = repository
    page._sync_runtime_selection()

    model = page._runtime_row.get_model()
    assert model is not None
    names = [model.get_string(index) for index in range(model.get_n_items())]

    assert names == ["Default", "0.99.0"]
    assert page._runtime_row.get_selected() == 0
    assert page._selected_runtime_version() is None


def test_default_option_shows_for_easyrpg_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A defined EasyRPG global shows Por defecto on EasyRPG games."""
    from box_gui.core.defaults import DefaultsRepository

    page, _library, _entry = _make_page(
        monkeypatch, tmp_path, lambda path: _make_inspection(path, EngineName.RPG_MAKER_2000_2003)
    )
    DefaultsRepository(_make_paths(tmp_path)).set_preferred_easyrpg_runtime("0.8.1")
    page._sync_runtime_selection()

    model = page._runtime_row.get_model()
    assert model is not None
    names = [model.get_string(index) for index in range(model.get_n_items())]

    assert names[0] == "Default"
    assert page._runtime_row.get_selected() == 0
    assert page._selected_runtime_version() is None


def test_permission_switches_persist_to_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Permission flips write through to the stored entry."""
    page, library, _entry = _make_page(monkeypatch, tmp_path)

    page._network_switch.set_active(True)
    page._writes_switch.set_active(True)
    page._x11_switch.set_active(True)

    stored = library.load()[0]
    assert stored.allow_network is True
    assert stored.allow_game_writes is True
    assert stored.allow_x11 is True


def test_display_name_survives_reinspection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Re-inspection populates info rows but never overwrites free text."""
    page, _library, _entry = _make_page(monkeypatch, tmp_path)

    page._display_row.set_text("Custom")
    page._on_inspect_done(_make_inspection(tmp_path / "game", title="Detected Title"))

    assert page._display_row.get_text() == "Custom"
    assert page._info_title_row.get_subtitle() == "Detected Title"
    assert page._engine_row.get_subtitle() == EngineName.RPG_MAKER_MV.value


def test_easyrpg_hides_files_and_sdk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """EasyRPG games hide the SDK toggle and the extra-root-files group."""
    page, _library, _entry = _make_page(
        monkeypatch,
        tmp_path,
        lambda path: _make_inspection(path, EngineName.RPG_MAKER_2000_2003),
    )

    assert page._sdk_row.get_visible() is False
    assert page._files_group.get_visible() is False
    assert page._entrypoint_row.get_visible() is False
    assert page._plugins_row.get_visible() is False
    assert page._launch_button.get_sensitive() is True
    assert page._diagnose_button.get_sensitive() is True


def test_nwjs_shows_entrypoint_and_plugins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """NW.js games keep the entrypoint and plugins rows visible."""
    page, _library, _entry = _make_page(monkeypatch, tmp_path)

    assert page._entrypoint_row.get_visible() is True
    assert page._plugins_row.get_visible() is True


def test_launch_uses_persisted_permissions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Toggled permissions persist and reach the launch flags."""
    page, library, entry = _make_page(monkeypatch, tmp_path)
    page._entry = library.update(
        LibraryEntry(
            path=entry.path,
            display_name=entry.display_name,
            order=entry.order,
            preferred_runtime="0.99.0",
            preferred_sdk=True,
            copy_root_files=("extra.txt",),
        )
    )
    page._sdk_row.set_active(True)
    page._on_inspect_done(_make_inspection(tmp_path / "game", title="Demo"))
    calls: dict[str, Any] = {}

    def _fake_launch(
        paths: Any,
        repository: Any,
        game_path: Any,
        version: Any,
        sdk: Any,
        copy_root_files: Any = (),
        *,
        allow_network: bool = False,
        allow_game_writes: bool = False,
        x11: bool = False,
        interaction: Any = None,
    ) -> int:
        calls.update(
            {
                "version": version,
                "sdk": sdk,
                "copy_root_files": copy_root_files,
                "allow_network": allow_network,
                "allow_game_writes": allow_game_writes,
                "x11": x11,
                "interaction": interaction,
            }
        )
        return 0

    def _fake_run_launch(
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
    ) -> Any:
        try:
            code = _fake_launch(
                paths,
                repository,
                game_path,
                version,
                sdk,
                copy_root_files,
                allow_network=allow_network,
                allow_game_writes=allow_game_writes,
                x11=x11,
                interaction=interaction,
            )
        except BaseException as exc:
            on_error(exc)
        else:
            on_done(code)
        return None

    monkeypatch.setattr(
        sys.modules["box_gui.gtk.workers"], "run_launch", _fake_run_launch, raising=False
    )
    page._network_switch.set_active(True)
    page._writes_switch.set_active(False)
    page._x11_switch.set_active(True)

    page._on_launch_clicked(page._launch_button)

    assert calls["version"] == "0.99.0"
    assert calls["sdk"] is True
    assert calls["copy_root_files"] == ("extra.txt",)
    assert calls["allow_network"] is True
    assert calls["allow_game_writes"] is False
    assert calls["x11"] is True
    assert calls["interaction"].confirm_x11(":0") is True
    assert page._status.get_text() == ""
    assert page._status_box.get_visible() is False
    stored = library.load()[0]
    assert stored.allow_network is True
    assert stored.allow_game_writes is False
    assert stored.allow_x11 is True


def test_launch_runtime_error_offers_runtimes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A runtime launch failure offers Open Runtimes and opens the manager."""
    page, _library, _entry = _make_page(monkeypatch, tmp_path)
    presented = _capture_alerts(monkeypatch)
    opened: list[Any] = []
    page._on_open_runtimes = opened.append

    page._on_launch_error(BoxRuntimeError("no matching NW.js runtime is installed"))

    assert len(presented) == 1
    assert presented[0].get_heading() == "Launch Failed"
    assert presented[0].has_response("open-runtimes")
    presented[0].emit("response", "open-runtimes")
    assert opened == [page]


def test_launch_other_error_stays_close_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-runtime launch failure never opens the manager."""
    page, _library, _entry = _make_page(monkeypatch, tmp_path)
    presented = _capture_alerts(monkeypatch)
    opened: list[Any] = []
    page._on_open_runtimes = opened.append

    page._on_launch_error(BoxError("session failed"))

    assert len(presented) == 1
    assert presented[0].get_heading() == "Launch Failed"
    assert not presented[0].has_response("open-runtimes")
    presented[0].emit("response", "close")
    assert opened == []


def test_extra_files_add_and_remove_persist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Chip add/remove flows persist copy_root_files through update."""
    page, library, _entry = _make_page(monkeypatch, tmp_path)
    page._file_options = ("extra.txt", "other.txt")

    class _Dialog:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    dialog = _Dialog()
    first = Adw.SwitchRow(title="extra.txt")
    first.set_active(True)
    second = Adw.SwitchRow(title="other.txt")
    second.set_active(False)
    page._on_add_files_confirmed(None, dialog, {"extra.txt": first, "other.txt": second})

    assert dialog.closed is True
    assert library.load()[0].copy_root_files == ("extra.txt",)

    page._on_remove_file_clicked(None, "extra.txt")

    assert library.load()[0].copy_root_files == ()


def test_extra_files_chips_use_pill_style(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Each chip carries the pill background with a flat remove button."""
    from gi.repository import Gtk

    page, _library, _entry = _make_page(monkeypatch, tmp_path)
    page._entry = replace(page._entry, copy_root_files=("lng.txt",))
    page._rebuild_chips()

    chips: list[Any] = []
    child = page._chips.get_first_child()
    while child is not None:
        chip = child.get_child()
        if chip is not None:
            chips.append(chip)
        child = child.get_next_sibling()

    assert len(chips) == 1
    chip = chips[0]
    assert chip.get_halign() == Gtk.Align.START
    assert chip.get_spacing() == 6
    assert not chip.has_css_class("runtime-pill")
    pill = chip.get_first_child()
    assert pill is not None
    assert pill is chip.get_last_child()
    assert pill.has_css_class("runtime-pill")
    label = pill.get_first_child()
    assert isinstance(label, Gtk.Label)
    assert label.get_text() == "lng.txt"
    remove = pill.get_last_child()
    assert isinstance(remove, Gtk.Button)
    assert remove.has_css_class("flat")
    assert remove.has_css_class("chip-remove")
    assert page._chips.has_css_class("chip-flow")


def _capture_launch_version(monkeypatch: pytest.MonkeyPatch, page: Any) -> dict[str, Any]:
    """Stub run_launch and return the dict capturing its keyword arguments."""
    calls: dict[str, Any] = {}

    def _fake_run_launch(
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
    ) -> Any:
        calls.update(
            {
                "version": version,
                "sdk": sdk,
                "copy_root_files": copy_root_files,
            }
        )
        on_done(0)
        return None

    monkeypatch.setattr(
        sys.modules["box_gui.gtk.workers"], "run_launch", _fake_run_launch, raising=False
    )
    return calls


def test_launch_falls_back_to_easyrpg_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """EasyRPG launch uses the global when the entry has no runtime."""
    from box_gui.core.defaults import DefaultsRepository

    page, _library, _entry = _make_page(
        monkeypatch,
        tmp_path,
        lambda path: _make_inspection(path, EngineName.RPG_MAKER_2000_2003),
    )
    assert page._entry.preferred_runtime is None
    DefaultsRepository(page._paths).set_preferred_easyrpg_runtime("0.8.1")
    calls = _capture_launch_version(monkeypatch, page)

    page._on_launch_clicked(page._launch_button)

    assert calls["version"] == "0.8.1"
    assert calls["sdk"] is False
    assert calls["copy_root_files"] == ()


def test_launch_keeps_none_without_easyrpg_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without an EasyRPG global, launch keeps the previous None behavior."""
    page, _library, _entry = _make_page(
        monkeypatch,
        tmp_path,
        lambda path: _make_inspection(path, EngineName.RPG_MAKER_2000_2003),
    )
    assert page._entry.preferred_runtime is None
    calls = _capture_launch_version(monkeypatch, page)

    page._on_launch_clicked(page._launch_button)

    assert calls["version"] is None


def test_launch_keeps_explicit_runtime_over_easyrpg_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit EasyRPG runtime wins over the configured global."""
    from box_gui.core.defaults import DefaultsRepository

    page, library, entry = _make_page(
        monkeypatch,
        tmp_path,
        lambda path: _make_inspection(path, EngineName.RPG_MAKER_2000_2003),
    )
    page._entry = library.update(
        LibraryEntry(
            path=entry.path,
            display_name=entry.display_name,
            order=entry.order,
            preferred_runtime="0.7.0",
            preferred_sdk=False,
            copy_root_files=(),
        )
    )
    DefaultsRepository(page._paths).set_preferred_easyrpg_runtime("0.8.1")
    calls = _capture_launch_version(monkeypatch, page)

    page._on_launch_clicked(page._launch_button)

    assert calls["version"] == "0.7.0"


def test_launch_keeps_none_for_nwjs_with_easyrpg_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NW.js launch with None never falls back to the EasyRPG global."""
    from box_gui.core.defaults import DefaultsRepository

    page, _library, _entry = _make_page(monkeypatch, tmp_path)
    assert page._entry.preferred_runtime is None
    DefaultsRepository(page._paths).set_preferred_easyrpg_runtime("0.8.1")
    calls = _capture_launch_version(monkeypatch, page)

    page._on_launch_clicked(page._launch_button)

    assert calls["version"] is None


def _write_detail_icon(path: Path) -> Path:
    """Write a solid PNG fixture through the row pixbuf loader."""
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    path.parent.mkdir(parents=True, exist_ok=True)
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 32, 32)
    pixbuf.fill(0x445566FF)
    pixbuf.savev(str(path), "png", [], [])
    return path


def _slot_images(page: Any) -> list[Any]:
    """Collect the current Icon row preview images."""
    from gi.repository import Gtk

    images: list[Any] = []
    child = page._icon_slot.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Image):
            images.append(child)
        child = child.get_next_sibling()
    return images


def _set_detail_icon(page: Any, library: Any, entry: Any, icon: Path) -> Any:
    """Persist icon_path on the page entry and refresh its preview."""
    updated = library.update(replace(entry, icon_path=icon))
    page._entry = updated
    page._refresh_icon_preview()
    return updated


def test_detail_icon_row_shows_engine_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Icon row previews the engine icon without a cached icon."""
    page, _library, _entry = _make_page(monkeypatch, tmp_path)

    assert page._icon_row.get_title() == "Icon"
    assert page._change_icon_button.get_label() == "Change…"
    images = _slot_images(page)
    assert len(images) == 1
    assert images[0].get_icon_name() == "box-rpg-nwjs-symbolic"


def test_detail_icon_row_shows_cached_icon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The Icon row previews the cached icon_path file when the game has one."""
    from gi.repository import Gtk

    page, library, entry = _make_page(monkeypatch, tmp_path)
    _set_detail_icon(page, library, entry, _write_detail_icon(tmp_path / "cache" / "icon.png"))

    images = _slot_images(page)
    assert len(images) == 1
    assert images[0].get_storage_type() == Gtk.ImageType.PAINTABLE


def test_detail_change_icon_single_exe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Change with one executable offers it through the picker into the cache."""
    from gi.repository import Gtk

    import box_gui.pages.game_detail_page

    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    (game / "Game.exe").write_bytes(b"fake")
    offered: list[Any] = []
    written: list[Path] = []

    def _fake_present(parent: Any, exes: Any, on_chosen: Any) -> None:
        offered.append(tuple(exes))
        on_chosen(exes[0])

    def _fake_extract(exe: Any, dest: Any) -> bool:
        written.append(Path(dest))
        _write_detail_icon(Path(dest))
        return True

    monkeypatch.setattr(box_gui.pages.game_detail_page, "present_exe_picker", _fake_present)
    monkeypatch.setattr(box_gui.pages.game_detail_page, "extract_icon_png", _fake_extract)
    page, library, _entry = _make_page(monkeypatch, tmp_path)

    page._change_icon_button.emit("clicked")

    assert offered == [((game / "Game.exe"),)]
    assert len(written) == 1
    assert written[0].parent == page._paths.config_root / "icons"
    assert written[0].is_file()
    assert not (game / "icon.png").exists()
    assert {child.name for child in game.iterdir()} == {"Game.exe"}
    assert library.load()[0].icon_path == written[0]
    images = _slot_images(page)
    assert len(images) == 1
    assert images[0].get_storage_type() == Gtk.ImageType.PAINTABLE


def test_detail_change_icon_default_reverts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Picking the engine default clears icon_path and restores the fallback."""
    from gi.repository import Gtk

    import box_gui.pages.game_detail_page

    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    (game / "Game.exe").write_bytes(b"fake")

    def _fake_present(parent: Any, exes: Any, on_chosen: Any) -> None:
        on_chosen("default")

    def _no_extract(exe: Any, dest: Any) -> bool:  # pragma: no cover - guard
        raise AssertionError("must not extract for the default choice")

    monkeypatch.setattr(box_gui.pages.game_detail_page, "present_exe_picker", _fake_present)
    monkeypatch.setattr(box_gui.pages.game_detail_page, "extract_icon_png", _no_extract)
    page, library, entry = _make_page(monkeypatch, tmp_path)
    cached = _write_detail_icon(page._paths.config_root / "icons" / "cached.png")
    _set_detail_icon(page, library, entry, cached)
    assert _slot_images(page)[0].get_storage_type() == Gtk.ImageType.PAINTABLE

    page._change_icon_button.emit("clicked")

    assert not cached.exists()
    assert library.load()[0].icon_path is None
    assert {child.name for child in game.iterdir()} == {"Game.exe"}
    images = _slot_images(page)
    assert len(images) == 1
    assert images[0].get_icon_name() == "box-rpg-nwjs-symbolic"


def test_detail_change_icon_without_exes_but_icon_offers_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without executables but with a cached icon, Change still offers the default."""
    import box_gui.pages.game_detail_page

    offered: list[Any] = []

    def _fake_present(parent: Any, exes: Any, on_chosen: Any) -> None:
        offered.append(tuple(exes))
        on_chosen("default")

    monkeypatch.setattr(box_gui.pages.game_detail_page, "present_exe_picker", _fake_present)
    page, library, entry = _make_page(monkeypatch, tmp_path)
    cached = _write_detail_icon(tmp_path / "cache" / "icon.png")
    _set_detail_icon(page, library, entry, cached)

    page._change_icon_button.emit("clicked")

    assert offered == [()]
    assert not cached.exists()
    assert library.load()[0].icon_path is None


def test_detail_change_icon_asks_when_multiple_exes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Change with several executables extracts the picked one."""
    import box_gui.pages.game_detail_page

    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    for name in ("a.exe", "b.exe"):
        (game / name).write_bytes(b"fake")
    picked: list[Any] = []

    def _fake_present(parent: Any, exes: Any, on_chosen: Any) -> None:
        picked.append(tuple(exes))
        on_chosen(exes[0])

    def _fake_extract(exe: Any, dest: Any) -> bool:
        _write_detail_icon(Path(dest))
        return True

    monkeypatch.setattr(box_gui.pages.game_detail_page, "present_exe_picker", _fake_present)
    monkeypatch.setattr(box_gui.pages.game_detail_page, "extract_icon_png", _fake_extract)
    page, library, _entry = _make_page(monkeypatch, tmp_path)

    page._change_icon_button.emit("clicked")

    assert picked == [((game / "a.exe"), (game / "b.exe"))]
    cached = library.load()[0].icon_path
    assert cached is not None
    assert cached.parent == page._paths.config_root / "icons"
    assert cached.is_file()
    assert not (game / "icon.png").exists()


def test_detail_change_icon_without_exes_picks_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Change without executables falls back to the image picker."""
    import box_gui.pages.game_detail_page

    called: list[bool] = []
    monkeypatch.setattr(
        box_gui.pages.game_detail_page.GameDetailPage,
        "_pick_image_file",
        lambda self: called.append(True),
    )
    page, _library, _entry = _make_page(monkeypatch, tmp_path)

    page._change_icon_button.emit("clicked")

    assert called == [True]


def test_detail_image_choice_installs_icon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A picked image file becomes the previewed game icon."""
    from gi.repository import Gtk

    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    art = _write_detail_icon(tmp_path / "art.png")

    class _FakeFile:
        def get_path(self) -> str | None:
            return str(art)

    class _FakeDialog:
        def open_finish(self, _result: Any) -> Any:
            return _FakeFile()

    page, library, _entry = _make_page(monkeypatch, tmp_path)

    page._on_image_chosen(_FakeDialog(), None)

    cached = library.load()[0].icon_path
    assert cached is not None
    assert cached.parent == page._paths.config_root / "icons"
    assert cached.is_file()
    assert not (game / "icon.png").exists()
    assert {child.name for child in game.iterdir()} == set()
    images = _slot_images(page)
    assert len(images) == 1
    assert images[0].get_storage_type() == Gtk.ImageType.PAINTABLE


def test_detail_image_choice_rejects_non_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-image choice alerts and keeps the fallback preview."""
    notes = tmp_path / "notes.txt"
    notes.write_text("plain text")

    class _FakeFile:
        def get_path(self) -> str | None:
            return str(notes)

    class _FakeDialog:
        def open_finish(self, _result: Any) -> Any:
            return _FakeFile()

    page, _library, _entry = _make_page(monkeypatch, tmp_path)
    presented = _capture_alerts(monkeypatch)

    page._on_image_chosen(_FakeDialog(), None)

    assert len(presented) == 1
    assert presented[0].get_heading() == "Unexpected Error"
    images = _slot_images(page)
    assert len(images) == 1
    assert images[0].get_icon_name() == "box-rpg-nwjs-symbolic"


def test_detail_icon_pick_never_writes_into_game_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Picking an icon leaves the game directory byte-for-byte untouched."""
    import box_gui.pages.game_detail_page

    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    (game / "Game.exe").write_bytes(b"fake")
    before = {child.name for child in game.iterdir()}

    def _fake_present(parent: Any, exes: Any, on_chosen: Any) -> None:
        on_chosen(exes[0])

    written: list[Path] = []

    def _fake_extract(exe: Any, dest: Any) -> bool:
        written.append(Path(dest))
        _write_detail_icon(Path(dest))
        return True

    monkeypatch.setattr(box_gui.pages.game_detail_page, "present_exe_picker", _fake_present)
    monkeypatch.setattr(box_gui.pages.game_detail_page, "extract_icon_png", _fake_extract)
    page, library, _entry = _make_page(monkeypatch, tmp_path)

    page._change_icon_button.emit("clicked")

    assert {child.name for child in game.iterdir()} == before
    assert len(written) == 1
    assert written[0].parent == page._paths.config_root / "icons"
    assert library.load()[0].icon_path == written[0]


def test_detail_icon_cache_is_disjoint_from_root_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The extra-root-files picker never lists anything from the icon cache."""
    from box.api.launch import list_root_files

    import box_gui.pages.game_detail_page

    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    (game / "Game.exe").write_bytes(b"fake")
    (game / "config.ini").write_text("language=en")

    def _fake_present(parent: Any, exes: Any, on_chosen: Any) -> None:
        on_chosen(exes[0])

    def _fake_extract(exe: Any, dest: Any) -> bool:
        _write_detail_icon(Path(dest))
        return True

    monkeypatch.setattr(box_gui.pages.game_detail_page, "present_exe_picker", _fake_present)
    monkeypatch.setattr(box_gui.pages.game_detail_page, "extract_icon_png", _fake_extract)
    page, library, _entry = _make_page(monkeypatch, tmp_path)

    page._change_icon_button.emit("clicked")

    cached = library.load()[0].icon_path
    assert cached is not None
    assert cached.is_file()
    assert cached.parent == page._paths.config_root / "icons"
    assert page._inspection is not None
    root_names = list_root_files(page._inspection.game)
    assert cached.name not in root_names
    assert cached.name not in page._root_file_options(page._inspection.game)
    assert "config.ini" in page._root_file_options(page._inspection.game)


def test_launch_button_is_rocket_icon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The header launch action shows the rocket icon instead of text."""
    from gi.repository import Gtk

    page, _library, _entry = _make_page(monkeypatch, tmp_path)

    button = page._launch_button
    assert button.get_label() is None
    assert button.get_tooltip_text() == "Launch"
    assert button.has_css_class("suggested-action")
    icon = button.get_first_child()
    assert isinstance(icon, Gtk.Image)
    assert icon.get_icon_name() == "box-rpg-rocket-symbolic"


def test_permission_switches_init_from_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Permission switches reflect the persisted entry on open."""
    from box_gui.pages.game_detail_page import GameDetailPage

    page, library, entry = _make_page(monkeypatch, tmp_path)
    stored = library.update(
        LibraryEntry(
            path=entry.path,
            display_name=entry.display_name,
            order=entry.order,
            preferred_runtime=entry.preferred_runtime,
            preferred_sdk=entry.preferred_sdk,
            copy_root_files=entry.copy_root_files,
            engine=entry.engine,
            allow_network=True,
            allow_game_writes=False,
            allow_x11=True,
        )
    )
    reopened = GameDetailPage(
        entry=stored,
        paths=page._paths,
        repository=page._repository,
        library=library,
    )

    assert reopened._network_switch.get_active() is True
    assert reopened._writes_switch.get_active() is False
    assert reopened._x11_switch.get_active() is True


def test_edits_survive_reopen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Editing every option then reopening shows the persisted values."""
    from box_gui.pages.game_detail_page import GameDetailPage

    page, library, _entry = _make_page(monkeypatch, tmp_path)
    page._display_row.set_text("Renamed")
    page._network_switch.set_active(True)
    page._writes_switch.set_active(True)
    page._x11_switch.set_active(True)
    page._sdk_row.set_active(True)
    page._runtime_row.set_selected(1)

    reopened = GameDetailPage(
        entry=library.load()[0],
        paths=page._paths,
        repository=page._repository,
        library=library,
    )

    assert reopened._display_row.get_text() == "Renamed"
    assert reopened._network_switch.get_active() is True
    assert reopened._writes_switch.get_active() is True
    assert reopened._x11_switch.get_active() is True
    assert reopened._sdk_row.get_active() is True
    assert reopened._entry.preferred_runtime == "0.99.0"
    assert reopened._runtime_row.get_selected() == 1
    assert reopened._title_label.get_text() == "Renamed"
