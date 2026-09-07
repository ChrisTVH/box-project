# box-rpg manual

## Scope

`box-rpg` launches exported RPG Maker MV and MZ games with NW.js. It requires
Python 3.14+ and is not a launcher for RPG Maker XP, VX, VX Ace, or non-NW.js
applications.

## Configuration and storage

The default configuration file is
`$XDG_CONFIG_HOME/box-rpg/config.toml`, or `~/.config/box-rpg/config.toml` when
`XDG_CONFIG_HOME` is unset. Downloaded NW.js runtimes and disposable launcher
state live under `$XDG_CACHE_HOME/box-rpg`, or `~/.cache/box-rpg` when
`XDG_CACHE_HOME` is unset.

Add an allowed root before launching a game:

```sh
box-rpg config set allowed-game-root /home/alice/games
```

All game paths are resolved before launch and must remain below an allowed root.
Managed runtime path components are lower-case to keep deletion checks
deterministic across filesystems.

## Commands

```text
box-rpg
box-rpg inspect GAME_PATH
box-rpg runtime list
box-rpg runtime install VERSION [--architecture ARCHITECTURE] [--sdk]
box-rpg runtime remove VERSION [--architecture ARCHITECTURE] [--sdk]
box-rpg launch [GAME_PATH] [--runtime VERSION] [--sdk]
box-rpg config show
box-rpg config set KEY VALUE
box-rpg diagnose GAME_PATH [--runtime VERSION] [--sdk]
```

Run `box-rpg` from a game directory to launch it directly. `launch` also uses
the current directory when `GAME_PATH` is omitted. Use `inspect` before launch
to check that a directory is an MV/MZ export. `runtime install` fetches a
requested NW.js version into the cache; `list` shows managed versions; `remove`
only removes a managed cached version. `config set` updates one TOML value, for
example `box-rpg config set preferred-runtime 0.90.0`. `diagnose` is local-only
and does not transmit game data.

Use `--architecture` only with a supported NW.js identifier: `x64`, `ia32`,
`arm64`, or `arm`. `--sdk` selects an NW.js SDK build instead of a standard
runtime.

## Security

Treat games and NW.js archives as untrusted software. Restrict allowed roots to
directories you control, review configured download sources, and avoid running
the launcher with elevated privileges. The launcher must reject symlink or path
traversal escapes outside allowed roots and must never delete paths outside its
own cache.
