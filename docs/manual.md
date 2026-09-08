# box-rpg manual

## Scope

`box-rpg` launches exported RPG Maker MV/MZ games with NW.js and RPG Maker
2000/2003 projects with EasyRPG Player. It requires Python 3.14+ and is not a
launcher for RPG Maker XP, VX, VX Ace, or other runtimes.

## Configuration and storage

The default configuration file is
`$XDG_CONFIG_HOME/box-rpg/config.toml`, or `~/.config/box-rpg/config.toml` when
`XDG_CONFIG_HOME` is unset. Downloaded NW.js runtimes and disposable launcher
state live under `$XDG_CACHE_HOME/box-rpg`, or `~/.cache/box-rpg` when
`XDG_CACHE_HOME` is unset.

To authorize an entire game library instead of individual games, add its root:

```sh
box-rpg config set allowed-game-root /home/alice/games
```

When a game is outside configured roots, an interactive box-rpg launch asks before
registering its exact root. Non-interactive launches remain restricted to configured
roots. Add a library root explicitly to authorize more than one game below it.

All game paths are resolved before launch and must remain below an allowed root.
Managed runtime path components are lower-case to keep deletion checks
deterministic across filesystems.

## Commands

```text
box-rpg
box-rpg cleanup
box-rpg inspect GAME_PATH
box-rpg runtime list
box-rpg runtime available [--page PAGE] [--architecture ARCHITECTURE] [--sdk]
box-rpg runtime available --interactive [--page PAGE] [--architecture ARCHITECTURE] [--sdk]
box-rpg runtime install VERSION [--architecture ARCHITECTURE] [--sdk]
box-rpg runtime remove VERSION [--architecture ARCHITECTURE] [--sdk]
box-rpg runtime easyrpg list
box-rpg runtime easyrpg available [--page PAGE]
box-rpg runtime easyrpg available --interactive [--page PAGE]
box-rpg runtime easyrpg install VERSION
box-rpg runtime easyrpg remove VERSION
box-rpg launch [GAME_PATH] [--runtime VERSION] [--sdk]
box-rpg config show
box-rpg config set KEY VALUE
box-rpg diagnose GAME_PATH [--runtime VERSION] [--sdk]
```

Run `box-rpg` from a game directory to launch it directly. `launch` also uses
the current directory when `GAME_PATH` is omitted. Use `inspect` before launch
to check that a directory is a supported game. `runtime install` fetches a
requested NW.js version into the cache; `runtime easyrpg install` fetches an
EasyRPG Player version. Each provider can list and remove only its managed
cached versions. `config set` updates one TOML value, for example
`box-rpg config set preferred-runtime 0.90.0`. `diagnose` is local-only and
does not transmit game data.

`cleanup` requires an interactive terminal. It presents paginated lists of
authorized roots, managed runtimes, and cached runtime archives. Removing a root
only removes its authorization; it never deletes games. Its global cleanup
requires typing `DELETE ALL` and does not remove `config.toml`, sessions, or reports.

Running `box-rpg` without arguments requires the current directory to contain a
supported game. Otherwise it prints help and explains how to launch one.

`runtime available` reads the official stable NW.js version index in pages of five. With
`--interactive`, enter a number to choose a version, `n` for the next page, `p`
for the previous page, or `q` to quit. A selected version is installed only after
confirmation. The architecture is detected automatically unless overridden.
Runtime downloads use a 60-second connection timeout and resume partial archives after
temporary connection failures. Interactive terminals display a progress bar. The
runtime commands manage both NW.js and EasyRPG Player archives.

RPG Maker 2000/2003 projects are detected from `RPG_RT.ini`, `RPG_RT.ldb`, and
`RPG_RT.lmt`. They require a managed x64 EasyRPG Player runtime and launch with
`--project-path` and `--fullscreen`; saves remain in the game directory.

At launch, box-rpg selects NW.js Wayland only when both `XDG_SESSION_TYPE=wayland`
and `WAYLAND_DISPLAY` are present; otherwise it uses X11.

Use `--architecture` only with a supported NW.js identifier: `x64`, `ia32`,
`arm64`, or `arm`. `--sdk` selects an NW.js SDK build instead of a standard
runtime.

## Security

Treat games and NW.js archives as untrusted software. Restrict allowed roots to
directories you control, review configured download sources, and avoid running
the launcher with elevated privileges. The launcher must reject symlink or path
traversal escapes outside allowed roots and must never delete paths outside its
own cache.
