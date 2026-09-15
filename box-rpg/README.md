# box-rpg

Safe command-line launcher for RPG Maker games on Linux (command `box-rpg`). Each game runs isolated in its own Bubblewrap sandbox; there is no unsandboxed mode.

Supported games, and nothing else by decision:

- RPG Maker MV/MZ exports, played with NW.js.
- RPG Maker 2000/2003 projects, played with EasyRPG Player.

`box-rpg` inspects a game directory, installs the matching runtime, and launches the game with explicit per-launch consent for network access, game-file writes, and X11. Game directories outside the allowed roots need one authorization each.

This is one distribution of the `box` monorepo. Start with the [root README](../README.md), then read the backend guides:

- [User guide](../docs/box-rpg/manual.md): launch games, install runtimes, manage stored data.
- [Security and compatibility limits](../docs/box-rpg/security.md): trust model and sandbox limits.
- [Public API](../docs/box-rpg/api.md): stable backend surface shared by the CLI and the GUI.
- [Backend development](../docs/box-rpg/development.md): locked environment, builds, and checks.
