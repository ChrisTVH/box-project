# box-rpg

`box-rpg` is a Python 3.14+ launcher for **RPG Maker MV/MZ** games running on
**NW.js** and **RPG Maker 2000/2003** projects running on **EasyRPG Player**.
It intentionally does not support other RPG Maker generations or runtimes.

## Install and uninstall

Install the current checkout for the user. The installer performs a dry run by
default; use `--yes` to skip confirmation:

```sh
./install.py --install
./install.py --install --yes
```

To uninstall box-rpg and its shell completions:

```sh
./install.py --uninstall
./install.py --uninstall --yes
```

The installer verifies Linux, Python 3.14+, and pip. It requires no elevated
privileges. Use `box-rpg --help` after installation.

## Development

Create the local virtual environment and install development dependencies:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Clean Python caches, virtual environments, and build artifacts with a dry run:

```sh
./cleaner.py
```

Apply the cleanup after reviewing the listed paths:

```sh
./cleaner.py --apply
./cleaner.py --yes
```

## Commands

The command interface is intentionally small:

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

Run `box-rpg` from a game directory to launch that directory. `inspect`
identifies a supported game. `runtime` manages downloaded NW.js and EasyRPG
Player versions. `launch` starts an allowed game and defaults to the current
directory when `GAME_PATH` is omitted. `config` displays or changes the launcher
settings, and `diagnose` creates a local report without launching the game.

`cleanup` requires an interactive terminal. It can remove individual or all
authorized roots, managed runtimes, and cached runtime archives; it never deletes
game directories, `config.toml`, sessions, or reports.

Running `box-rpg` without arguments requires the current directory to contain a
supported game; otherwise it prints help and explains how to launch one.

`runtime available` queries the official stable NW.js version index in pages of five. Add
`--interactive` to browse pages, choose a version, and confirm its installation.
The architecture is detected automatically unless `--architecture` is supplied.
Runtime downloads use a 60-second connection timeout and resume partial archives after
temporary connection failures. Interactive terminals display a progress bar.
Launches select Wayland only when the session exposes `WAYLAND_DISPLAY`; otherwise they use X11.

RPG Maker 2000/2003 projects require `RPG_RT.ini`, `RPG_RT.ldb`, and `RPG_RT.lmt`.
They launch with the managed x64 EasyRPG Player using `--project-path` and `--fullscreen`.
Use `box-rpg runtime easyrpg available --interactive` to choose and install a version.

See [the manual](docs/manual.md) and
[the example configuration](res/config/box-rpg.toml.example).

## Safety and path rules

Game paths are never trusted merely because they contain `package.json`.
`box-rpg` validates the game structure and resolves paths before use. It only
launches games below configured `allowed_game_roots`; in an interactive terminal,
it asks before storing an unregistered game's exact validated root. Runtime removal
is limited to runtimes managed inside the configured cache; it must not remove
arbitrary paths.

Add a library root explicitly to authorize more than one game below it.

Managed runtime paths use lower-case components. Games are resolved before they
are launched, including symlinks, and must remain below an allowed root.

NW.js executes game JavaScript with the permissions available to your user.
Only launch games and install runtime downloads from sources you trust.
