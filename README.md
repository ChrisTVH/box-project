# box-rpg

`box-rpg` is a Python 3.14+ launcher for **RPG Maker MV/MZ** games running on
**NW.js**. It intentionally does not support other RPG Maker generations or
other web runtimes.

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

The installer verifies Python 3.14+ and an Arch Linux or Arch-based system. It
requires no elevated privileges. Use `box-rpg --help` after installation.

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
box-rpg inspect GAME_PATH
box-rpg runtime list
box-rpg runtime install VERSION [--architecture ARCHITECTURE] [--sdk]
box-rpg runtime remove VERSION [--architecture ARCHITECTURE] [--sdk]
box-rpg launch [GAME_PATH] [--runtime VERSION] [--sdk]
box-rpg config show
box-rpg config set KEY VALUE
box-rpg diagnose GAME_PATH [--runtime VERSION] [--sdk]
```

Run `box-rpg` from a game directory to launch that directory. `inspect`
identifies a supported MV/MZ export. `runtime` manages downloaded NW.js
versions. `launch` starts an allowed game and defaults to the current directory
when `GAME_PATH` is omitted. `config` displays or changes the launcher settings,
and `diagnose` creates a local report without launching the game.

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
