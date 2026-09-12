# box-rpg user guide

`box-rpg` launches RPG Maker MV/MZ exports with NW.js and RPG Maker 2000/2003
projects with EasyRPG Player. It requires Python 3.14+. RPG Maker XP, VX, and
VX Ace are not supported.

## Launch a game

Open a terminal in the game directory and run:

```sh
box-rpg
```

The launcher detects the game type. On the first interactive launch of a game
outside your configured libraries, confirm the prompt to authorize that exact
game directory. You can also launch a game from elsewhere:

```sh
box-rpg launch /path/to/game
```

Use `box-rpg inspect /path/to/game` to check detection without launching it.

NW.js games always start fullscreen; there is no windowed mode.

Bubblewrap at `/usr/bin/bwrap`, enabled user namespaces, and a local Wayland
socket are required. The launcher refuses unsandboxed execution. Runtime
version probes also use the sandbox, without desktop
or network access.

On an X11 session the launcher prints a security warning and asks for explicit
confirmation before exposing only the local X display socket; without a
terminal it refuses. X11 clients can observe input and screen contents, so
Wayland remains the recommended session. `diagnose` never uses X11.

To select the X11 display explicitly for one launch, without any prompt:

```sh
box-rpg launch /path/to/game --x11
```

The flag itself is the consent; an unusable display still fails instead of
falling back. The permission is not saved.

To grant network access for one launch:

```sh
box-rpg launch /path/to/game --allow-network
```

The permission is not saved. It shares the host network, including Internet,
LAN, loopback services and abstract Unix sockets. Only grant it to trusted games.

To let a game update its own files for one launch:

```sh
box-rpg launch /path/to/game --allow-game-writes
```

The permission is not saved. The game directory is mounted writable, so only
use it with games you trust not to damage their own assets. Saves keep their
usual locations; the flag only lifts the read-only protection on game files.

## Install the required runtime

If no compatible runtime is installed, install one for the detected game type,
then run `box-rpg` again from the game directory.

### RPG Maker MV/MZ

MV/MZ games need NW.js. Choose a stable version from the interactive list; the
architecture is detected automatically.

```sh
box-rpg runtime nwjs available --interactive
```

Use `n` and `p` to browse version pages, enter a number to choose a version,
and confirm the installation.

When several matching runtimes are installed and no version was requested
(`--runtime`) or preferred (`box-rpg config set preferred-runtime`), an
interactive launch asks which one to use; empty input keeps the newest.
Non-interactive launches always use the newest without asking.

NW.js uses the owned Wayland socket selected by
`WAYLAND_DISPLAY` under `XDG_RUNTIME_DIR`. On X11 the launcher asks first and
then exposes only the local X socket with `DISPLAY` set. GPU devices under
`/dev/dri` and your PipeWire audio socket are exposed after validation so games
can render and play sound; both widen the sandbox (see [security and compatibility limits](security.md)).

Some desktop exports need an auxiliary file from the game root. The supported
workaround is to copy the required direct game-root file into the isolated
session:

```sh
box-rpg launch --copy-root-file game_messages.csv
```

`--copy-root-file` can be repeated for multiple files. It accepts only direct,
regular files from the validated game root and rejects paths containing
directories, symlinks, or session-owned names. Each copied file is limited to
16 MiB; FIFOs and other nonregular files are rejected without waiting for input.

Each NW.js game uses a persistent private Chromium/NW.js profile at
`$XDG_CACHE_HOME/box-rpg/profiles/<opaque-game-id>/sandbox` (or the corresponding
path under `~/.cache`). The profile stores preferences, web storage, and cache.
Existing profiles from older layouts are not migrated automatically.
RPG Maker saves remain in `save/` beside the entrypoint, usually `<game>/www/save/`
or `<game>/save/`. Existing saves are used directly, without copying to XDG data.

### RPG Maker 2000/2003

These projects are detected from `RPG_RT.ini`, `RPG_RT.ldb`, and `RPG_RT.lmt`.
Choose an x64 EasyRPG Player version:

```sh
box-rpg runtime easyrpg available --interactive
```

EasyRPG Player launches the project fullscreen with its project path set to the
game directory. The launcher creates `<game>/save/` if needed and passes it as
`--save-path` through the sandbox view. New files such as `Save01.lsd` and
`Save02.lsd` appear there immediately. Only that directory is writable, not the
entire game root.

The managed EasyRPG Player build has no Wayland video driver, so it runs
through XWayland: on a Wayland session the launcher asks once per launch for
explicit X11 consent (as described above) and exposes only the local X socket
next to the Wayland one, letting SDL negotiate the backend. Without XWayland
the game cannot open a window; `diagnose` still works because it never opens
one.

When several player versions are installed, an interactive launch asks which
one to use; empty input keeps the newest. Non-interactive launches always use
the newest without asking. Pass an explicit version with
`box-rpg launch /path/to/game --runtime 0.8.1.1` to skip the question
(`diagnose` accepts `--runtime` the same way).

To continue older saves, close the game, back up the root-level `Save*.lsd` files,
and copy them into `<game>/save/` without overwriting newer slots. There is no
automatic migration. Partially tested older launcher builds may also have saves
under XDG data or cached profiles: recover those manually before cleaning cache.

## Manage cached data

Inspect launcher-managed cleanup selectors as JSON Lines:

```sh
box-rpg cleanup list
```

Use `roots`, `runtimes`, `downloads`, or `profiles` to list one category. Remove
one listed item or every item in a category:

```sh
box-rpg cleanup remove CATEGORY SELECTOR
box-rpg cleanup remove CATEGORY --all
```

Removal prompts for confirmation. Add `--yes` to either command for immediate
noninteractive deletion; without a terminal, `--yes` is required.

`box-rpg cleanup all` shows the global scope and requires `DELETE ALL`. Add
`--yes` for immediate noninteractive deletion. To browse the interactive menu
instead, run `box-rpg cleanup --interactive`; it is incompatible with `--yes`.
Run `box-rpg cleanup -h` to see the available cleanup actions.

Cleanup only removes launcher-owned data; see
[security and compatibility limits](security.md) for the exact guarantees.

## Configuration and safety

Configuration is stored in `$XDG_CONFIG_HOME/box-rpg/config.toml` (or
`~/.config/box-rpg/config.toml`). To authorize an entire game library:

```sh
box-rpg config set allowed-game-root /home/alice/games
```

The launcher removes configured roots that no longer exist when it authorizes a
game. Existing roots are retained because they may be game libraries.

Game paths must stay below an authorized root; see
[security and compatibility limits](security.md) for the trust model and its
limits.
