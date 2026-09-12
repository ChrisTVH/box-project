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

Add `--force-reinstall` to reinstall even when the installed version matches
the checkout (plain `pip install` would otherwise report the requirement as
satisfied and change nothing). On externally managed Pythons add the separate
`--break-system-packages` consent for user-site installation.

To uninstall box-rpg and its shell completions:

```sh
./install.py --uninstall
./install.py --uninstall --yes
```

The installer verifies Linux, Python 3.14+, pip, Bubblewrap (`/usr/bin/bwrap`
with working user namespaces), and GnuPG (`/usr/bin/gpg`), and rejects root
execution. It shows a short plan by default; pass `--verbose` to see the exact
commands.
Overriding an externally managed Python environment requires separate consent;
`--yes` does not grant it. Installing overwrites the managed completions with
the current copies, and uninstallation requires a package in the base
interpreter's user-site.
Use `box-rpg --help` after installation.

Launching games requires `/usr/bin/bwrap` (Bubblewrap), enabled user namespaces,
and a local Wayland session. There is no unsandboxed fallback. On X11 the
launcher warns and asks for explicit confirmation before exposing only the local
X display socket; without a terminal it refuses. NW.js
signature verification additionally requires `/usr/bin/gpg`. The installer does
not install these system tools. See the manual for current sandbox limitations.

## Development

Follow [locked development and builds](docs/development.md) to create `.venv`
and install the pinned bootstrap, build, and development requirements with hashes.
The installer builds in a private temporary environment using the same build lock;
plain `pip install .` does not provide that guarantee.

Preview recursive cleanup of ignored tool caches, environments, build outputs,
egg-info, and bytecode:

```sh
./cleaner.py
```

Apply the cleanup after reviewing the listed paths:

```sh
./cleaner.py --apply
./cleaner.py --yes
```

Tracked files and nested projects are preserved. Deleted environments can be
recreated from the locked requirements in `docs/development.md`.

## Commands

The command interface is intentionally small:

```text
box-rpg
box-rpg cleanup all [--yes]
box-rpg cleanup --interactive
box-rpg cleanup list [roots|runtimes|downloads|profiles]
box-rpg cleanup remove CATEGORY SELECTOR [--yes]
box-rpg cleanup remove CATEGORY --all [--yes]
box-rpg inspect GAME_PATH
box-rpg runtime nwjs list
box-rpg runtime nwjs available [--page PAGE] [--architecture ARCHITECTURE] [--sdk]
box-rpg runtime nwjs available --interactive [--page PAGE] [--architecture ARCHITECTURE] [--sdk]
box-rpg runtime nwjs install VERSION [--architecture ARCHITECTURE] [--sdk]
box-rpg runtime nwjs remove VERSION [--architecture ARCHITECTURE] [--sdk]
box-rpg runtime easyrpg list
box-rpg runtime easyrpg available [--page PAGE]
box-rpg runtime easyrpg available --interactive [--page PAGE]
box-rpg runtime easyrpg install VERSION
box-rpg runtime easyrpg remove VERSION
box-rpg launch [GAME_PATH] [--runtime VERSION] [--sdk] [--copy-root-file FILE] [--allow-network] [--allow-game-writes] [--x11]
box-rpg config show
box-rpg config set KEY VALUE
box-rpg diagnose GAME_PATH [--runtime VERSION] [--sdk]
```

Run `box-rpg` from a game directory to launch that directory. `inspect`
identifies a supported game. `runtime` manages downloaded NW.js and EasyRPG
Player versions. `launch` starts an allowed game and defaults to the current
directory when `GAME_PATH` is omitted. `config` displays or changes the launcher
settings, and `diagnose` creates a local report without launching the game.
Diagnosis does execute the selected runtime with `--version`; use trusted runtimes.

`cleanup all` shows the global cleanup scope and requires `DELETE ALL`. Add
`--yes` for immediate noninteractive deletion. Use
`cleanup --interactive` to open the menu; it cannot be combined with `--yes`.
`cleanup list` emits JSON Lines records with safe selectors for roots, runtimes,
downloads, or profiles.
`cleanup remove CATEGORY SELECTOR` removes one item, while `--all` removes a
category. Removal prompts unless `--yes` is supplied; without a terminal,
`--yes` is required. Cleanup never deletes game directories, `config.toml`,
sessions, or reports.

Running `box-rpg` without arguments requires the current directory to contain a
supported game; otherwise it prints help and explains how to launch one.

`runtime nwjs available` queries the official stable NW.js version index in pages
of ten. Add `--interactive` to browse pages, choose a version, and confirm its
installation. Transient list failures offer a retry instead of aborting. The architecture is detected automatically unless `--architecture`
is supplied. Runtime downloads use a 60-second network timeout and restart from
zero after temporary failures instead of combining partial representations.
Interactive terminals display a progress bar. Downloads and extraction enforce
resource limits; concurrent operations on the same runtime fail with a busy error.
Launches require an owned Wayland socket selected by `WAYLAND_DISPLAY` under
`XDG_RUNTIME_DIR`. Networking is disabled unless `launch --allow-network` is
explicitly supplied for that run. This grants host network access, including
local services, but never grants write access to game assets. Games that update
themselves need `launch --allow-game-writes` for that run; it mounts the game
directory writable, so use it only with games you trust. GPU (`/dev/dri`) and
the user PipeWire and PulseAudio sockets are exposed after validation; all widen the
sandbox and are documented in the manual.

RPG Maker 2000/2003 projects require `RPG_RT.ini`, `RPG_RT.ldb`, and `RPG_RT.lmt`.
They launch with the managed x64 EasyRPG Player using `--project-path` and `--fullscreen`.
Use `box-rpg runtime easyrpg available --interactive` to choose and install a version.
EasyRPG saves to `<game>/save/` via `--save-path`. Copy existing `Save01.lsd`,
`Save02.lsd`, etc. from the game root into that directory to continue old saves;
the launcher does not move them automatically.

Some NW.js exports need auxiliary files from the game root. The supported
workaround is to copy a direct regular file into the isolated launch session. For
example, use
`box-rpg launch --copy-root-file game_messages.csv`. The option can be repeated
for multiple direct files and rejects paths containing directories, symlinks, or
session-owned names.

Each NW.js game has a persistent private Chromium/NW.js profile at
`$XDG_CACHE_HOME/box-rpg/profiles/<opaque-game-id>/sandbox` (or the corresponding
path under `~/.cache`). It stores browser preferences,
web storage, and cache. RPG Maker saves stay in `save/` beside the entrypoint
(usually `<game>/www/save/` or `<game>/save/`) inside the game directory.
Use `box-rpg cleanup remove profiles SELECTOR` to remove one profile
or `box-rpg cleanup remove profiles --all` to remove every profile.

See [the manual](docs/manual.md) and
[the example configuration](res/config/box-rpg.toml.example).

## Safety and path rules

`box-rpg` validates game structure, resolves symlinks before use, and only
launches games below configured `allowed_game_roots` (add a library root
explicitly to authorize more than one game). Games and version probes run in a
mandatory Bubblewrap sandbox, and NW.js archives are checked against upstream
signed checksums when available. See [security and compatibility
limits](docs/security.md) for the trust model and its limits.
