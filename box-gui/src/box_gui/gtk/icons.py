"""Bundled interface icons used by box_gui widgets.

UI icons (`box-rpg-plus-symbolic`, `box-rpg-settings-symbolic`,
`box-rpg-dots-symbolic`, `box-rpg-rocket-symbolic`,
`box-rpg-rocket-off-symbolic`, `box-rpg-x-symbolic`, `box-rpg-trash-symbolic`, `box-rpg-box-symbolic`,
`box-rpg-warning-symbolic`, `box-rpg-folder-symbolic`)
and the engine tabs below
are Tabler Icons (Copyright (c) Tabler, MIT license), vendored from
@tabler/icons 3.34.1 into `res/icons/`. Only the application icon stays
Fluent UI.

The `-symbolic` suffix is required: GTK only recolors icons loaded by
name through `Gtk.IconTheme` when the name ends in `-symbolic`, which is
what makes these outline icons follow the light/dark foreground
automatically. No manual theme detection is needed.

Two further constraints come from the symbolic pipeline, verified
empirically: it only paints fills (strokes are dropped), so the Tabler
strokes were expanded to filled outlines with `fill="#2e3436"` (the
Adwaita symbolic convention); and lookup of `X-symbolic` falls back to
a plain `X` icon, so no stale `box-rpg-*.svg` copies without the suffix
may remain in hicolor or they shadow the themed icons.
"""

from __future__ import annotations

__all__ = [
    "APP_ICON_NAME",
    "BOX_ICON_NAME",
    "EASYRPG_ICON_NAME",
    "FOLDER_ICON_NAME",
    "NWJS_ICON_NAME",
    "WARNING_ICON_NAME",
]


APP_ICON_NAME: str = "io.gitlab.christvh.BoxRpgApp"
"""Themed icon name for the application windows.

Resolves once `res/icons/io.gitlab.christvh.BoxRpgApp.svg` (aliased as
`res/icons/box-rpg-app.svg`) is installed as a hicolor icon; harmless
fallback to the default window icon until then.
"""

NWJS_ICON_NAME: str = "box-rpg-nwjs-symbolic"
"""Themed icon name for the NW.js engine tab (Tabler "world" in `res/icons/`)."""

EASYRPG_ICON_NAME: str = "box-rpg-easyrpg-symbolic"
"""Themed icon name for the EasyRPG engine tab (Tabler "device-gamepad" in `res/icons/`)."""

BOX_ICON_NAME: str = "box-rpg-box-symbolic"
"""Fallback icon for games with an unknown engine (Tabler "box" in `res/icons/`)."""

WARNING_ICON_NAME: str = "box-rpg-warning-symbolic"
"""Themed icon name for unavailable-feature warnings (Tabler "alert-triangle")."""

FOLDER_ICON_NAME: str = "box-rpg-folder-symbolic"
"""Themed icon name for ghost relocate buttons (Tabler "folder")."""
