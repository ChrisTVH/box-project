# box-rpg-maker

GTK4/libadwaita front end for the `box-rpg` launcher (command `box-rpg-maker`, App ID `io.gitlab.christvh.BoxRpgApp`). Add a game folder with `+`, open it, and press launch. Runtime choice and sandbox permissions live on the game detail page.

Supported games, and nothing else by decision:

- RPG Maker MV/MZ exports, played with NW.js.
- RPG Maker 2000/2003 projects, played with EasyRPG Player.

The GUI drives the stable `box.api` backend surface and never reimplements it: game inspection, runtime management, and sandboxed launching all run through the installed `box-rpg` backend, with per-launch consent for X11, network, and game writes.

This is one distribution of the `box` monorepo. Start with the [root README](../README.md), then read the frontend guides:

- [Frontend development](../docs/box-gui/development.md): layout, conventions, and checks.
- [Frontend translations](../docs/box-gui/translations.md): string workflow and catalog rules.
- [Backend user guide](../docs/box-rpg/manual.md) and [security limits](../docs/box-rpg/security.md) for launcher behavior.
