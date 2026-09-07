# box-rpg

`box-rpg` is a Python 3.14+ launcher for **RPG Maker MV/MZ** games running on
**NW.js**. It intentionally does not support other RPG Maker generations or
other web runtimes.

## Install

Install a released package for the current user:

```sh
python3.14 -m pip install --user box-rpg
```

Ensure Python's user scripts directory is in `PATH`, then verify the command:

```sh
box-rpg --help
```

For a source checkout, use a Python 3.14+ virtual environment and install the
project with its documented development dependencies when they are available.

## Commands

The command interface is intentionally small:

```text
box-rpg inspect GAME_PATH
box-rpg runtime list
box-rpg runtime install VERSION [--architecture ARCHITECTURE] [--sdk]
box-rpg runtime remove VERSION [--architecture ARCHITECTURE] [--sdk]
box-rpg launch GAME_PATH [--runtime VERSION] [--sdk]
box-rpg config show
box-rpg config set KEY VALUE
box-rpg diagnose GAME_PATH [--runtime VERSION] [--sdk]
```

`inspect` identifies a supported MV/MZ export. `runtime` manages downloaded
NW.js versions. `launch` starts an allowed game. `config` displays or changes
the launcher settings, and `diagnose` creates a local report without launching
the game.

See [the manual](docs/manual.md) and
[the example configuration](res/config/box-rpg.toml.example).

## Safety and path rules

Game paths are never trusted merely because they contain `package.json`.
`box-rpg` validates the game structure, resolves paths before use, and only
launches games below configured `allowed_game_roots`. Runtime removal is limited to
runtimes managed inside the configured cache; it must not remove arbitrary
paths.

Managed runtime paths use lower-case components. Games are resolved before they
are launched, including symlinks, and must remain below an allowed root.

NW.js executes game JavaScript with the permissions available to your user.
Only launch games and install runtime downloads from sources you trust.
