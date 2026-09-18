"""RowAvailability controller tests with stubbed toolkit, no network."""

# pyright: reportMissingImports=false

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")

    import box_gui.widgets.availability as availability_module
    from box_gui.gtk.icons import WARNING_ICON_NAME
    from box_gui.widgets.availability import RowAvailability

    _availability_available = True
except Exception:
    availability_module: Any = None
    WARNING_ICON_NAME: Any = None  # pyright: ignore[reportConstantRedefinition]
    RowAvailability: Any = None
    _availability_available = False

pytestmark = pytest.mark.skipif(not _availability_available, reason="gi/Adw unavailable")


class _FakeImage:
    """Record tooltip updates without needing a display."""

    def __init__(self, icon_name: str = "") -> None:
        """Remember the icon name used at construction."""
        self._icon_name = icon_name
        self._tooltip: str | None = None

    @classmethod
    def new_from_icon_name(cls, icon_name: str) -> _FakeImage:
        """Build one fake warning image for the requested icon name."""
        return cls(icon_name)

    def set_tooltip_text(self, text: str | None) -> None:
        """Record the latest tooltip text."""
        self._tooltip = text

    def get_tooltip_text(self) -> str | None:
        """Return the latest tooltip text."""
        return self._tooltip

    def get_icon_name(self) -> str:
        """Return the icon name used at construction."""
        return self._icon_name


class _FakeRow:
    """Mimic Adw.ActionRow suffix and sensitivity handling."""

    def __init__(self) -> None:
        """Start sensitive with no suffixes."""
        self._suffixes: list[Any] = []
        self._sensitive = True

    def add_suffix(self, widget: Any) -> None:
        """Record one suffix widget."""
        self._suffixes.append(widget)

    def remove(self, widget: Any) -> None:
        """Drop one previously added suffix widget."""
        self._suffixes.remove(widget)

    def set_sensitive(self, sensitive: bool) -> None:
        """Record the sensitivity flag."""
        self._sensitive = sensitive

    def get_sensitive(self) -> bool:
        """Return the sensitivity flag."""
        return self._sensitive

    def suffix_count(self) -> int:
        """Return how many suffix widgets are attached."""
        return len(self._suffixes)


class _FakeSwitchRow(_FakeRow):
    """Mimic Adw.SwitchRow with an active toggle."""

    def __init__(self, active: bool = False) -> None:
        """Start with the requested active state."""
        super().__init__()
        self._active = active

    def set_active(self, active: bool) -> None:
        """Record the toggle state."""
        self._active = active

    def get_active(self) -> bool:
        """Return the toggle state."""
        return self._active


def _install_fake_toolkit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub Adw/Gtk in the availability module so tests run headless."""
    fake_adw = SimpleNamespace(ActionRow=_FakeRow, SwitchRow=_FakeSwitchRow)
    fake_gtk = SimpleNamespace(Image=_FakeImage)
    monkeypatch.setattr(availability_module, "Adw", fake_adw)
    monkeypatch.setattr(availability_module, "Gtk", fake_gtk)


def test_mark_unavailable_attaches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first mark creates the icon and grays the row."""
    _install_fake_toolkit(monkeypatch)
    row: Any = _FakeSwitchRow()
    indicator = RowAvailability(row)

    indicator.mark_unavailable("first reason")

    assert row.get_sensitive() is False
    assert indicator.warning is not None
    assert indicator.is_unavailable is True
    assert indicator.warning.get_icon_name() == WARNING_ICON_NAME
    assert indicator.warning.get_tooltip_text() == "first reason"
    assert row.suffix_count() == 1


def test_mark_unavailable_updates_reason_without_duplicating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second mark refreshes the tooltip without stacking icons."""
    _install_fake_toolkit(monkeypatch)
    row: Any = _FakeRow()
    indicator = RowAvailability(row)

    indicator.mark_unavailable("generic reason")
    first = indicator.warning
    indicator.mark_unavailable("EasyRPG reason")

    assert indicator.warning is first
    assert indicator.warning is not None
    assert indicator.warning.get_tooltip_text() == "EasyRPG reason"
    assert row.suffix_count() == 1
    assert row.get_sensitive() is False


def test_mark_available_detaches_and_enables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clearing removes the icon and re-enables the row."""
    _install_fake_toolkit(monkeypatch)
    row: Any = _FakeRow()
    indicator = RowAvailability(row)
    indicator.mark_unavailable("reason")

    indicator.mark_available()

    assert indicator.warning is None
    assert indicator.is_unavailable is False
    assert row.get_sensitive() is True
    assert row.suffix_count() == 0

    indicator.mark_available()

    assert indicator.warning is None
    assert row.get_sensitive() is True
    assert row.suffix_count() == 0


def test_disable_switch_forces_off_with_guard_and_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling forces the toggle off under the loading guard."""
    _install_fake_toolkit(monkeypatch)
    row: Any = _FakeSwitchRow(active=True)
    indicator = RowAvailability(row)
    loading: list[bool] = []
    persisted: list[bool] = []

    def _set_loading(value: bool) -> None:
        loading.append(value)

    def _persist_disabled() -> None:
        persisted.append(True)

    indicator.disable_switch("unavailable", _set_loading, _persist_disabled)

    assert row.get_active() is False
    assert loading == [True, False]
    assert persisted == [True]
    assert row.get_sensitive() is False
    assert indicator.warning is not None
    assert indicator.warning.get_tooltip_text() == "unavailable"


def test_enable_switch_restores_persisted_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabling clears the warning and restores the persisted toggle."""
    _install_fake_toolkit(monkeypatch)
    row: Any = _FakeSwitchRow(active=False)
    indicator = RowAvailability(row)
    indicator.mark_unavailable("reason")
    loading: list[bool] = []

    def _set_loading(value: bool) -> None:
        loading.append(value)

    indicator.enable_switch(True, _set_loading)

    assert indicator.warning is None
    assert row.get_sensitive() is True
    assert row.get_active() is True
    assert loading == [True, False]


def test_enable_switch_skips_guard_when_already_correct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No programmatic toggle happens when the switch already matches."""
    _install_fake_toolkit(monkeypatch)
    row: Any = _FakeSwitchRow(active=False)
    indicator = RowAvailability(row)
    loading: list[bool] = []

    def _set_loading(value: bool) -> None:
        loading.append(value)

    indicator.enable_switch(False, _set_loading)

    assert loading == []
    assert row.get_active() is False
    assert row.get_sensitive() is True
