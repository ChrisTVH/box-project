"""Unit tests for display-only path abbreviation (no GTK dependency)."""

# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

from box_gui.core.display import PATH_DISPLAY_WIDTH, abbreviate_display_path


def test_full_path_when_it_fits(tmp_path: Path) -> None:
    """Short paths render in full with the home collapsed to ~."""
    home = tmp_path / "home"
    game = home / "Juegos" / "Demo"
    assert abbreviate_display_path(game, 80, home) == "~/Juegos/Demo"


def test_initials_then_ellipsis_ladder(tmp_path: Path) -> None:
    """Narrow widths walk the same ladder as the backend helper."""
    home = tmp_path / "home"
    game = home / "Documentos" / "Juegos" / "Linux" / "The Demon King's Reclusive Strategist"
    assert abbreviate_display_path(game, 80, home) == (
        "~/Documentos/Juegos/Linux/The Demon King's Reclusive Strategist"
    )
    assert (
        abbreviate_display_path(game, 45, home) == "~/D/J/L/The Demon King's Reclusive Strategist"
    )
    assert abbreviate_display_path(game, 44, home) == "…D/J/L/The Demon King's Reclusive Strategist"
    assert abbreviate_display_path(game, 43, home) == "…/J/L/The Demon King's Reclusive Strategist"
    assert abbreviate_display_path(game, 41, home) == "…/L/The Demon King's Reclusive Strategist"
    assert abbreviate_display_path(game, 39, home) == "…/The Demon King's Reclusive Strategist"
    assert abbreviate_display_path(game, 30, home) == "…/The Demon King's Reclusive S"
    assert abbreviate_display_path(home, 80, home) == "~"


def test_outside_home_uses_absolute_ladder(tmp_path: Path) -> None:
    """Paths outside home keep the absolute ladder without ~/."""
    home = tmp_path / "home"
    game = tmp_path / "mnt" / "games" / "Xyz Quest"
    assert abbreviate_display_path(game, 400, home) == game.as_posix()
    assert abbreviate_display_path(game, 12, home) == "…/Xyz Quest"


def test_relative_paths_pass_through() -> None:
    """Relative entrypoints render unchanged instead of gaining a slash."""
    assert abbreviate_display_path(Path("www/index.html")) == "www/index.html"


def test_default_budget_and_home(tmp_path: Path) -> None:
    """Defaults use the shared width constant and the real home."""
    home = tmp_path / "home"
    game = home / "Documentos" / "Juegos" / "Linux" / "Yarisutemesubuta"
    assert PATH_DISPLAY_WIDTH == 48
    assert abbreviate_display_path(game, home=home) == "~/Documentos/Juegos/Linux/Yarisutemesubuta"
    assert abbreviate_display_path(game, 30, home) == "~/D/J/L/Yarisutemesubuta"
